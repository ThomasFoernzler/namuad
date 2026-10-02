from enum import StrEnum

import httpx
from pydantic import BaseModel, Field, field_validator
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from ..config import Settings


class TidarrStatus(StrEnum):
    QUEUED = "queue_download"
    DOWNLOADING = "download"
    QUEUED_PROCESSING = "queue_processing"
    PROCESSING = "processing"
    FINISHED = "finished"
    ERROR = "error"


class TidarrItem(BaseModel):
    id: str
    type: str = "track"
    status: str
    title: str | None = None
    artist: str | None = None
    error: bool = False

    @field_validator("id", mode="before")
    @classmethod
    def normalize_id(cls, value: str | int) -> str:
        """Tidarr emits IDs as both JSON strings and numbers."""
        return str(value)


class TidarrQueue(BaseModel):
    total: int = 0
    queue: list[TidarrItem] = Field(default_factory=list)


class TidarrClient:
    """Small client around Tidarr's stable automation API.

    Tidarr replaces an existing queue item when POST /api/save receives the same
    ID. Callers must therefore never post an ID that is already present, including
    finished and failed items. Retrying a download is an explicit user action.
    """

    ACTIVE = {
        TidarrStatus.QUEUED,
        TidarrStatus.DOWNLOADING,
        TidarrStatus.QUEUED_PROCESSING,
        TidarrStatus.PROCESSING,
    }

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.tidarr_url.rstrip("/"),
            timeout=settings.provider_timeout_seconds,
            transport=transport,
        )

    def _auth_headers(self) -> dict[str, str]:
        key = self.settings.tidarr_api_key.get_secret_value().strip()
        if self.settings.tidarr_api_key_file:
            try:
                file_key = self.settings.tidarr_api_key_file.read_text().strip()
            except OSError:
                file_key = ""
            key = file_key or key
        return {"X-Api-Key": key} if key else {}

    @retry(
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=0.5, max=5),
        reraise=True,
    )
    async def list_queue(self) -> dict[str, TidarrItem]:
        response = await self._client.get("/api/queue/list", headers=self._auth_headers())
        response.raise_for_status()
        queue = TidarrQueue.model_validate(response.json())
        return {item.id: item for item in queue.queue}

    async def ensure_track_queued(self, tidal_track_id: str) -> TidarrItem | None:
        existing = (await self.list_queue()).get(str(tidal_track_id))
        if existing:
            return existing

        await self.queue_track(tidal_track_id)
        return None

    async def queue_track(self, tidal_track_id: str) -> None:
        response = await self._client.post(
            "/api/save",
            headers=self._auth_headers(),
            json={
                "item": {
                    "id": str(tidal_track_id),
                    "url": f"https://listen.tidal.com/track/{tidal_track_id}",
                    "type": "track",
                    "status": TidarrStatus.QUEUED,
                }
            },
        )
        response.raise_for_status()

    async def close(self) -> None:
        await self._client.aclose()
