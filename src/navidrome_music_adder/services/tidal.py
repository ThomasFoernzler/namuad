import asyncio
import json
import os
import re
from datetime import UTC, datetime

import tidalapi
from cryptography.fernet import Fernet, InvalidToken

from ..config import Settings
from ..domain import SourcePlaylist, SourceTrack, TidalPlaylistSummary

PLAYLIST_RE = re.compile(
    r"(?:https?://(?:listen\.)?tidal\.com/(?:browse/)?playlist/)?"
    r"([0-9a-fA-F-]{36})(?:[/?#].*)?$"
)
TRACK_RE = re.compile(
    r"(?:https?://(?:listen\.)?tidal\.com/(?:browse/)?track/)?"
    r"(\d+)(?:[/?#].*)?$"
)


class TidalNotAuthenticated(RuntimeError):
    pass


class TidalClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.session: tidalapi.Session | None = None
        self._login_session: tidalapi.Session | None = None
        key = settings.token_encryption_key
        self._fernet = Fernet(key.get_secret_value().encode()) if key else None

    @staticmethod
    def parse_playlist_id(value: str) -> str:
        match = PLAYLIST_RE.fullmatch(value.strip())
        if not match:
            raise ValueError("Enter a valid public TIDAL playlist URL")
        return match.group(1)

    @staticmethod
    def parse_track_id(value: str) -> str:
        match = TRACK_RE.fullmatch(value.strip())
        if not match:
            raise ValueError("Enter a valid TIDAL playlist or track URL")
        return match.group(1)

    async def _authenticated_session(self) -> tidalapi.Session:
        if not self.session and not await self.initialize():
            raise TidalNotAuthenticated("Authenticate the service with TIDAL first")
        assert self.session is not None
        return self.session

    async def account(self) -> dict | None:
        try:
            session = await self._authenticated_session()
        except TidalNotAuthenticated:
            return None
        user = session.user
        return {
            "id": str(user.id) if user and user.id is not None else None,
            "username": getattr(user, "username", None),
            "email": getattr(user, "email", None),
        }

    async def logout(self) -> None:
        self.session = None
        self._login_session = None
        await asyncio.to_thread(self.settings.tidal_session_file.unlink, missing_ok=True)

    async def list_playlists(self) -> list[TidalPlaylistSummary]:
        session = await self._authenticated_session()
        if session.user is None:
            raise TidalNotAuthenticated("TIDAL session has no user")
        playlists = []
        offset = 0
        while True:
            page = await asyncio.to_thread(
                session.user.playlist_and_favorite_playlists, offset, 50
            )
            playlists.extend(page)
            if len(page) < 50:
                break
            offset += len(page)

        summaries = []
        for playlist in playlists:
            image_url = None
            try:
                image_url = playlist.image(320)
            except AttributeError:
                pass
            summaries.append(
                TidalPlaylistSummary(
                    id=str(playlist.id),
                    name=playlist.name or "Untitled playlist",
                    description=playlist.description or None,
                    track_count=max(0, playlist.num_tracks),
                    public=bool(playlist.public),
                    url=playlist.share_url,
                    image_url=image_url,
                )
            )
        return summaries

    async def initialize(self) -> bool:
        if not self._fernet:
            return False
        try:
            document = json.loads(
                await asyncio.to_thread(self.settings.tidal_session_file.read_text)
            )
            encrypted = document["encrypted_payload"]
            data = json.loads(self._fernet.decrypt(encrypted.encode()))
        except (OSError, KeyError, TypeError, InvalidToken, ValueError, json.JSONDecodeError):
            return False
        session = tidalapi.Session()
        loaded = await asyncio.to_thread(
            session.load_oauth_session,
            token_type="Bearer",
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expiry_time=datetime.fromisoformat(data["expires_at"]),
            is_pkce=True,
        )
        if loaded and await asyncio.to_thread(session.check_login):
            self.session = session
            return True
        return False

    async def begin_login(self) -> str:
        if not self._fernet:
            raise RuntimeError("NMA_TOKEN_ENCRYPTION_KEY must be configured")
        self._login_session = tidalapi.Session()
        return await asyncio.to_thread(self._login_session.pkce_login_url)

    async def finish_login(self, redirect_url: str) -> None:
        if not self._login_session or not self._fernet:
            raise RuntimeError("Start TIDAL login first")
        token = await asyncio.to_thread(self._login_session.pkce_get_auth_token, redirect_url)
        await asyncio.to_thread(self._login_session.process_auth_token, token, is_pkce_token=True)
        if not await asyncio.to_thread(self._login_session.check_login):
            raise TidalNotAuthenticated("TIDAL login failed")
        self.session = self._login_session
        payload = {
            "access_token": self.session.access_token,
            "refresh_token": self.session.refresh_token,
            "expires_at": self.session.expiry_time.astimezone(UTC).isoformat(),
        }
        encrypted = self._fernet.encrypt(json.dumps(payload).encode()).decode()
        await asyncio.to_thread(self._write_session_file, encrypted)

    def _write_session_file(self, encrypted_payload: str) -> None:
        path = self.settings.tidal_session_file
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(
            json.dumps({"version": 1, "encrypted_payload": encrypted_payload}) + "\n"
        )
        temporary.chmod(0o600)
        os.replace(temporary, path)

    async def load_playlist(self, value: str) -> SourcePlaylist:
        session = await self._authenticated_session()
        try:
            playlist_id = self.parse_playlist_id(value)
        except ValueError:
            return await self._load_track(session, value)
        playlist = await asyncio.to_thread(session.playlist, playlist_id)
        tracks: list[SourceTrack] = []
        skipped = 0
        offset = 0
        while True:
            page = await asyncio.to_thread(playlist.tracks, 100, offset)
            for item in page:
                # Video/podcast-like objects and unavailable items are snapshots but not imports.
                if not isinstance(item, tidalapi.media.Track) or not getattr(
                    item, "available", True
                ):
                    skipped += 1
                    continue
                tracks.append(
                    SourceTrack(
                        position=len(tracks),
                        source_track_id=str(item.id),
                        source_url=f"https://listen.tidal.com/track/{item.id}",
                        title=item.name,
                        artists=[artist.name for artist in item.artists],
                        album=item.album.name if item.album else None,
                        duration_seconds=getattr(item, "duration", None),
                        isrc=getattr(item, "isrc", None),
                        explicit=bool(getattr(item, "explicit", False)),
                    )
                )
            if len(page) < 100:
                break
            offset += len(page)
        return SourcePlaylist(
            source_playlist_id=playlist_id,
            source_url=f"https://listen.tidal.com/playlist/{playlist_id}",
            name=playlist.name,
            description=getattr(playlist, "description", None),
            tracks=tracks,
            skipped_items=skipped,
        )

    async def _load_track(self, session: tidalapi.Session, value: str) -> SourcePlaylist:
        track_id = self.parse_track_id(value)
        track = await asyncio.to_thread(session.track, track_id)
        if not isinstance(track, tidalapi.media.Track) or not getattr(track, "available", True):
            raise ValueError("This TIDAL track is unavailable")
        artists = [artist.name for artist in track.artists]
        title = track.name
        return SourcePlaylist(
            source_playlist_id=f"track:{track_id}",
            source_url=f"https://listen.tidal.com/track/{track_id}",
            name=f"{title} — {', '.join(artists)}" if artists else title,
            tracks=[
                SourceTrack(
                    position=0,
                    source_track_id=str(track.id),
                    source_url=f"https://listen.tidal.com/track/{track.id}",
                    title=title,
                    artists=artists,
                    album=track.album.name if track.album else None,
                    duration_seconds=getattr(track, "duration", None),
                    isrc=getattr(track, "isrc", None),
                    explicit=bool(getattr(track, "explicit", False)),
                )
            ],
        )
