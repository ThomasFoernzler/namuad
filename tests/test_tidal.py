import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from navidrome_music_adder.config import Settings
from navidrome_music_adder.services.tidal import TidalClient


def test_parses_tidal_track_links() -> None:
    assert TidalClient.parse_track_id("https://tidal.com/browse/track/123456") == "123456"
    assert TidalClient.parse_track_id("https://listen.tidal.com/track/123456?foo=bar") == "123456"


@pytest.mark.asyncio
async def test_lists_connected_users_playlists(monkeypatch) -> None:
    async def call_directly(function, *args):
        return function(*args)

    monkeypatch.setattr(asyncio, "to_thread", call_directly)
    playlist = SimpleNamespace(
        id="playlist-id",
        name="Favorites",
        description="My playlist",
        num_tracks=12,
        public=False,
        share_url="https://tidal.com/browse/playlist/playlist-id",
        image=lambda dimensions: f"https://images.example/{dimensions}.jpg",
    )
    user = SimpleNamespace(
        playlist_and_favorite_playlists=lambda offset, limit: [playlist]
    )
    client = TidalClient(Settings())
    client.session = SimpleNamespace(user=user)  # type: ignore[assignment]

    playlists = await client.list_playlists()

    assert len(playlists) == 1
    assert playlists[0].name == "Favorites"
    assert playlists[0].track_count == 12
    assert playlists[0].image_url == "https://images.example/320.jpg"


@pytest.mark.asyncio
async def test_tidal_session_is_encrypted_in_json_file(
    monkeypatch, tmp_path: Path
) -> None:
    async def call_directly(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", call_directly)
    key = Fernet.generate_key()
    session_file = tmp_path / "tidal-session.json"
    client = TidalClient(
        Settings(
            token_encryption_key=key.decode(),
            tidal_session_file=session_file,
        )
    )
    client._login_session = SimpleNamespace(
        pkce_get_auth_token=lambda redirect_url: "token-response",
        process_auth_token=lambda token, is_pkce_token: None,
        check_login=lambda: True,
        access_token="access-token",
        refresh_token="refresh-token",
        expiry_time=datetime.now(UTC) + timedelta(hours=1),
    )

    await client.finish_login("https://login.tidal.com/callback?code=test")

    document = json.loads(session_file.read_text())
    assert document["version"] == 1
    decrypted = json.loads(Fernet(key).decrypt(document["encrypted_payload"].encode()))
    assert decrypted["access_token"] == "access-token"
    assert decrypted["refresh_token"] == "refresh-token"
    assert session_file.stat().st_mode & 0o777 == 0o600

    restored_session = SimpleNamespace(
        load_oauth_session=lambda **kwargs: True,
        check_login=lambda: True,
    )
    monkeypatch.setattr(
        "navidrome_music_adder.services.tidal.tidalapi.Session",
        lambda: restored_session,
    )
    restored = TidalClient(client.settings)

    assert await restored.initialize()
    assert restored.session is restored_session

    await client.logout()

    assert not session_file.exists()
