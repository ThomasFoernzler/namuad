import re
import unicodedata
from enum import StrEnum

from pydantic import BaseModel, Field

ISRC_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{7}$")


def normalize_isrc(value: str | None) -> str | None:
    if not value:
        return None
    normalized = re.sub(r"[^A-Za-z0-9]", "", value).upper()
    return normalized if ISRC_RE.fullmatch(normalized) else None


def normalize_title(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", value).split())


class ImportStatus(StrEnum):
    NEW = "new"
    MATCHING = "matching"
    REVIEW = "review"
    DOWNLOADING = "downloading"
    WAITING_FOR_NAVIDROME = "waiting_for_navidrome"
    AWAITING_DECISION = "awaiting_decision"
    CREATING_PLAYLIST = "creating_playlist"
    COMPLETE = "complete"
    FAILED = "failed"


class TrackStatus(StrEnum):
    PRESENT = "present"
    ON_DISK = "on_disk"
    TIDARR_FINISHED = "tidarr_finished"
    MISSING = "missing"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    WAITING_FOR_NAVIDROME = "waiting_for_navidrome"
    FAILED = "failed"
    SKIPPED = "skipped"


class SourceTrack(BaseModel):
    position: int
    source_track_id: str
    source_url: str
    title: str
    artists: list[str] = Field(default_factory=list)
    album: str | None = None
    duration_seconds: int | None = None
    isrc: str | None = None
    explicit: bool = False
    available: bool = True

    def model_post_init(self, __context: object) -> None:
        self.isrc = normalize_isrc(self.isrc)


class SourcePlaylist(BaseModel):
    source: str = "tidal"
    source_playlist_id: str
    source_url: str
    name: str
    description: str | None = None
    tracks: list[SourceTrack]
    skipped_items: int = 0


class TidalPlaylistSummary(BaseModel):
    id: str
    name: str
    description: str | None = None
    track_count: int = 0
    public: bool = False
    url: str
    image_url: str | None = None


class NavidromeSong(BaseModel):
    id: str
    title: str
    artist: str = ""
    album: str | None = None
    duration: int | None = None
    isrc: list[str] = Field(default_factory=list)
    path: str | None = None

    @classmethod
    def from_api(cls, item: dict) -> "NavidromeSong":
        raw_isrc = item.get("isrc") or []
        if isinstance(raw_isrc, str):
            raw_isrc = [raw_isrc]
        return cls(
            id=str(item["id"]),
            title=item.get("title") or item.get("name") or "Unknown",
            artist=item.get("artist") or "",
            album=item.get("album"),
            duration=item.get("duration"),
            isrc=[value for value in (normalize_isrc(v) for v in raw_isrc) if value],
            path=item.get("path"),
        )


class NavidromePlaylistSummary(BaseModel):
    id: str
    name: str
    owner: str | None = None
    song_count: int = 0
