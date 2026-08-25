from __future__ import annotations

import os
import stat
import struct
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

FULL_FRAME_RATE_PLAYBACK_INTENT_KEY = (
    b"com.apple.quicktime.full-frame-rate-playback-intent"
)
QUICKTIME_UINT8_DATA_TYPE = 22
_CONTAINER_ATOMS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf"}
_PLAYBACK_INTENT_LOCKS: dict[str, threading.Lock] = {}
_PLAYBACK_INTENT_LOCKS_GUARD = threading.Lock()


class QuickTimeMetadataError(RuntimeError):
    pass


def _atom(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def _full_frame_rate_metadata_atom() -> bytes:
    """Build Apple's movie-level UInt8 full-frame-rate playback intent."""
    handler = _atom(
        b"hdlr",
        b"\0\0\0\0" + b"\0\0\0\0" + b"mdta" + b"\0" * 14,
    )
    key_entry = (
        struct.pack(">I4s", 8 + len(FULL_FRAME_RATE_PLAYBACK_INTENT_KEY), b"mdta")
        + FULL_FRAME_RATE_PLAYBACK_INTENT_KEY
    )
    keys = _atom(b"keys", b"\0\0\0\0" + struct.pack(">I", 1) + key_entry)
    value = _atom(
        b"data",
        struct.pack(">II", QUICKTIME_UINT8_DATA_TYPE, 0) + b"\x01",
    )
    items = _atom(b"ilst", _atom(b"\0\0\0\x01", value))
    return _atom(b"meta", handler + keys + items)


def _atoms(
    data: bytes | bytearray,
    start: int,
    end: int,
) -> Iterator[tuple[int, int, int, bytes]]:
    position = start
    while position + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, position)
        header_size = 8
        if size == 1:
            if position + 16 > end:
                raise QuickTimeMetadataError("MP4 atom mở rộng bị cắt cụt.")
            size = struct.unpack_from(">Q", data, position + 8)[0]
            header_size = 16
        elif size == 0:
            size = end - position
        if size < header_size or position + size > end:
            raise QuickTimeMetadataError("Cấu trúc atom MP4 không hợp lệ.")
        yield position, size, header_size, kind
        position += size
    if position != end:
        raise QuickTimeMetadataError("MP4 có dữ liệu atom dư hoặc bị cắt cụt.")


def _top_level_atoms(path: Path) -> list[tuple[int, int, int, bytes]]:
    atoms: list[tuple[int, int, int, bytes]] = []
    file_size = path.stat().st_size
    with path.open("rb") as handle:
        position = 0
        while position + 8 <= file_size:
            handle.seek(position)
            header = handle.read(16)
            if len(header) < 8:
                break
            size, kind = struct.unpack_from(">I4s", header)
            header_size = 8
            if size == 1:
                if len(header) < 16:
                    raise QuickTimeMetadataError("MP4 atom mở rộng bị cắt cụt.")
                size = struct.unpack_from(">Q", header, 8)[0]
                header_size = 16
            elif size == 0:
                size = file_size - position
            if size < header_size or position + size > file_size:
                raise QuickTimeMetadataError("Cấu trúc atom MP4 không hợp lệ.")
            atoms.append((position, size, header_size, kind))
            position += size
    if not atoms or position != file_size:
        raise QuickTimeMetadataError("Không đọc được toàn bộ cấu trúc MP4.")
    return atoms


