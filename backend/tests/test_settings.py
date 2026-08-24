from __future__ import annotations

from pathlib import Path

from karaoke_studio.settings import default_data_dir


def test_default_data_dir_preserves_project_local_storage_on_posix(tmp_path: Path) -> None:
    assert default_data_dir(
        tmp_path,
        platform_name="posix",
        environment={},
    ) == tmp_path / ".karaoke-studio-data"


def test_default_data_dir_uses_short_local_appdata_path_on_windows(tmp_path: Path) -> None:
    local_appdata = tmp_path / "AppData" / "Local"

    assert default_data_dir(
        tmp_path / "deep" / "source" / "checkout",
        platform_name="nt",
        environment={"LOCALAPPDATA": str(local_appdata)},
    ) == local_appdata / "KaraokeStudio"


def test_default_data_dir_windows_falls_back_when_localappdata_is_missing(
    tmp_path: Path,
) -> None:
    assert default_data_dir(
        tmp_path,
        platform_name="nt",
        environment={},
    ) == tmp_path / ".karaoke-studio-data"
