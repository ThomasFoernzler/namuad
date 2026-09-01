from navidrome_music_adder.domain import ImportStatus
from navidrome_music_adder.store import ImportJob, ImportStore


def make_job(subject: str, name: str) -> ImportJob:
    return ImportJob(
        oidc_subject=subject,
        username=subject,
        source_url="https://tidal.com/playlist/test",
        source_playlist_id=name,
        playlist_name=name,
        snapshot={},
        status=ImportStatus.REVIEW,
    )


def test_store_keeps_jobs_in_memory_and_separates_users() -> None:
    store = ImportStore()
    alice = make_job("alice", "Alice playlist")
    bob = make_job("bob", "Bob playlist")

    store.add(alice)
    store.add(bob)

    assert store.get_for_user(alice.id, "alice") is alice
    assert store.get_for_user(alice.id, "bob") is None
    assert store.list_for_user("alice") == [alice]
    assert store.ids_with_status({ImportStatus.REVIEW}) == [alice.id, bob.id]
