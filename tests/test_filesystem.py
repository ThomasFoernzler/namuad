from pathlib import Path

import pytest
from pydantic import SecretStr

from navidrome_music_adder.config import Settings
from navidrome_music_adder.services.filesystem import MusicFilesystem


@pytest.mark.asyncio
async def test_indexes_isrc_filenames_but_ignores_misc(tmp_path: Path) -> None:
    (tmp_path / "Artist").mkdir()
    (tmp_path / "Artist" / "USRC17607839.flac").touch()
    (tmp_path / "misc").mkdir()
    (tmp_path / "misc" / "GBAYE0601696.flac").touch()
    settings = Settings(music_path=tmp_path, secret_key=SecretStr("test"))

    inventory = await MusicFilesystem(settings).inventory()
    assert inventory.available is True
    assert inventory.isrcs == {"USRC17607839"}
    assert "gbaye0601696" in inventory.misc_titles


@pytest.mark.asyncio
async def test_reports_unavailable_when_music_path_is_disabled() -> None:
    settings = Settings(music_path=None, secret_key=SecretStr("test"))

    inventory = await MusicFilesystem(settings).inventory()

    assert inventory.available is False
    assert inventory.isrcs == set()
    assert inventory.misc_titles == set()


def test_empty_music_path_disables_filesystem_access() -> None:
    assert Settings(music_path="").music_path is None
