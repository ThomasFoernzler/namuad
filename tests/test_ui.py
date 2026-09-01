from types import SimpleNamespace

from navidrome_music_adder.domain import ImportStatus, TrackStatus
from navidrome_music_adder.ui import imports_need_polling, imports_signature


def make_job(status: ImportStatus, track_status: TrackStatus):
    track = SimpleNamespace(
        id=1,
        position=0,
        title="Song",
        artists=["Artist"],
        album="Album",
        isrc="USRC17607839",
        explicit=False,
        status=track_status,
        navidrome_song_id=None,
        error=None,
    )
    return SimpleNamespace(
        id="import-1",
        playlist_name="Playlist",
        status=status,
        allow_partial=None,
        navidrome_playlist_id=None,
        error=None,
        snapshot={"skipped_items": 0},
        tracks=[track],
    )


def test_complete_imports_do_not_need_polling() -> None:
    jobs = [make_job(ImportStatus.COMPLETE, TrackStatus.PRESENT)]

    assert not imports_need_polling(jobs)  # type: ignore[arg-type]


def test_displayed_track_change_updates_signature() -> None:
    job = make_job(ImportStatus.DOWNLOADING, TrackStatus.QUEUED)
    before = imports_signature([job])  # type: ignore[list-item]

    job.tracks[0].status = TrackStatus.DOWNLOADING

    assert imports_signature([job]) != before  # type: ignore[list-item]
