from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


def default_data_dir(
    root: Path,
    *,
    platform_name: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> Path:
    effective_platform = platform_name or os.name
    effective_environment = environment if environment is not None else os.environ
    if effective_platform == "nt" and effective_environment.get("LOCALAPPDATA"):
        # Keep the deeply nested Hugging Face cache away from the checkout so
        # ordinary Windows installations are less likely to hit legacy MAX_PATH.
        return Path(effective_environment["LOCALAPPDATA"]) / "KaraokeStudio"
    return root / ".karaoke-studio-data"


@dataclass(frozen=True)
class Settings:
    root: Path
    data_dir: Path
    ffmpeg: str
    ffprobe: str
    host: str
    port: int
    frontend_origin: str

    @classmethod
    def load(cls) -> Settings:
        root = Path(__file__).resolve().parents[2]
        data_dir = Path(os.environ.get("KARAOKE_STUDIO_DATA", default_data_dir(root)))
        return cls(
            root=root,
            data_dir=data_dir.resolve(),
            ffmpeg=os.environ.get("KARAOKE_STUDIO_FFMPEG", "ffmpeg"),
            ffprobe=os.environ.get("KARAOKE_STUDIO_FFPROBE", "ffprobe"),
            host="127.0.0.1",
            port=int(os.environ.get("KARAOKE_STUDIO_PORT", "8000")),
            frontend_origin=os.environ.get("KARAOKE_STUDIO_FRONTEND", "http://127.0.0.1:3000"),
        )

    def ensure(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "projects").mkdir(exist_ok=True)
        (self.data_dir / "models").mkdir(exist_ok=True)


settings = Settings.load()
