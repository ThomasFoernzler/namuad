from urllib.parse import parse_qs

import httpx
import pytest

from navidrome_music_adder.config import Settings
from navidrome_music_adder.services.navidrome import NavidromeClient


@pytest.mark.asyncio
async def test_inventory_and_create_playlist_as_user() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "search3" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "subsonic-response": {
                        "status": "ok",
                        "searchResult3": {
                            "song": [
                                {
                                    "id": "song-1",
                                    "title": "Track",
                                    "artist": "Artist",
                                    "isrc": [],
                                    "path": "Artist/Album/USRC17607839.flac",
                                }
                            ]
                        },
                    }
                },
            )
        return httpx.Response(
            200,
            json={"subsonic-response": {"status": "ok", "playlist": {"id": "p1"}}},
        )

    client = NavidromeClient(
        Settings(navidrome_page_size=500), transport=httpx.MockTransport(handler)
    )
    inventory = await client.inventory_by_isrc("alice")
    playlist_id = await client.create_playlist("alice", "Snapshot", ["song-1", "song-1"])
    await client.close()

    assert inventory["USRC17607839"][0].id == "song-1"
    assert playlist_id == "p1"
    assert all(request.headers["Remote-User"] == "alice" for request in requests)
    query = parse_qs(requests[-1].content.decode())
    assert query["songId"] == ["song-1", "song-1"]


@pytest.mark.asyncio
async def test_replaces_existing_playlist_with_same_name() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "getPlaylists" in request.url.path:
            body = {
                "status": "ok",
                "playlists": {
                    "playlist": [
                        {"id": "existing", "name": "Snapshot", "owner": "alice"},
                        {"id": "public", "name": "Snapshot", "owner": "bob"},
                    ]
                },
            }
        elif "getPlaylist" in request.url.path:
            body = {
                "status": "ok",
                "playlist": {"id": "existing", "entry": [{"id": "old-1"}, {"id": "old-2"}]},
            }
        else:
            body = {"status": "ok"}
        return httpx.Response(200, json={"subsonic-response": body})

    client = NavidromeClient(Settings(), transport=httpx.MockTransport(handler))
    playlist_id = await client.create_or_replace_playlist(
        "alice", "Snapshot", ["new-1", "new-2"]
    )
    await client.close()

    assert playlist_id == "existing"
    update = requests[-1]
    assert "updatePlaylist" in update.url.path
    assert update.method == "POST"
    params = parse_qs(update.content.decode())
    assert params["playlistId"] == ["existing"]
    assert params["songIndexToRemove"] == ["0", "1"]
    assert params["songIdToAdd"] == ["new-1", "new-2"]
