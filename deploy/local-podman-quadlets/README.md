# Complete local test stack

This stack runs Traefik, Authelia, Navidrome, Tidarr, and Navidrome Music Adder
as rootless Podman Quadlets. Traefik is configured exclusively through the
dynamic file provider.

## URLs

- `https://auth.nma.test:8443`
- `https://music.nma.test:8443`
- `https://tidarr.nma.test:8443`
- `https://adder.nma.test:8443`

The test login is `music-test` / `music-test`. These credentials and all other
secrets in this stack are development-only.

## Start

Run from the repository root:

```bash
deploy/local/setup.sh
systemctl --user start nma-local.target
```

The `nma-local.target` unit groups the entire stack. To start it automatically
when your user systemd instance starts, use:

```bash
systemctl --user enable --now nma-local.target
```

The setup script installs the local mkcert CA, creates a certificate, prepares
state directories, installs the Quadlets, and prints the `/etc/hosts` line that
must be added with `sudo`.

Follow startup with:

```bash
journalctl --user -f \
  -u nma-local-traefik \
  -u nma-local-authelia \
  -u nma-local-navidrome \
  -u nma-local-tidarr \
  -u nma-local-adder
```

First open Navidrome through Traefik. Authelia will sign in `music-test`, and
Navidrome will create the matching user. Because this is a new Navidrome data
directory, that first user becomes its administrator.

Then open Tidarr and complete its TIDAL device login. Tidarr generates its API
key at `/shared/.tidarr-api-key`; the music-adder reads that file directly from
the shared read-only mount, so no manual copying or restart is required.

Tidarr intentionally runs as container root without `PUID`/`PGID`. Because the
container itself is rootless, that identity maps to the host user running the
Quadlet rather than host root. It can therefore write the host-user-owned local
music directory without a recursive `:U` chown.

Finally, complete the music-adder's separate TIDAL login through its admin API.
Both logins may use the same TIDAL account, but they store separate OAuth tokens.
The adder's encrypted OAuth session JSON is kept in the
`nma-local-adder-state` named volume. Its import queue and completed-job history
are kept in memory and cleared whenever the adder restarts.

## Stop and reset

```bash
systemctl --user stop nma-local.target
```

Most persistent development state is under `.local-dev/`. The adder's TIDAL
session is instead in the `nma-local-adder-state` Podman volume. Removing both
resets all accounts, tokens, downloads, and databases. Stop the units first.

## Why the Tailscale address is not used here

All five services run on the same private Podman network. Navidrome sees the
fixed container addresses of Traefik and the music-adder, so trusting this PC's
Tailscale address would not authenticate either caller. The Tailscale address is
only needed when the music-adder runs on this PC while Navidrome runs elsewhere.
