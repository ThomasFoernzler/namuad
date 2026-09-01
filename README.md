# Navidrome Music Adder

Import a public TIDAL playlist snapshot, download only missing recordings through
Tidarr, and create a new playlist owned by the current Navidrome user.

## Current scope

- TIDAL playlist snapshots and individual TIDAL tracks.
- Shared service-level TIDAL account using `tidalapi` PKCE login.
- Browse playlists belonging to or favorited by the connected TIDAL account.
- Exact ISRC matching against Navidrome's OpenSubsonic tags or reported filename.
- Optional read-only ISRC filename fallback on a mounted music dataset.
- NiceGUI review screen showing every track and its current library/download state.
- Explicit confirmation before any missing track is submitted to Tidarr.
- Missing-track downloads through Tidarr's HTTP API.
- A Navidrome playlist owned by the signed-in user; an exact name match is
  updated in place, otherwise a new playlist is created.
- Explicit tracks are included.
- Unavailable and non-track playlist items are counted and skipped.
- Failed tracks cause a user decision: create a partial playlist or cancel.
- No automatic source synchronization and no later retry of unavailable tracks.

Spotify is intentionally deferred until the TIDAL path works end to end.

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

[`deploy/local/README.md`](deploy/local/README.md) contains a complete rootless
Podman Quadlet environment with Traefik dynamic-file routing, locally trusted
HTTPS, Authelia, Navidrome, Tidarr, and this application. It uses isolated data
and a development-only `music-test` account.

## Development

Requires Python 3.12 and `uv`:

```bash
cp .env.example .env
uv sync --extra dev
uv run pytest
NMA_DEV_AUTH_BYPASS=true uv run navidrome-music-adder
```

Open [http://localhost:8080](http://localhost:8080). External Tidarr, Navidrome and TIDAL credentials
are required for an actual import.

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
