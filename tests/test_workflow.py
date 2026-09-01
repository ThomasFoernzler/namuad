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
from navidrome_music_adder.store import ImportStore, ImportTrack
from navidrome_music_adder.workflow import ImportWorkflow


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
    assert track.stale_requeue_attempted is False

    workflow._wait_for_navidrome_after_finished(track, first_seen + timedelta(seconds=11))

    assert track.status == TrackStatus.FAILED
    assert track.error is not None
    assert "Navidrome did not index" in track.error
