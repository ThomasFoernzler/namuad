import httpx
import pytest
from pydantic import SecretStr

from navidrome_music_adder.config import Settings
from navidrome_music_adder.services.tidarr import TidarrClient


@pytest.mark.asyncio
async def test_normalizes_numeric_queue_item_id() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "total": 1,
                "queue": [{"id": 131356320, "type": "track", "status": "finished"}],
            },
        )

    client = TidarrClient(Settings(), transport=httpx.MockTransport(handler))
    queue = await client.list_queue()
    await client.close()

    assert list(queue) == ["131356320"]
    assert queue["131356320"].id == "131356320"


@pytest.mark.asyncio
async def test_does_not_replace_active_item() -> None:
    posts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "total": 1,
                    "queue": [{"id": "42", "type": "track", "status": "download"}],
                },
            )
        posts += 1
        return httpx.Response(201)

    client = TidarrClient(
        Settings(tidarr_api_key=SecretStr("key")), transport=httpx.MockTransport(handler)
    )
    item = await client.ensure_track_queued("42")
    await client.close()
    assert item is not None
    assert posts == 0


@pytest.mark.asyncio
async def test_does_not_replace_finished_item() -> None:
    posts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "total": 1,
                    "queue": [{"id": "42", "type": "track", "status": "finished"}],
                },
            )
        posts += 1
        return httpx.Response(201)

    client = TidarrClient(
        Settings(tidarr_api_key=SecretStr("key")), transport=httpx.MockTransport(handler)
    )
    item = await client.ensure_track_queued("42")
    await client.close()
    assert item is not None
    assert item.status == "finished"
    assert posts == 0


@pytest.mark.asyncio
async def test_reads_generated_api_key_file(tmp_path) -> None:
    key_file = tmp_path / ".tidarr-api-key"
    key_file.write_text("generated-key\n")

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Api-Key"] == "generated-key"
        return httpx.Response(200, json={"total": 0, "queue": []})

    client = TidarrClient(
        Settings(tidarr_api_key_file=key_file), transport=httpx.MockTransport(handler)
    )
    await client.list_queue()
    await client.close()