def _adjust_chunk_offsets(
    atom_data: bytearray,
    start: int,
    end: int,
    delta: int,
    shift_from: int,
) -> None:
    for position, size, header_size, kind in _atoms(atom_data, start, end):
        payload_start = position + header_size
        if kind in _CONTAINER_ATOMS:
            _adjust_chunk_offsets(
                atom_data,
                payload_start,
                position + size,
                delta,
                shift_from,
            )
            continue
        if kind not in {b"stco", b"co64"}:
            continue
        if payload_start + 8 > position + size:
            raise QuickTimeMetadataError("Atom chunk offset MP4 bị cắt cụt.")
        count = struct.unpack_from(">I", atom_data, payload_start + 4)[0]
        width = 4 if kind == b"stco" else 8
        number_format = ">I" if width == 4 else ">Q"
        first_offset = payload_start + 8
        if first_offset + count * width > position + size:
            raise QuickTimeMetadataError("Số lượng chunk offset MP4 không hợp lệ.")
        for index in range(count):
            offset = first_offset + index * width
            value = struct.unpack_from(number_format, atom_data, offset)[0]
            shifted = value + delta if value >= shift_from else value
            if width == 4 and shifted > 0xFFFFFFFF:
                raise QuickTimeMetadataError(
                    "MP4 vượt giới hạn stco khi thêm metadata QuickTime."
                )
            struct.pack_into(number_format, atom_data, offset, shifted)


def _copy_range(source, destination, byte_count: int) -> None:
    remaining = byte_count
    while remaining:
        chunk = source.read(min(4 * 1024 * 1024, remaining))
        if not chunk:
            raise QuickTimeMetadataError("MP4 bị cắt cụt trong lúc ghi metadata.")
        destination.write(chunk)
        remaining -= len(chunk)


def inject_full_frame_rate_playback_intent(path: Path) -> None:
    """Atomically add Apple's typed real-time HFR intent to an MP4/MOV file."""
    lock_key = os.path.normcase(str(path.resolve()))
    with _PLAYBACK_INTENT_LOCKS_GUARD:
        path_lock = _PLAYBACK_INTENT_LOCKS.setdefault(lock_key, threading.Lock())
    with path_lock:
        _inject_full_frame_rate_playback_intent(path)


def _inject_full_frame_rate_playback_intent(path: Path) -> None:
    top_level = _top_level_atoms(path)
    try:
        moov_position, moov_size, moov_header_size, _ = next(
            atom for atom in top_level if atom[3] == b"moov"
        )
        next(atom for atom in top_level if atom[3] == b"mdat")
    except StopIteration as exc:
        raise QuickTimeMetadataError("MP4 thiếu moov hoặc mdat atom.") from exc

    with path.open("rb") as source:
        source.seek(moov_position)
        moov_data = bytearray(source.read(moov_size))
    if len(moov_data) != moov_size:
        raise QuickTimeMetadataError("Moov atom bị cắt cụt.")
    if FULL_FRAME_RATE_PLAYBACK_INTENT_KEY in moov_data:
        if read_full_frame_rate_playback_intent(path) == 1:
            return
        raise QuickTimeMetadataError(
            "MP4 đã có playback intent nhưng không phải UInt8 real-time hợp lệ."
        )

    metadata = _full_frame_rate_metadata_atom()
    _adjust_chunk_offsets(
        moov_data,
        moov_header_size,
        len(moov_data),
        len(metadata),
        moov_position + moov_size,
    )
    new_moov_size = moov_size + len(metadata)
    if moov_header_size == 8:
        if new_moov_size > 0xFFFFFFFF:
            raise QuickTimeMetadataError("Moov atom vượt giới hạn 32-bit.")
        struct.pack_into(">I", moov_data, 0, new_moov_size)
    else:
        struct.pack_into(">Q", moov_data, 8, new_moov_size)

    original_stat = path.stat()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.hfr-metadata.",
        suffix=".tmp",
        dir=path.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        original_size = original_stat.st_size
        with path.open("rb") as source, temporary.open("wb") as destination:
            _copy_range(source, destination, moov_position)
            destination.write(moov_data)
            destination.write(metadata)
            source.seek(moov_position + moov_size)
            _copy_range(
                source,
                destination,
                original_size - (moov_position + moov_size),
            )
            destination.flush()
            os.fsync(destination.fileno())
        os.chmod(temporary, stat.S_IMODE(original_stat.st_mode))
        if read_full_frame_rate_playback_intent(temporary) != 1:
            raise QuickTimeMetadataError(
                "Không xác nhận được playback intent UInt8 trước khi thay MP4."
            )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

    if read_full_frame_rate_playback_intent(path) != 1:
        raise QuickTimeMetadataError(
            "Không xác nhận được playback intent UInt8 sau khi ghi MP4."
        )


