import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from authlib.integrations.starlette_client import OAuth
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from .config import Settings, get_settings
from .services.filesystem import MusicFilesystem
from .services.navidrome import NavidromeClient
from .services.tidal import TidalClient, TidalNotAuthenticated
from .services.tidarr import TidarrClient
from .store import ImportJob, ImportStore
from .ui import install_ui
from .workflow import ImportWorkflow

logger = logging.getLogger(__name__)


class CreateImportRequest(BaseModel):
    url: str


class PartialDecisionRequest(BaseModel):
    create_partial: bool


class FinishTidalLoginRequest(BaseModel):
    redirect_url: str


def serialize_job(job: ImportJob) -> dict:
    return {
        "id": job.id,
        "source": job.source,
        "source_url": job.source_url,
        "playlist_name": job.playlist_name,
        "status": job.status,
        "allow_partial": job.allow_partial,
        "navidrome_playlist_id": job.navidrome_playlist_id,
        "error": job.error,
        "created_at": job.created_at.isoformat(),
        "snapshot": job.snapshot,
        "tracks": [
            {
                "position": track.position,
                "tidal_track_id": track.tidal_track_id,
                "title": track.title,
                "artists": track.artists,
                "album": track.album,
                "duration_seconds": track.duration_seconds,
                "isrc": track.isrc,
                "explicit": track.explicit,
                "status": track.status,
                "navidrome_song_id": track.navidrome_song_id,
                "error": track.error,
            }
            for track in job.tracks
        ],
    }


def current_identity(request: Request, settings: Settings) -> dict:
    if settings.proxy_auth_enabled:
        source = request.client.host if request.client else None
        if not settings.proxy_source_is_trusted(source):
            raise HTTPException(status_code=401, detail="Untrusted authentication proxy")
        username = request.headers.get(settings.proxy_auth_user_header, "").strip()
        if not username:
            raise HTTPException(status_code=401, detail="Authentication proxy did not provide a user")
        raw_groups = request.headers.get(settings.proxy_auth_groups_header, "")
        groups = [group.strip() for group in raw_groups.split(",") if group.strip()]
        return {
            "sub": f"proxy:{username}",
            "preferred_username": username,
            "groups": groups,
        }
    if settings.dev_auth_bypass:
        return {
            "sub": f"dev:{settings.dev_auth_username}",
            "preferred_username": settings.dev_auth_username,
            "groups": [settings.authelia_users_group, settings.authelia_admin_group],
        }
    identity = request.session.get("identity")
    if not identity:
        raise HTTPException(status_code=401, detail="Authentication required")
    return identity


