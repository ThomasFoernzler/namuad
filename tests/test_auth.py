from starlette.requests import Request

from navidrome_music_adder.app import current_identity
from navidrome_music_adder.config import Settings


def request_from(host: str, headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
            "client": (host, 12345),
        }
    )


def test_proxy_auth_uses_headers_from_trusted_source() -> None:
    settings = Settings(
        proxy_auth_enabled=True,
        proxy_auth_trusted_sources="10.89.91.2/32",
    )
    request = request_from(
        "10.89.91.2",
        {"Remote-User": "music-test", "Remote-Groups": "users,admins"},
    )

    assert current_identity(request, settings) == {
        "sub": "proxy:music-test",
        "preferred_username": "music-test",
        "groups": ["users", "admins"],
    }


def test_proxy_auth_rejects_untrusted_source() -> None:
    settings = Settings(
        proxy_auth_enabled=True,
        proxy_auth_trusted_sources="10.89.91.2/32",
    )
    request = request_from("10.89.91.99", {"Remote-User": "admin"})

    try:
        current_identity(request, settings)
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 401
    else:
        raise AssertionError("untrusted proxy was accepted")
