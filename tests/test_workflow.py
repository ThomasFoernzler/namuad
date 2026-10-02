from datetime import UTC, datetime, timedelta

import pytest

from navidrome_music_adder.config import Settings
from navidrome_music_adder.domain import (
    ImportStatus,
    SourcePlaylist,
    SourceTrack,
    TrackStatus,
)
from navidrome_music_adder.services.filesystem import FileInventory
from navidrome_music_adder.services.navidrome import LibraryInventory
from navidrome_music_adder.services.tidarr import TidarrItem, TidarrStatus
from navidrome_music_adder.store import ImportStore, ImportTrack
from navidrome_music_adder.workflow import ImportWorkflow


class StubNavidrome:
    async def inventory(self, username: str) -> LibraryInventory:
        return LibraryInventory()

    async def find_playlist_by_name(self, username: str, name: str):
        return None


class StubFilesystem:
    def __init__(self, available: bool = True):
        self.available = available

    async def inventory(self) -> FileInventory:
        return FileInventory(self.available, set(), set())


class StubTidarr:
    def __init__(self, queue: dict[str, TidarrItem] | None = None):
        self.queue = queue or {}
        self.queued: list[str] = []

    async def list_queue(self) -> dict[str, TidarrItem]:
        return self.queue

    async def queue_track(self, track_id: str) -> None:
        self.queued.append(track_id)


def build_workflow(tidarr: StubTidarr, *, files_available: bool = True) -> ImportWorkflow:
    return ImportWorkflow(
        Settings(),
        store=ImportStore(),
        tidal=None,  # type: ignore[arg-type]
        tidarr=tidarr,  # type: ignore[arg-type]
        navidrome=StubNavidrome(),  # type: ignore[arg-type]
        filesystem=StubFilesystem(files_available),  # type: ignore[arg-type]
    )


def build_job(workflow: ImportWorkflow, track_ids: list[str]):
    snapshot = SourcePlaylist(
        source_playlist_id="playlist-1",
        source_url="https://listen.tidal.com/playlist/playlist-1",
        name="Test playlist",
        tracks=[
            SourceTrack(
                position=position,
                source_track_id=track_id,
                source_url=f"https://listen.tidal.com/track/{track_id}",
                title=f"Track {track_id}",
                artists=["Artist"],
                isrc=f"ISRC{track_id}",
            )
            for position, track_id in enumerate(track_ids)
        ],
    )
    return workflow._build_job(
        snapshot,
        oidc_subject="proxy:alice",
        username="alice",
        source_url=snapshot.source_url,
    )


@pytest.mark.asyncio
async def test_preview_is_not_persisted_or_queued_before_confirmation() -> None:
    class Tidal:
        async def load_playlist(self, source_url: str) -> SourcePlaylist:
            return SourcePlaylist(
                source_playlist_id="playlist-1",
                source_url=source_url,
                name="Preview",
                tracks=[
                    SourceTrack(
                        position=0,
                        source_track_id="42",
                        source_url="https://listen.tidal.com/track/42",
                        title="Missing song",
                        artists=["Artist"],
                        isrc="USRC17607839",
                    )
                ],
            )

    class Tidarr:
        queued: list[str] = []

        async def list_queue(self) -> dict:
            return {}

        async def queue_track(self, track_id: str) -> None:
            self.queued.append(track_id)

    class Navidrome:
        async def inventory(self, username: str) -> LibraryInventory:
            return LibraryInventory()

        async def find_playlist_by_name(self, username: str, name: str):
            return None

    class Filesystem:
        async def inventory(self) -> FileInventory:
            return FileInventory(False, set(), set())

    store = ImportStore()
    tidarr = Tidarr()
    workflow = ImportWorkflow(
        Settings(),
        store=store,
        tidal=Tidal(),  # type: ignore[arg-type]
        tidarr=tidarr,  # type: ignore[arg-type]
        navidrome=Navidrome(),  # type: ignore[arg-type]
        filesystem=Filesystem(),  # type: ignore[arg-type]
    )

    job = await workflow.preview(
        oidc_subject="proxy:alice",
        username="alice",
        source_url="https://listen.tidal.com/playlist/playlist-1",
    )

    assert job.status == ImportStatus.REVIEW
    assert job.tracks[0].status == TrackStatus.MISSING
    assert tidarr.queued == []
    assert store.get(job.id) is None

    await workflow.persist_and_start(job, "proxy:alice")

    assert store.get(job.id) is job
    assert job.status == ImportStatus.DOWNLOADING
    assert job.tracks[0].status == TrackStatus.QUEUED
    assert tidarr.queued == ["42"]


def test_api_only_finished_item_waits_without_requeueing() -> None:
    workflow = ImportWorkflow(
        Settings(music_path=None, navidrome_index_timeout_seconds=10),
        store=ImportStore(),
        tidal=None,  # type: ignore[arg-type]
        tidarr=None,  # type: ignore[arg-type]
        navidrome=None,  # type: ignore[arg-type]
        filesystem=None,  # type: ignore[arg-type]
    )
    track = ImportTrack(
        position=0,
        tidal_track_id="42",
        title="Missing song",
        artists=["Artist"],
        isrc="USRC17607839",
        status=TrackStatus.DOWNLOADING,
    )
    first_seen = datetime.now(UTC)

    workflow._wait_for_navidrome_after_finished(track, first_seen)

    assert track.status == TrackStatus.WAITING_FOR_NAVIDROME
    assert track.finished_seen_at == first_seen
    assert track.tidarr_started is False

    workflow._wait_for_navidrome_after_finished(track, first_seen + timedelta(seconds=11))

    assert track.status == TrackStatus.FAILED
    assert track.error is not None
    assert "Navidrome did not index" in track.error


@pytest.mark.asyncio
async def test_finished_item_without_file_is_not_requeued() -> None:
    tidarr = StubTidarr(
        {
            "42": TidarrItem(
                id="42",
                status=TidarrStatus.FINISHED,
            )
        }
    )
    workflow = build_workflow(tidarr)
    job = build_job(workflow, ["42"])

    await workflow._refresh_track_states(job, queue_missing=True)

    assert tidarr.queued == []
    assert job.tracks[0].tidarr_started is True
    assert job.tracks[0].status == TrackStatus.FAILED
    assert "no matching file" in (job.tracks[0].error or "")


@pytest.mark.asyncio
async def test_disappeared_item_is_not_submitted_again() -> None:
    tidarr = StubTidarr()
    workflow = build_workflow(tidarr)
    job = build_job(workflow, ["42"])
    job.tracks[0].tidarr_started = True
    job.tracks[0].status = TrackStatus.DOWNLOADING

    await workflow._refresh_track_states(job, queue_missing=True)

    assert tidarr.queued == []
    assert job.tracks[0].status == TrackStatus.FAILED
    assert "disappeared" in (job.tracks[0].error or "")


@pytest.mark.asyncio
async def test_duplicate_playlist_track_is_submitted_once() -> None:
    tidarr = StubTidarr()
    workflow = build_workflow(tidarr)
    job = build_job(workflow, ["42", "42"])

    await workflow._refresh_track_states(job, queue_missing=True)

    assert tidarr.queued == ["42"]
    assert all(track.tidarr_started for track in job.tracks)
    assert all(track.status == TrackStatus.QUEUED for track in job.tracks)
