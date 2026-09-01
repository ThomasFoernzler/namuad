import asyncio
from collections import Counter
from collections.abc import Callable

from fastapi import FastAPI, HTTPException, Request
from nicegui import ui

from .config import Settings
from .domain import ImportStatus, TidalPlaylistSummary, TrackStatus
from .services.tidal import TidalClient, TidalNotAuthenticated
from .store import ImportJob
from .workflow import ImportWorkflow

TRACK_LABELS = {
    TrackStatus.PRESENT: "In Navidrome",
    TrackStatus.ON_DISK: "On disk",
    TrackStatus.TIDARR_FINISHED: "Finished in Tidarr",
    TrackStatus.MISSING: "Missing",
    TrackStatus.QUEUED: "Queued",
    TrackStatus.DOWNLOADING: "Downloading",
    TrackStatus.WAITING_FOR_NAVIDROME: "Waiting for Navidrome",
    TrackStatus.FAILED: "Failed",
    TrackStatus.SKIPPED: "Skipped",
}

TRACK_COLUMNS = [
    {"name": "position", "label": "#", "field": "position", "sortable": True},
    {"name": "title", "label": "Title", "field": "title", "sortable": True},
    {"name": "artist", "label": "Artist", "field": "artist", "sortable": True},
    {"name": "album", "label": "Album", "field": "album", "sortable": True},
    {"name": "isrc", "label": "ISRC", "field": "isrc", "sortable": True},
    {"name": "explicit", "label": "Explicit", "field": "explicit"},
    {"name": "status", "label": "Status", "field": "status", "sortable": True},
    {"name": "note", "label": "Note", "field": "note"},
]

ACTIVE_IMPORT_STATUSES = {
    ImportStatus.NEW,
    ImportStatus.MATCHING,
    ImportStatus.DOWNLOADING,
    ImportStatus.WAITING_FOR_NAVIDROME,
    ImportStatus.CREATING_PLAYLIST,
}


def imports_signature(jobs: list[ImportJob]) -> tuple:
    """Return only state which can change what the import list displays."""
    return tuple(
        (
            job.id,
            job.playlist_name,
            str(job.status),
            job.allow_partial,
            job.navidrome_playlist_id,
            job.error,
            int((job.snapshot or {}).get("skipped_items", 0)),
            tuple(
                (
                    track.id,
                    track.position,
                    track.title,
                    tuple(track.artists),
                    track.album,
                    track.isrc,
                    track.explicit,
                    str(track.status),
                    track.navidrome_song_id,
                    track.error,
                )
                for track in job.tracks
            ),
        )
        for job in jobs
    )


def imports_need_polling(jobs: list[ImportJob]) -> bool:
    return any(job.status in ACTIVE_IMPORT_STATUSES for job in jobs)


def track_rows(job: ImportJob) -> list[dict]:
    return [
        {
            "position": track.position + 1,
            "title": track.title,
            "artist": ", ".join(track.artists),
            "album": track.album or "",
            "isrc": track.isrc or "—",
            "explicit": "Yes" if track.explicit else "",
            "status": TRACK_LABELS.get(track.status, str(track.status)),
            "note": track.error or "",
        }
        for track in job.tracks
    ]


def page_identity(
    request: Request,
    settings: Settings,
    identity_resolver: Callable[[Request, Settings], dict],
) -> dict | None:
    try:
        identity = identity_resolver(request, settings)
    except HTTPException:
        ui.navigate.to("/login")
        return None
    groups = identity.get("groups") or []
    if isinstance(groups, str):
        groups = [groups]
    if settings.authelia_users_group not in groups:
        raise HTTPException(
            status_code=403,
            detail=f"Membership in {settings.authelia_users_group!r} is required",
        )
    return identity


def render_header(username: str, *, show_tidal: bool) -> None:
    with ui.header().classes("items-center px-6"):
        ui.icon("library_music").classes("text-2xl")
        ui.label("Navidrome Music Adder").classes("text-xl font-semibold")
        ui.space()
        ui.button("Imports", icon="home", on_click=lambda: ui.navigate.to("/")).props("flat")
        if show_tidal:
            ui.button(
                "TIDAL account",
                icon="account_circle",
                on_click=lambda: ui.navigate.to("/tidal"),
            ).props("flat")
        ui.label(username).classes("text-sm text-grey-4 ml-2")


