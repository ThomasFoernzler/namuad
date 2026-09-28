#!/usr/bin/env bash
set -euo pipefail

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
state_dir="$project_dir/.local-dev"
quadlet_dir="${XDG_CONFIG_HOME:-$HOME/.config}/containers/systemd"
user_unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
hostnames=(
  auth.nma.test
  music.nma.test
  tidarr.nma.test
  adder.nma.test
)

for command_name in mkcert openssl podman systemctl; do
  command -v "$command_name" >/dev/null || {
    echo "Missing required command: $command_name" >&2
    exit 1
  }
done

mkdir -p \
  "$state_dir/certs" \
  "$state_dir/authelia/data" \
  "$state_dir/navidrome/data" \
  "$state_dir/tidarr/shared/.tiddl" \
  "$state_dir/music" \
  "$state_dir/adder" \
  "$quadlet_dir" \
  "$user_unit_dir"

mkcert -install
mkcert \
  -cert-file "$state_dir/certs/dev.crt" \
  -key-file "$state_dir/certs/dev.key" \
  '*.nma.test'

if [[ ! -f "$state_dir/tidarr/shared/.tiddl/config.toml" ]]; then
  cp "$project_dir/deploy/local-podman-quadlets/tidarr/config.toml" \
    "$state_dir/tidarr/shared/.tiddl/config.toml"
fi

if [[ ! -f "$state_dir/adder/app.env" ]]; then
  session_secret=$(openssl rand -hex 32)
  fernet_key=$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n')
  sed \
    -e "s|@SESSION_SECRET@|$session_secret|" \
    -e "s|@FERNET_KEY@|$fernet_key|" \
    "$project_dir/deploy/local-podman-quadlets/app.env.template" \
    > "$state_dir/adder/app.env"
  chmod 600 "$state_dir/adder/app.env"
fi

# Keep existing generated secrets while refreshing non-secret application settings.
sed -i \
  's|^NMA_BASE_URL=.*|NMA_BASE_URL=https://adder.nma.test:8443|' \
  "$state_dir/adder/app.env"
sed -i '/^NMA_DATABASE_URL=/d' "$state_dir/adder/app.env"
if ! grep -q '^NMA_TIDAL_SESSION_FILE=' "$state_dir/adder/app.env"; then
  printf '\nNMA_TIDAL_SESSION_FILE=/state/tidal-session.json\n' \
    >> "$state_dir/adder/app.env"
fi

for source in "$project_dir"/deploy/local-podman-quadlets/quadlets/*; do
  target="$quadlet_dir/$(basename "$source")"
  sed "s|@PROJECT_DIR@|$project_dir|g" "$source" > "$target"
  chmod 0644 "$target"
done
install -m 0644 \
  "$project_dir/deploy/local-podman-quadlets/systemd/nma-local.target" \
  "$user_unit_dir/"
systemctl --user daemon-reload

echo
echo "Add this line to /etc/hosts if it is not already present:"
printf '127.0.0.1'
printf ' %s' "${hostnames[@]}"
printf '\n\n'
echo "Then start the complete stack with: systemctl --user start nma-local.target"