def require_group(identity: dict, group: str) -> None:
    groups = identity.get("groups") or []
    if isinstance(groups, str):
        groups = [groups]
    if group not in groups:
        raise HTTPException(status_code=403, detail=f"Membership in {group!r} is required")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    store = ImportStore()
    tidarr = TidarrClient(settings)
    navidrome = NavidromeClient(settings)
    tidal = TidalClient(settings)
    workflow = ImportWorkflow(
        settings,
        store,
        tidal,
        tidarr,
        navidrome,
        MusicFilesystem(settings),
    )

    oauth = OAuth()
    if settings.authelia_issuer_url:
        oauth.register(
            name="authelia",
            server_metadata_url=(
                f"{settings.authelia_issuer_url.rstrip('/')}/.well-known/openid-configuration"
            ),
            client_id=settings.authelia_client_id,
            client_secret=settings.authelia_client_secret.get_secret_value(),
            client_kwargs={"scope": "openid profile email groups"},
        )

    async def worker(stop: asyncio.Event) -> None:
        while not stop.is_set():
            await workflow.advance_all()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=settings.worker_interval_seconds)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await tidal.initialize()
        stop = asyncio.Event()
        worker_task = asyncio.create_task(worker(stop), name="import-worker")
        app.state.workflow = workflow
        try:
            yield
        finally:
            stop.set()
            await worker_task
            await tidarr.close()
            await navidrome.close()

    app = FastAPI(title="NAMUAD", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key.get_secret_value(),
        same_site="lax",
        https_only=settings.base_url.startswith("https://"),
    )

    @app.get("/healthz")
    async def healthz() -> dict:
        return {
            "status": "ok",
            "filesystem_check_configured": settings.music_path is not None,
        }

    @app.get("/login")
    async def login(request: Request):
        if settings.dev_auth_bypass or settings.proxy_auth_enabled:
            return RedirectResponse("/")
        if not settings.authelia_issuer_url:
            raise HTTPException(status_code=503, detail="Authelia OIDC is not configured")
        callback = f"{settings.base_url.rstrip('/')}/auth/callback"
        return await oauth.authelia.authorize_redirect(request, callback)

    @app.get("/auth/callback")
    async def auth_callback(request: Request):
        token = await oauth.authelia.authorize_access_token(request)
        userinfo = token.get("userinfo") or {}
        username = userinfo.get("preferred_username") or userinfo.get("name")
        if not userinfo.get("sub") or not username:
            raise HTTPException(status_code=401, detail="OIDC identity is incomplete")
        identity = {
            "sub": userinfo["sub"],
            "preferred_username": username,
            "groups": userinfo.get("groups", []),
        }
        require_group(identity, settings.authelia_users_group)
        request.session["identity"] = identity
        return RedirectResponse("/")

    @app.post("/logout")
    async def logout(request: Request):
        request.session.clear()
        return {"ok": True}

    @app.get("/api/imports")
    async def list_imports(request: Request):
        identity = current_identity(request, settings)
        jobs = await workflow.list_for_user(identity["sub"])
        return [serialize_job(job) for job in jobs]

    @app.post("/api/imports", status_code=201)
    async def create_import(body: CreateImportRequest, request: Request):
        identity = current_identity(request, settings)
        require_group(identity, settings.authelia_users_group)
        try:
            job = await workflow.create(
                oidc_subject=identity["sub"],
                username=identity["preferred_username"],
                source_url=body.url,
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await workflow.analyze(job.id)
        loaded = await workflow.get_for_user(job.id, identity["sub"])
        return serialize_job(loaded)

    @app.get("/api/imports/{import_id}")
    async def get_import(import_id: str, request: Request):
        identity = current_identity(request, settings)
        job = await workflow.get_for_user(import_id, identity["sub"])
        if not job:
            raise HTTPException(status_code=404, detail="Import not found")
        return serialize_job(job)

    @app.post("/api/imports/{import_id}/partial-decision")
    async def partial_decision(import_id: str, body: PartialDecisionRequest, request: Request):
        identity = current_identity(request, settings)
        try:
            await workflow.decide_partial(import_id, identity["sub"], body.create_partial)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if body.create_partial:
            await workflow.advance(import_id)
        job = await workflow.get_for_user(import_id, identity["sub"])
        return serialize_job(job)

    @app.post("/api/imports/{import_id}/start")
    async def start_download(import_id: str, request: Request):
        identity = current_identity(request, settings)
        try:
            await workflow.start_download(import_id, identity["sub"])
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        job = await workflow.get_for_user(import_id, identity["sub"])
        return serialize_job(job)

    @app.get("/api/admin/tidal/status")
    async def tidal_status(request: Request):
        identity = current_identity(request, settings)
        require_group(identity, settings.authelia_admin_group)
        account = await tidal.account()
        return {"authenticated": account is not None, "account": account}

    @app.get("/api/tidal/playlists")
    async def tidal_playlists(request: Request):
        identity = current_identity(request, settings)
        require_group(identity, settings.authelia_users_group)
        try:
            playlists = await tidal.list_playlists()
        except TidalNotAuthenticated as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return [playlist.model_dump(mode="json") for playlist in playlists]

    @app.post("/api/admin/tidal/login")
    async def tidal_login(request: Request):
        identity = current_identity(request, settings)
        require_group(identity, settings.authelia_admin_group)
        return {"login_url": await tidal.begin_login()}

    @app.post("/api/admin/tidal/login/complete")
    async def complete_tidal_login(body: FinishTidalLoginRequest, request: Request):
        identity = current_identity(request, settings)
        require_group(identity, settings.authelia_admin_group)
        await tidal.finish_login(body.redirect_url)
        return {"authenticated": True}

    @app.delete("/api/admin/tidal/session")
    async def tidal_logout(request: Request):
        identity = current_identity(request, settings)
        require_group(identity, settings.authelia_admin_group)
        await tidal.logout()
        return {"authenticated": False}

    install_ui(app, workflow, tidal, settings, current_identity)

    return app
