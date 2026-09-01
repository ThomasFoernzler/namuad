import asyncio
import logging
from datetime import UTC, datetime

from .config import Settings
from .domain import ImportStatus, SourcePlaylist, TrackStatus, normalize_title
from .services.filesystem import MusicFilesystem
from .services.navidrome import NavidromeClient, pick_track_match
from .services.tidal import TidalClient
from .services.tidarr import TidarrClient, TidarrStatus
from .store import ImportJob, ImportStore, ImportTrack

logger = logging.getLogger(__name__)


class ImportWorkflow:
    def __init__(
        self,
        settings: Settings,
        store: ImportStore,
        tidal: TidalClient,
        tidarr: TidarrClient,
        navidrome: NavidromeClient,
        filesystem: MusicFilesystem,
    ):
        self.settings = settings
        self.store = store
        self.tidal = tidal
        self.tidarr = tidarr
        self.navidrome = navidrome
        self.filesystem = filesystem
        self._advance_lock = asyncio.Lock()

    async def create(self, *, oidc_subject: str, username: str, source_url: str) -> ImportJob:
        snapshot = await self.tidal.load_playlist(source_url)
        job = self._build_job(
            snapshot,
            oidc_subject=oidc_subject,
            username=username,
            source_url=source_url,
        )
        self.store.add(job)
        return job

    def _build_job(
        self,
        snapshot: SourcePlaylist,
        *,
        oidc_subject: str,
        username: str,
        source_url: str,
    ) -> ImportJob:
        job = ImportJob(
            oidc_subject=oidc_subject,
            username=username,
            source="tidal",
            source_url=source_url,
            source_playlist_id=snapshot.source_playlist_id,
            playlist_name=snapshot.name,
            description=snapshot.description,
            snapshot=snapshot.model_dump(mode="json"),
            status=ImportStatus.MATCHING,
        )
        job.tracks = [
            ImportTrack(
                position=track.position,
                tidal_track_id=track.source_track_id,
                title=track.title,
                artists=track.artists,
                album=track.album,
                duration_seconds=track.duration_seconds,
                isrc=track.isrc,
                explicit=track.explicit,
                status=TrackStatus.MISSING,
            )
            for track in snapshot.tracks
        ]
        return job

    async def preview(
        self, *, oidc_subject: str, username: str, source_url: str
    ) -> ImportJob:
        """Analyze a source without writing an import job or queueing downloads."""
        snapshot = await self.tidal.load_playlist(source_url)
        job = self._build_job(
            snapshot,
            oidc_subject=oidc_subject,
            username=username,
            source_url=source_url,
        )
        await self._refresh_track_states(job, queue_missing=False)
        job.status = ImportStatus.REVIEW
        return job

    async def persist_and_start(self, preview: ImportJob, subject: str) -> str:
        if preview.oidc_subject != subject or preview.status != ImportStatus.REVIEW:
            raise ValueError("Preview is not awaiting download confirmation")
        self.store.add(preview)
        await self.start_download(preview.id, subject)
        return preview.id

    async def get_for_user(self, import_id: str, subject: str) -> ImportJob | None:
        return self.store.get_for_user(import_id, subject)

    async def list_for_user(self, subject: str) -> list[ImportJob]:
        return self.store.list_for_user(subject)

    async def decide_partial(self, import_id: str, subject: str, create_partial: bool) -> None:
        job = self.store.get_for_user(import_id, subject)
        if not job or job.status != ImportStatus.AWAITING_DECISION:
            raise ValueError("Import is not awaiting a decision")
        job.allow_partial = create_partial
        job.status = ImportStatus.CREATING_PLAYLIST if create_partial else ImportStatus.FAILED
        if not create_partial:
            job.error = "User chose not to create a partial playlist"
        job.touch()

    async def analyze(self, import_id: str) -> None:
        """Match a playlist without submitting any new Tidarr downloads."""
        await self.advance(import_id, queue_missing=False)

    async def start_download(self, import_id: str, subject: str) -> None:
        async with self._advance_lock:
            job = self.store.get_for_user(import_id, subject)
            if not job or job.status != ImportStatus.REVIEW:
                raise ValueError("Import is not awaiting download confirmation")
            job.status = ImportStatus.DOWNLOADING
            job.touch()
            await self._advance(import_id)

    async def advance_all(self) -> None:
        if self._advance_lock.locked():
            return
        async with self._advance_lock:
            ids = self.store.ids_with_status(
                {
                    ImportStatus.MATCHING,
                    ImportStatus.DOWNLOADING,
                    ImportStatus.WAITING_FOR_NAVIDROME,
                    ImportStatus.CREATING_PLAYLIST,
                }
            )
            for import_id in ids:
                try:
                    await self._advance(import_id)
                except Exception:
                    logger.exception("Could not advance import %s", import_id)

    async def _refresh_track_states(
        self, job: ImportJob, *, queue_missing: bool
    ) -> None:
        inventory = await self.navidrome.inventory(job.username)
        files = await self.filesystem.inventory()
        queue = await self.tidarr.list_queue()
        if not queue_missing and not job.navidrome_playlist_id:
            existing_playlist = await self.navidrome.find_playlist_by_name(
                job.username, job.playlist_name
            )
            if existing_playlist:
                job.navidrome_playlist_id = existing_playlist.id

        for track in job.tracks:
            if track.status in {TrackStatus.FAILED, TrackStatus.SKIPPED}:
                continue
            match = pick_track_match(
                inventory,
                isrc=track.isrc,
                title=track.title,
                artists=track.artists,
                duration_seconds=track.duration_seconds,
            )
            if match:
                track.navidrome_song_id = match.id
                track.status = TrackStatus.PRESENT
                track.error = None
                continue

            remote = queue.get(track.tidal_track_id)
            on_disk = files.available and bool(
                (track.isrc and track.isrc in files.isrcs)
                or (not track.isrc and normalize_title(track.title) in files.misc_titles)
            )
            if on_disk:
                if not queue_missing:
                    track.status = TrackStatus.ON_DISK
                    continue
                now = datetime.now(UTC)
                if track.finished_seen_at is None:
                    track.finished_seen_at = now
                    track.status = TrackStatus.WAITING_FOR_NAVIDROME
                elif self._index_wait_expired(track.finished_seen_at, now):
                    track.status = TrackStatus.FAILED
                    track.error = "File exists, but Navidrome did not index it in time"
                else:
                    track.status = TrackStatus.WAITING_FOR_NAVIDROME
                continue
            if remote is None:
                if queue_missing:
                    await self.tidarr.queue_track(track.tidal_track_id)
                    track.status = TrackStatus.QUEUED
                else:
                    track.status = TrackStatus.MISSING
            elif remote.status in TidarrClient.ACTIVE:
                track.status = (
                    TrackStatus.QUEUED
                    if remote.status == TidarrStatus.QUEUED
                    else TrackStatus.DOWNLOADING
                )
            elif remote.status == TidarrStatus.ERROR or remote.error:
                track.status = TrackStatus.FAILED
                track.error = "Tidarr reported a download error"
            elif remote.status == TidarrStatus.FINISHED:
                if not queue_missing:
                    track.status = TrackStatus.TIDARR_FINISHED
                    continue
                if not files.available:
                    self._wait_for_navidrome_after_finished(track, datetime.now(UTC))
                    continue
                # Tidarr replaces a same-ID finished item when it is posted again.
                if not track.stale_requeue_attempted:
                    track.stale_requeue_attempted = True
                    await self.tidarr.queue_track(track.tidal_track_id)
                    track.status = TrackStatus.QUEUED
                else:
                    track.status = TrackStatus.FAILED
                    track.error = "Tidarr finished, but no ISRC file appeared in the library"

    async def advance(self, import_id: str, *, queue_missing: bool = True) -> None:
        async with self._advance_lock:
            await self._advance(import_id, queue_missing=queue_missing)

    async def _advance(self, import_id: str, *, queue_missing: bool = True) -> None:
        job = self.store.get(import_id)
        if not job:
            return
        if job.status in {
            ImportStatus.COMPLETE,
            ImportStatus.FAILED,
            ImportStatus.AWAITING_DECISION,
            ImportStatus.REVIEW,
        }:
            if queue_missing or job.status != ImportStatus.REVIEW:
                return

        await self._refresh_track_states(job, queue_missing=queue_missing)

        if not queue_missing:
            job.status = ImportStatus.REVIEW
            job.touch()
            return

        active = any(
            track.status
            in {
                TrackStatus.MISSING,
                TrackStatus.QUEUED,
                TrackStatus.DOWNLOADING,
                TrackStatus.WAITING_FOR_NAVIDROME,
            }
            for track in job.tracks
        )
        failed = [track for track in job.tracks if track.status == TrackStatus.FAILED]
        if active:
            job.status = (
                ImportStatus.WAITING_FOR_NAVIDROME
                if any(t.status == TrackStatus.WAITING_FOR_NAVIDROME for t in job.tracks)
                else ImportStatus.DOWNLOADING
            )
        elif failed and job.allow_partial is None:
            job.status = ImportStatus.AWAITING_DECISION
        elif failed and job.allow_partial is False:
            job.status = ImportStatus.FAILED
        else:
            job.status = ImportStatus.CREATING_PLAYLIST

        if job.status == ImportStatus.CREATING_PLAYLIST:
            song_ids = [
                track.navidrome_song_id
                for track in job.tracks
                if track.status == TrackStatus.PRESENT and track.navidrome_song_id
            ]
            if not song_ids:
                job.status = ImportStatus.FAILED
                job.error = "No tracks could be added to Navidrome"
            else:
                job.navidrome_playlist_id = await self.navidrome.create_or_replace_playlist(
                    job.username,
                    job.playlist_name,
                    song_ids,
                    job.navidrome_playlist_id,
                )
                job.status = ImportStatus.COMPLETE
        job.touch()

    def _index_wait_expired(self, seen_at: datetime, now: datetime) -> bool:
        if seen_at.tzinfo is None:
            seen_at = seen_at.replace(tzinfo=UTC)
        return (now - seen_at).total_seconds() >= self.settings.navidrome_index_timeout_seconds

    def _wait_for_navidrome_after_finished(
        self, track: ImportTrack, now: datetime
    ) -> None:
        if track.finished_seen_at is None:
            track.finished_seen_at = now
        elif self._index_wait_expired(track.finished_seen_at, now):
            track.status = TrackStatus.FAILED
            track.error = "Tidarr finished, but Navidrome did not index the track in time"
            return
        track.status = TrackStatus.WAITING_FOR_NAVIDROME