def render_tidal_playlist(
    playlist: TidalPlaylistSummary,
    analyze_source: Callable[[str], object],
    navidrome_playlist_id: str | None = None,
) -> None:
    with ui.card().classes("w-full p-3"):
        with ui.row().classes("w-full items-center gap-4 no-wrap"):
            if playlist.image_url:
                ui.image(playlist.image_url).classes("w-16 h-16 rounded object-cover shrink-0")
            else:
                ui.icon("queue_music").classes("text-4xl text-grey-6 w-16")
            with ui.column().classes("gap-0 min-w-0"):
                ui.label(playlist.name).classes("font-semibold text-base")
                ui.label(f"{playlist.track_count} tracks").classes("text-sm text-grey-5")
                if playlist.description:
                    ui.label(playlist.description).classes("text-sm text-grey-6 line-clamp-1")
            ui.space()
            if navidrome_playlist_id:
                ui.badge("In Navidrome", color="positive")
            ui.link("TIDAL", playlist.url, new_tab=True).classes("text-sm")
            ui.button(
                "Review update" if navidrome_playlist_id else "Analyze",
                icon="search",
                on_click=lambda url=playlist.url: analyze_source(url),
            ).props("unelevated dense")


def install_ui(
    app: FastAPI,
    workflow: ImportWorkflow,
    tidal: TidalClient,
    settings: Settings,
    identity_resolver: Callable[[Request, Settings], dict],
) -> None:
    @ui.page("/")
    async def index(request: Request) -> None:
        identity = page_identity(request, settings, identity_resolver)
        if identity is None:
            return

        groups = identity.get("groups") or []
        subject = str(identity["sub"])
        username = str(identity["preferred_username"])
        is_admin = settings.authelia_admin_group in groups
        rendered_signature: tuple | None = None
        playlists_loaded = False

        ui.page_title("Navidrome Music Adder")
        ui.dark_mode().enable()
        ui.colors(primary="#7c3aed", secondary="#06b6d4", accent="#f59e0b")
        render_header(username, show_tidal=is_admin)

        def show_preview(preview: ImportJob) -> None:
            counts = Counter(str(track.status) for track in preview.tracks)
            dialog = ui.dialog()
            with dialog, ui.card().classes("w-[min(1100px,95vw)] max-w-none p-5 gap-4"):
                with ui.row().classes("w-full items-start"):
                    with ui.column().classes("gap-1"):
                        ui.label(preview.playlist_name).classes("text-2xl font-semibold")
                        ui.label(f"{len(preview.tracks)} importable tracks").classes(
                            "text-sm text-grey-5"
                        )
                    ui.space()
                    if preview.navidrome_playlist_id:
                        ui.badge("Will update existing playlist", color="positive")

                with ui.row().classes("w-full gap-2"):
                    ui.badge(f"{counts[TrackStatus.PRESENT]} in Navidrome", color="positive")
                    if counts[TrackStatus.ON_DISK]:
                        ui.badge(f"{counts[TrackStatus.ON_DISK]} on disk", color="cyan")
                    ui.badge(f"{counts[TrackStatus.MISSING]} missing", color="warning")
                    if counts[TrackStatus.TIDARR_FINISHED]:
                        ui.badge(
                            f"{counts[TrackStatus.TIDARR_FINISHED]} finished in Tidarr",
                            color="secondary",
                        )
                    skipped = int((preview.snapshot or {}).get("skipped_items", 0))
                    if skipped:
                        ui.badge(f"{skipped} skipped", color="grey")

                ui.table(
                    columns=TRACK_COLUMNS,
                    rows=track_rows(preview),
                    row_key="position",
                    pagination={"rowsPerPage": 25},
                ).classes("w-full max-h-[55vh]").props("flat bordered dense wrap-cells")

                async def confirm_download() -> None:
                    download_button.disable()
                    try:
                        await workflow.persist_and_start(preview, subject)
                    except Exception as exc:
                        ui.notify(str(exc), type="negative", close_button="Dismiss")
                        download_button.enable()
                        return
                    dialog.close()
                    ui.notify("Import started", type="positive")
                    await update_imports(force=True)

                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("Cancel", on_click=dialog.close).props("flat")
                    download_button = ui.button(
                        "Download",
                        icon="download",
                        on_click=confirm_download,
                    ).props("unelevated")
            dialog.open()

        async def analyze_source(source_url: str) -> None:
            notice = ui.notification("Analyzing TIDAL source…", spinner=True, timeout=None)
            try:
                preview = await workflow.preview(
                    oidc_subject=subject,
                    username=username,
                    source_url=source_url,
                )
                show_preview(preview)
            except Exception as exc:
                ui.notify(str(exc), type="negative", close_button="Dismiss")
            finally:
                notice.dismiss()

        @ui.refreshable
        def tidal_playlist_browser(
            playlists: list[TidalPlaylistSummary] | None = None,
            *,
            navidrome_by_name: dict[str, str] | None = None,
            error: str | None = None,
            loading: bool = False,
            login_required: bool = False,
        ) -> None:
            if loading:
                with ui.row().classes("w-full items-center justify-center p-8 gap-3"):
                    ui.spinner(size="lg")
                    ui.label("Loading TIDAL playlists…")
                return
            if login_required:
                with ui.column().classes("w-full items-center p-8 gap-3"):
                    ui.icon("link_off").classes("text-5xl text-grey-6")
                    ui.label("No TIDAL account is connected").classes("text-lg")
                    if is_admin:
                        ui.button(
                            "Open TIDAL login",
                            icon="login",
                            on_click=lambda: ui.navigate.to("/tidal"),
                        )
                return
            if error:
                ui.label(f"Could not load TIDAL playlists: {error}").classes("text-negative")
                return
            if playlists is None:
                ui.label("Open this tab to load playlists from TIDAL.").classes(
                    "text-grey-5 p-6"
                )
                return
            if not playlists:
                ui.label("The connected TIDAL account has no playlists.").classes("p-6")
                return

            navidrome_by_name = navidrome_by_name or {}
            synced = [playlist for playlist in playlists if playlist.name in navidrome_by_name]
            available = [
                playlist for playlist in playlists if playlist.name not in navidrome_by_name
            ]
            with ui.scroll_area().classes("w-full h-80"):
                with ui.column().classes("w-full gap-2 pr-3"):
                    ui.label("Already in Navidrome").classes("text-lg font-semibold")
                    if not synced:
                        ui.label("No matching playlists yet.").classes("text-sm text-grey-5 p-2")
                    for playlist in synced:
                        render_tidal_playlist(
                            playlist,
                            analyze_source,
                            navidrome_by_name[playlist.name],
                        )
                    ui.separator().classes("my-3")
                    ui.label("Available to analyze").classes("text-lg font-semibold")
                    if not available:
                        ui.label("All playlists already exist in Navidrome.").classes(
                            "text-sm text-grey-5 p-2"
                        )
                    for playlist in available:
                        render_tidal_playlist(playlist, analyze_source)

        async def load_tidal_playlists() -> None:
            nonlocal playlists_loaded
            await tidal_playlist_browser.refresh(
                None,
                navidrome_by_name=None,
                error=None,
                loading=True,
                login_required=False,
            )
            try:
                playlists, navidrome_playlists = await asyncio.gather(
                    tidal.list_playlists(),
                    workflow.navidrome.list_playlists(username),
                )
            except TidalNotAuthenticated:
                playlists_loaded = True
                await tidal_playlist_browser.refresh(
                    None,
                    navidrome_by_name=None,
                    error=None,
                    loading=False,
                    login_required=True,
                )
                return
            except Exception as exc:
                await tidal_playlist_browser.refresh(
                    None,
                    navidrome_by_name=None,
                    error=str(exc),
                    loading=False,
                    login_required=False,
                )
                return
            playlists_loaded = True
            navidrome_by_name = {playlist.name: playlist.id for playlist in navidrome_playlists}
            await tidal_playlist_browser.refresh(
                playlists,
                navidrome_by_name=navidrome_by_name,
                error=None,
                loading=False,
                login_required=False,
            )

        async def add_tab_changed(event) -> None:
            if event.value == "playlists" and not playlists_loaded:
                await load_tidal_playlists()

        @ui.refreshable
        def imports(jobs: list[ImportJob]) -> None:
            completed_jobs = [
                job for job in jobs if job.status in {ImportStatus.COMPLETE, ImportStatus.FAILED}
            ]
            current_jobs = [job for job in jobs if job not in completed_jobs]

            ui.label("Current imports").classes("text-2xl font-semibold")
            if not current_jobs:
                with ui.card().classes("w-full p-8 items-center"):
                    ui.icon("hourglass_empty").classes("text-4xl text-grey-6")
                    ui.label("No imports in progress").classes("text-lg text-grey-5")

            for index, job in enumerate(current_jobs + completed_jobs):
                if index == len(current_jobs):
                    ui.separator().classes("my-4")
                    ui.label("Completed jobs").classes("text-2xl font-semibold")
                counts = Counter(str(track.status) for track in job.tracks)
                with ui.card().classes("w-full p-5 gap-4"):
                    with ui.row().classes("w-full items-start"):
                        with ui.column().classes("gap-1"):
                            ui.label(job.playlist_name).classes("text-xl font-semibold")
                            ui.label(f"Import status: {job.status}").classes("text-sm text-grey-5")
                        ui.space()
                        ui.badge(f"{len(job.tracks)} tracks", color="secondary")
                        if job.navidrome_playlist_id and job.status == ImportStatus.REVIEW:
                            ui.badge("Will update existing playlist", color="positive")
                        skipped = int((job.snapshot or {}).get("skipped_items", 0))
                        if skipped:
                            ui.badge(f"{skipped} skipped", color="grey")

                    with ui.row().classes("w-full gap-2"):
                        ui.badge(f"{counts[TrackStatus.PRESENT]} in Navidrome", color="positive")
                        if counts[TrackStatus.ON_DISK]:
                            ui.badge(f"{counts[TrackStatus.ON_DISK]} on disk", color="cyan")
                        ui.badge(f"{counts[TrackStatus.MISSING]} missing", color="warning")
                        active = (
                            counts[TrackStatus.QUEUED]
                            + counts[TrackStatus.DOWNLOADING]
                            + counts[TrackStatus.WAITING_FOR_NAVIDROME]
                        )
                        if active:
                            ui.badge(f"{active} active", color="primary")
                        if counts[TrackStatus.FAILED]:
                            ui.badge(f"{counts[TrackStatus.FAILED]} failed", color="negative")

                    if job.error:
                        ui.label(job.error).classes("text-negative")

                    async def start_download(job_id: str = job.id) -> None:
                        try:
                            await workflow.start_download(job_id, subject)
                            ui.notify("Downloads started", type="positive")
                        except ValueError as exc:
                            ui.notify(str(exc), type="warning")
                        await update_imports(force=True)

                    async def decide_partial(
                        create_partial: bool, job_id: str = job.id
                    ) -> None:
                        try:
                            await workflow.decide_partial(job_id, subject, create_partial)
                            if create_partial:
                                await workflow.advance(job_id)
                        except ValueError as exc:
                            ui.notify(str(exc), type="warning")
                        await update_imports(force=True)

                    with ui.row().classes("gap-2"):
                        if job.status == ImportStatus.REVIEW:
                            ui.button(
                                "Start downloading", icon="download", on_click=start_download
                            ).props("unelevated")
                        elif job.status == ImportStatus.AWAITING_DECISION:
                            ui.button(
                                "Create partial playlist",
                                icon="playlist_add",
                                on_click=lambda job_id=job.id: decide_partial(True, job_id),
                            ).props("unelevated")
                            ui.button(
                                "Cancel",
                                icon="cancel",
                                on_click=lambda job_id=job.id: decide_partial(False, job_id),
                            ).props("outline color=negative")

                    with ui.expansion("Track details", icon="list").classes("w-full"):
                        ui.table(
                            columns=TRACK_COLUMNS,
                            rows=track_rows(job),
                            row_key="position",
                            pagination={"rowsPerPage": 25},
                        ).classes("w-full").props("flat bordered dense wrap-cells")

            if not completed_jobs:
                ui.separator().classes("my-4")
                ui.label("Completed jobs").classes("text-2xl font-semibold")
                with ui.card().classes("w-full p-8 items-center"):
                    ui.icon("task_alt").classes("text-4xl text-grey-6")
                    ui.label("No completed jobs yet").classes("text-lg text-grey-5")

        async def update_imports(*, force: bool = False) -> None:
            nonlocal rendered_signature
            jobs = await workflow.list_for_user(subject)
            signature = imports_signature(jobs)
            if force or signature != rendered_signature:
                rendered_signature = signature
                await imports.refresh(jobs)
            if imports_need_polling(jobs):
                poll_timer.activate()
            else:
                poll_timer.deactivate()

        with ui.column().classes("w-full max-w-7xl mx-auto p-4 md:p-8 gap-8"):
            with ui.card().classes("w-full min-h-[42vh] p-5"):
                ui.label("Add music").classes("text-2xl font-semibold")
                with ui.tabs(on_change=add_tab_changed).classes("w-full") as add_tabs:
                    link_tab = ui.tab("link", label="Add by link", icon="add_link")
                    playlists_tab = ui.tab(
                        "playlists", label="My TIDAL playlists", icon="queue_music"
                    )
                with ui.tab_panels(add_tabs, value=link_tab).classes("w-full bg-transparent"):
                    with ui.tab_panel(link_tab):
                        ui.label(
                            "Paste a TIDAL playlist or track link. "
                            "Analysis does not start downloads."
                        ).classes("text-grey-5 mb-3")
                        with ui.row().classes("w-full items-end gap-3"):
                            source_input = ui.input(
                                "TIDAL playlist or track URL",
                                placeholder="https://tidal.com/browse/playlist/…",
                            ).classes("grow")

                            async def analyze_link() -> None:
                                source_url = (source_input.value or "").strip()
                                if not source_url:
                                    ui.notify("Enter a TIDAL URL", type="warning")
                                    return
                                analyze_button.disable()
                                await analyze_source(source_url)
                                source_input.value = ""
                                analyze_button.enable()

                            analyze_button = ui.button(
                                "Analyze", icon="search", on_click=analyze_link
                            ).props("unelevated")
                    with ui.tab_panel(playlists_tab):
                        tidal_playlist_browser()

            with ui.column().classes("w-full gap-4"):
                initial_jobs = await workflow.list_for_user(subject)
                rendered_signature = imports_signature(initial_jobs)
                imports(initial_jobs)

        poll_timer = ui.timer(
            5.0,
            update_imports,
            active=imports_need_polling(initial_jobs),
            immediate=False,
        )

    @ui.page("/tidal")
    async def tidal_account(request: Request) -> None:
        identity = page_identity(request, settings, identity_resolver)
        if identity is None:
            return
        groups = identity.get("groups") or []
        if settings.authelia_admin_group not in groups:
            raise HTTPException(
                status_code=403,
                detail=f"Membership in {settings.authelia_admin_group!r} is required",
            )

        username = str(identity["preferred_username"])
        ui.page_title("TIDAL account · Navidrome Music Adder")
        ui.dark_mode().enable()
        ui.colors(primary="#7c3aed", secondary="#06b6d4", accent="#f59e0b")
        render_header(username, show_tidal=True)

        with ui.column().classes("w-full max-w-3xl mx-auto p-4 md:p-8 gap-6"):
            with ui.card().classes("w-full p-6 gap-4"):
                ui.label("TIDAL account").classes("text-2xl font-semibold")
                account = await tidal.account()
                if account:
                    ui.badge("Connected", color="positive")
                    ui.label(account.get("username") or account.get("email") or account["id"])
                    if account.get("email") and account.get("username"):
                        ui.label(account["email"]).classes("text-sm text-grey-5")

                    async def logout() -> None:
                        await tidal.logout()
                        ui.notify("TIDAL account disconnected", type="positive")
                        ui.navigate.to("/tidal")

                    ui.button("Disconnect TIDAL", icon="logout", on_click=logout).props(
                        "outline color=negative"
                    )
                else:
                    ui.badge("Not connected", color="warning")
                    ui.label(
                        "Start authentication, sign in at TIDAL, then paste the final "
                        "redirect URL below."
                    ).classes("text-grey-5")
                    login_flow = ui.column().classes("w-full gap-3")

                    async def start_login() -> None:
                        try:
                            login_url = await tidal.begin_login()
                        except Exception as exc:
                            ui.notify(str(exc), type="negative")
                            return
                        login_flow.clear()
                        with login_flow:
                            ui.link("Open TIDAL authentication", login_url, new_tab=True).classes(
                                "text-lg"
                            )
                            ui.label(
                                "After TIDAL redirects to the error page, copy its complete URL."
                            ).classes("text-sm text-grey-5")
                            redirect_input = ui.input(
                                "Final redirect URL",
                                placeholder="https://login.tidal.com/…?code=…",
                            ).classes("w-full")

                            async def finish_login() -> None:
                                redirect_url = (redirect_input.value or "").strip()
                                if not redirect_url:
                                    ui.notify("Paste the final redirect URL", type="warning")
                                    return
                                complete_button.disable()
                                try:
                                    await tidal.finish_login(redirect_url)
                                except Exception as exc:
                                    ui.notify(str(exc), type="negative", close_button="Dismiss")
                                    complete_button.enable()
                                    return
                                ui.notify("TIDAL account connected", type="positive")
                                ui.navigate.to("/tidal")

                            complete_button = ui.button(
                                "Complete login", icon="check", on_click=finish_login
                            ).props("unelevated")

                    ui.button("Start TIDAL login", icon="login", on_click=start_login).props(
                        "unelevated"
                    )
                    with login_flow:
                        ui.label("The authentication link will appear here.").classes(
                            "text-sm text-grey-6"
                        )

    ui.run_with(
        app,
        storage_secret=settings.secret_key.get_secret_value(),
        title="Navidrome Music Adder",
        favicon="🎵",
    )