def _metadata_children(
    metadata: bytes | bytearray,
    header_size: int,
) -> list[tuple[int, int, int, bytes]]:
    for offset in (header_size, header_size + 4):
        try:
            items = list(_atoms(metadata, offset, len(metadata)))
        except QuickTimeMetadataError:
            continue
        if not items or items[0][3] != b"hdlr":
            continue
        handler_position, handler_size, handler_header, _ = items[0]
        handler_payload = handler_position + handler_header
        if (
            handler_size >= handler_header + 12
            and metadata[handler_payload + 8 : handler_payload + 12] == b"mdta"
        ):
            return items
    return []


def is_legacy_quicktime_hfr_export(filename: str) -> bool:
    """Identify old 1080p120 exports that QuickTime may interpret as slow motion."""
    normalized = Path(filename).name.casefold()
    return normalized.endswith("-1080p120.mp4") and "-hfr-realtime-v1-" not in normalized


def read_full_frame_rate_playback_intent(path: Path) -> int | None:
    """Return the intent only when stored as Apple's required UInt8 metadata."""
    top_level = _top_level_atoms(path)
    try:
        moov_position, moov_size, moov_header_size, _ = next(
            atom for atom in top_level if atom[3] == b"moov"
        )
    except StopIteration:
        return None
    with path.open("rb") as source:
        source.seek(moov_position)
        moov = source.read(moov_size)
    for position, size, header_size, kind in _atoms(
        moov, moov_header_size, len(moov)
    ):
        if kind != b"meta":
            continue
        metadata = moov[position : position + size]
        children = _metadata_children(metadata, header_size)
        keys_atom = next((item for item in children if item[3] == b"keys"), None)
        items_atom = next((item for item in children if item[3] == b"ilst"), None)
        if not keys_atom or not items_atom:
            continue
        key_position, key_size, key_header, _ = keys_atom
        key_cursor = key_position + key_header
        if key_cursor + 8 > key_position + key_size:
            continue
        key_count = struct.unpack_from(">I", metadata, key_cursor + 4)[0]
        key_cursor += 8
        intent_index: int | None = None
        for index in range(1, key_count + 1):
            if key_cursor + 8 > key_position + key_size:
                break
            entry_size, namespace = struct.unpack_from(">I4s", metadata, key_cursor)
            if entry_size < 8 or key_cursor + entry_size > key_position + key_size:
                break
            value = metadata[key_cursor + 8 : key_cursor + entry_size]
            if namespace == b"mdta" and value == FULL_FRAME_RATE_PLAYBACK_INTENT_KEY:
                intent_index = index
                break
            key_cursor += entry_size
        if intent_index is None:
            continue
        item_position, item_size, item_header, _ = items_atom
        for entry_position, entry_size, entry_header, entry_kind in _atoms(
            metadata,
            item_position + item_header,
            item_position + item_size,
        ):
            if entry_kind != struct.pack(">I", intent_index):
                continue
            for data_position, data_size, data_header, data_kind in _atoms(
                metadata,
                entry_position + entry_header,
                entry_position + entry_size,
            ):
                if data_kind != b"data" or data_size < data_header + 9:
                    continue
                data_type, _locale = struct.unpack_from(
                    ">II", metadata, data_position + data_header
                )
                value = metadata[data_position + data_header + 8 : data_position + data_size]
                if data_type == QUICKTIME_UINT8_DATA_TYPE and len(value) == 1:
                    return value[0]
    return None
