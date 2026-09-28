# NAMUAD

NAMUAD (Navidrome Music Adder) analyzes TIDAL playlists and tracks, downloads
only missing recordings through Tidarr, and creates or updates playlists owned
by the current Navidrome user.

## Features

- Import public TIDAL playlists or individual TIDAL tracks by URL.
- Browse playlists owned by or favorited by the connected TIDAL account.
- Authenticate the shared TIDAL account with `tidalapi` PKCE, disconnect it from
  the UI, and retain its encrypted session JSON on a named volume.
- Analyze every track before downloading and show title, artist, album, ISRC,
  explicit flag, and current availability in a NiceGUI dialog.
- Find existing recordings through Navidrome, an optional read-only music mount,
  and Tidarr's queue/history without submitting a download during analysis.
- Submit only missing tracks to Tidarr after the user clicks **Download**.
- Create the Navidrome playlist for the signed-in user, or replace an exact-name
  match in place.
- Browse TIDAL playlists in separate already-synced and available sections.
- Show active imports and completed jobs separately, refreshing only while work
  is active and only redrawing when displayed state changes.
- Let the user create a partial playlist or cancel when some downloads fail.
- Include explicit tracks; report and skip unavailable tracks and non-track items.
- Authenticate users through Authelia OIDC or trusted Traefik ForwardAuth headers.

## Current limitations

- Spotify support is deferred until the TIDAL workflow is considered complete.
- Imports are snapshots; source playlists are not synchronized periodically.
- Unavailable source tracks are not retried automatically.
- Import jobs and completed-job history are process-local and are lost on restart.

## Architecture

The application stores import snapshots and per-track state as Pydantic models
in a process-local dictionary. A background worker advances non-terminal
imports while the process is running:

```text
TIDAL snapshot -> Navidrome/filesystem/Tidarr check -> user review
              -> explicit start -> Tidarr queue -> Navidrome index wait
              -> user decision if incomplete -> create or replace Navidrome playlist
```

Tidarr and Navidrome are accessed with small typed HTTP clients. The application
never writes into the music directory. It can run in API-only mode without a
library mount, including on a different machine from the NAS.

Import state is intentionally disposable for now. Restarting the process clears
current and completed imports, while downloaded music and playlists already
written to Navidrome remain intact. The encrypted TIDAL OAuth session is stored
separately as JSON on the small `adder-state` named volume.

## Authentication and SSO

The web application supports Authelia OIDC and trusted reverse-proxy headers.
With Traefik ForwardAuth, it accepts `Remote-User` only when the immediate
request source is included in `NMA_PROXY_AUTH_TRUSTED_SOURCES`. The validated
username is forwarded on internal Navidrome Subsonic requests, so Navidrome
creates the playlist as that user.

Navidrome must be configured for external authentication and must trust only this
application's internal address or narrowly scoped CIDR. The public reverse proxy
must remove client-provided `Remote-User` headers.

Example Navidrome configuration:

```env
ND_EXTAUTH_USERHEADER=Remote-User
ND_EXTAUTH_TRUSTEDSOURCES=172.30.0.10/32
ND_ENABLEUSEREDITING=false
```

Do not use `0.0.0.0/0` for the trusted source.

## Complete local stack

[`deploy/local-podman-quadlets/README.md`](deploy/local-podman-quadlets/README.md)
contains a complete rootless Podman Quadlet environment with Traefik dynamic-file
routing, locally trusted HTTPS, Authelia, Navidrome, Tidarr, and this application.
It uses isolated data, reserved `nma.test` hostnames, and a development-only
`music-test` account.

## Container images

The [container workflow](.github/workflows/container.yml) builds one image index
containing both supported platforms:

- `linux/amd64` for typical x86-64 PCs;
- `linux/arm64` for 64-bit ARM systems such as an RK3588-based CM3588 NAS.

Pushes to `main` publish `latest` and a `sha-*` tag. Git tags beginning with `v`
also publish the matching tag, for example `v0.1.0`. Pull requests build both
platforms without publishing them.

Images are published to GitHub Container Registry using the repository name:

```text
ghcr.io/<github-owner>/namuad:latest
```

Docker and Podman automatically select the correct architecture from the image
index. The same command therefore works on the NAS and local PC:

```bash
podman pull ghcr.io/<github-owner>/namuad:latest
```

For a native development build without publishing:

```bash
podman build -t namuad:dev .
```

The workflow uses the repository-provided `GITHUB_TOKEN`; no registry password
secret is required. If the GHCR package is private, log in with a GitHub token
that can read packages before pulling it, or change the package visibility to
public in GitHub's package settings.

## Development

Requires Python 3.12 and `uv`:

```bash
cp .env.example .env
uv sync --extra dev
uv run pytest
NMA_DEV_AUTH_BYPASS=true uv run namuad
```

Open [http://localhost:8080](http://localhost:8080). External Tidarr, Navidrome,
and TIDAL credentials are required for an actual import.

Submitting a URL only analyzes the snapshot and opens a preview dialog. The UI
lists title, artist, album, ISRC, explicit flag, and state for every track. The
preview is not stored as an import job and Tidarr is not called until
**Download** is clicked. Active imports and completed jobs are shown separately.

The TIDAL playlist browser groups exact-name Navidrome matches above playlists
which have not been imported yet.

Before TIDAL login, generate `NMA_TOKEN_ENCRYPTION_KEY` as shown in `.env.example`.
An administrator can open `/tidal`, start TIDAL authentication, complete the
login in a new tab, and paste the final redirect URL back into the application.
The same page can disconnect the stored TIDAL session. The API endpoints below
remain available for automation.

The encrypted OAuth token belongs to the service, not an individual app user.

## Tidarr duplicate behavior

Tidarr does not expose a dry-run or existence-check endpoint. The review phase
therefore performs that check in this application using Navidrome first, the
optional read-only filesystem second, and Tidarr's existing queue/history last.
It does not call Tidarr's `POST /api/save`.

Tidarr's `POST /api/save` replaces a queue item with the same ID. This project:

- never posts an ID already queued, downloading, or processing;
- leaves errors for user review;
- reposts a `finished` ID only when filesystem access is enabled and neither
  Navidrome nor the ISRC filename exists;
- performs that stale-item requeue at most once per import.

Without filesystem access, a finished Tidarr item is never requeued
automatically: the worker waits up to `NMA_NAVIDROME_INDEX_TIMEOUT_SECONDS` for
Navidrome, then reports the track as failed so the user can choose whether to
create a partial playlist.

That behavior follows Tidarr's `ProcessingStack.addItem` implementation rather
than assuming that `201 Created` is idempotent.

## Important assumptions

- Downloaded filenames are normalized ISRCs, for example `USRC17607839.flac`.
- `NMA_MUSIC_PATH` is optional. When set, it points at the same dataset seen by
  Tidarr and Navidrome and is mounted read-only inside this container.
- Navidrome's file watcher is enabled; the worker waits for the resulting song ID.
- Duplicate Navidrome ISRCs are invalid library state. If encountered, the first
  result returned by Navidrome is used; no release preference is promised.

## API

- `GET /api/imports`
- `POST /api/imports` with `{"url": "https://tidal.com/playlist/..."}`
- `GET /api/imports/{id}`
- `POST /api/imports/{id}/start`
- `POST /api/imports/{id}/partial-decision`
- `GET /api/tidal/playlists`
- `GET /api/admin/tidal/status`
- `POST /api/admin/tidal/login`
- `POST /api/admin/tidal/login/complete`
- `DELETE /api/admin/tidal/session`
- `GET /healthz`
