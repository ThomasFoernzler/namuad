from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from .domain import ImportStatus, TrackStatus


def utcnow() -> datetime:
    return datetime.now(UTC)


class ImportTrack(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    position: int
    tidal_track_id: str
    title: str
    artists: list[str]
    album: str | None = None
    duration_seconds: int | None = None
    isrc: str | None = None
    explicit: bool = False
    status: TrackStatus = TrackStatus.MISSING
    navidrome_song_id: str | None = None
    error: str | None = None
    finished_seen_at: datetime | None = None
    # True after NAMUAD submitted this track or observed it in Tidarr. Once set,
    # a missing queue entry is a failure; automatic resubmission is forbidden.
    tidarr_started: bool = False


class ImportJob(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    oidc_subject: str
    username: str
    source: str = "tidal"
    source_url: str
    source_playlist_id: str
    playlist_name: str
    description: str | None = None
    snapshot: dict[str, Any]
    status: ImportStatus = ImportStatus.NEW
    allow_partial: bool | None = None
    navidrome_playlist_id: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    tracks: list[ImportTrack] = Field(default_factory=list)

    def touch(self) -> None:
        self.updated_at = utcnow()


class ImportStore:
    """Process-local import state; intentionally empty after every restart."""

    def __init__(self) -> None:
        self._jobs: dict[str, ImportJob] = {}

    def add(self, job: ImportJob) -> None:
        self._jobs[job.id] = job

    def get(self, import_id: str) -> ImportJob | None:
        return self._jobs.get(import_id)

    def get_for_user(self, import_id: str, subject: str) -> ImportJob | None:
        job = self.get(import_id)
        return job if job and job.oidc_subject == subject else None

    def list_for_user(self, subject: str) -> list[ImportJob]:
        return sorted(
            (job for job in self._jobs.values() if job.oidc_subject == subject),
            key=lambda job: job.created_at,
            reverse=True,
        )

    def ids_with_status(self, statuses: set[ImportStatus]) -> list[str]:
        return [job.id for job in self._jobs.values() if job.status in statuses]
