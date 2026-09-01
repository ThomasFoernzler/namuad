from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from urllib.parse import urlencode

import httpx

from ..config import Settings
from ..domain import NavidromePlaylistSummary, NavidromeSong, normalize_isrc, normalize_title


@dataclass
class LibraryInventory:
    by_isrc: dict[str, list[NavidromeSong]] = field(default_factory=dict)
    by_title: dict[str, list[NavidromeSong]] = field(default_factory=dict)


class NavidromeError(RuntimeError):
    pass


class NavidromeClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.navidrome_url.rstrip("/"),
            timeout=settings.provider_timeout_seconds,
            transport=transport,
        )

    async def _request(
        self,
        username: str,
        endpoint: str,
        params: list[tuple[str, str]] | dict[str, str | int] | None = None,
    ) -> dict:
        common = [("v", "1.16.1"), ("c", self.settings.navidrome_client_name), ("f", "json")]
        if isinstance(params, dict):
            request_params = common + [(key, str(value)) for key, value in params.items()]
        else:
            request_params = common + (params or [])
        request_args = {
            "headers": {self.settings.navidrome_user_header: username},
        }
        if endpoint in {"createPlaylist", "updatePlaylist"}:
            response = await self._client.post(
                f"/rest/{endpoint}.view",
                content=urlencode(request_params),
                headers={
                    **request_args["headers"],
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
        else:
            response = await self._client.get(
                f"/rest/{endpoint}.view", params=request_params, **request_args
            )
        response.raise_for_status()
        payload = response.json().get("subsonic-response", {})
        if payload.get("status") != "ok":
            error = payload.get("error", {})
            raise NavidromeError(error.get("message", f"Navidrome {endpoint} failed"))
        return payload

    async def ping(self, username: str) -> None:
        await self._request(username, "ping")

    async def inventory(self, username: str) -> LibraryInventory:
        offset = 0
        by_isrc: dict[str, list[NavidromeSong]] = defaultdict(list)
        by_title: dict[str, list[NavidromeSong]] = defaultdict(list)
        while True:
            payload = await self._request(
                username,
                "search3",
                {
                    "query": "",
                    "artistCount": 0,
                    "albumCount": 0,
                    "songCount": self.settings.navidrome_page_size,
                    "songOffset": offset,
                },
            )
            items = payload.get("searchResult3", {}).get("song", [])
            for item in items:
                song = NavidromeSong.from_api(item)
                by_title[normalize_title(song.title)].append(song)
                for isrc in song.isrc:
                    by_isrc[isrc].append(song)
                if song.path:
                    filename_isrc = normalize_isrc(PurePosixPath(song.path).stem)
                    if filename_isrc and song not in by_isrc[filename_isrc]:
                        by_isrc[filename_isrc].append(song)
            if len(items) < self.settings.navidrome_page_size:
                break
            offset += len(items)
        return LibraryInventory(dict(by_isrc), dict(by_title))

    async def inventory_by_isrc(self, username: str) -> dict[str, list[NavidromeSong]]:
        return (await self.inventory(username)).by_isrc

    async def create_playlist(self, username: str, name: str, song_ids: list[str]) -> str:
        params = [("name", name)] + [("songId", song_id) for song_id in song_ids]
        payload = await self._request(username, "createPlaylist", params)
        playlist = payload.get("playlist")
        if not playlist or not playlist.get("id"):
            raise NavidromeError("Navidrome did not return the new playlist ID")
        return str(playlist["id"])

    async def list_playlists(self, username: str) -> list[NavidromePlaylistSummary]:
        payload = await self._request(username, "getPlaylists")
        items = payload.get("playlists", {}).get("playlist", [])
        return [
            NavidromePlaylistSummary(
                id=str(item["id"]),
                name=item.get("name") or "Untitled playlist",
                owner=item.get("owner"),
                song_count=int(item.get("songCount") or 0),
            )
            for item in items
            if not item.get("owner") or item.get("owner") == username
        ]

    async def find_playlist_by_name(
        self, username: str, name: str
    ) -> NavidromePlaylistSummary | None:
        return next(
            (playlist for playlist in await self.list_playlists(username) if playlist.name == name),
            None,
        )

    async def replace_playlist(
        self, username: str, playlist_id: str, song_ids: list[str]
    ) -> str:
        payload = await self._request(username, "getPlaylist", {"id": playlist_id})
        current = payload.get("playlist", {}).get("entry", [])
        params = [("playlistId", playlist_id)]
        params.extend(("songIndexToRemove", str(index)) for index in range(len(current)))
        params.extend(("songIdToAdd", song_id) for song_id in song_ids)
        await self._request(username, "updatePlaylist", params)
        return playlist_id

    async def create_or_replace_playlist(
        self,
        username: str,
        name: str,
        song_ids: list[str],
        playlist_id: str | None = None,
    ) -> str:
        if not playlist_id:
            existing = await self.find_playlist_by_name(username, name)
            playlist_id = existing.id if existing else None
        if playlist_id:
            return await self.replace_playlist(username, playlist_id, song_ids)
        return await self.create_playlist(username, name, song_ids)

    async def close(self) -> None:
        await self._client.aclose()


def pick_track_match(
    inventory: LibraryInventory,
    *,
    isrc: str | None,
    title: str,
    artists: list[str],
    duration_seconds: int | None,
) -> NavidromeSong | None:
    """Choose the first match. Order is intentionally unspecified for duplicate ISRCs."""
    normalized = normalize_isrc(isrc)
    matches = inventory.by_isrc.get(normalized, []) if normalized else []
    if matches:
        return matches[0]
    if isrc:
        return None
    candidates = inventory.by_title.get(normalize_title(title), [])
    expected_artist = normalize_title(artists[0]) if artists else ""
    for candidate in candidates:
        if expected_artist and expected_artist not in normalize_title(candidate.artist):
            continue
        if (
            duration_seconds is not None
            and candidate.duration is not None
            and abs(duration_seconds - candidate.duration) > 3
        ):
            continue
        return candidate
    return None
