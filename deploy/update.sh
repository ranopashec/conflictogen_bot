#!/usr/bin/env bash
# Server-side pull deployment: no credentials or inbound webhook required.
set -euo pipefail
umask 077
export GIT_TERMINAL_PROMPT=0
repo=${CONFLICTOGEN_REPO_URL:-https://github.com/ranopashec/conflictogen_bot.git}
state=${CONFLICTOGEN_STATE_DIR:-/var/lib/conflictogen-bot}
config=${CONFLICTOGEN_ENV_FILE:-/etc/conflictogen-bot.env}
mkdir -p "$state/releases"
exec 9>"$state/deploy.lock"
flock -n 9 || exit 0
test -s "$config" || { echo "Missing server config: $config" >&2; exit 1; }
if ! test -d "$state/repo.git"; then
    git clone --bare "$repo" "$state/repo.git"
fi
git --git-dir="$state/repo.git" fetch --quiet origin refs/heads/main:refs/remotes/origin/main
commit=$(git --git-dir="$state/repo.git" rev-parse refs/remotes/origin/main)
[[ "$commit" =~ ^[a-f0-9]{40}$ ]] || exit 1
previous=$(cat "$state/current" 2>/dev/null || true)
compose() {
    BOT_IMAGE="conflictogen-bot:$1" BOT_ENV_FILE="$config" \
        docker compose --project-name conflictogen-bot --env-file "$config" \
        -f "$state/releases/$1/compose.yaml" "${@:2}"
}
if [[ "$commit" == "$previous" ]]; then
    container=$(compose "$commit" ps -q conflict-bot)
    if [[ -n "$container" ]] && [[ "$(docker inspect --format '{{.State.Health.Status}}' "$container" 2>/dev/null || true)" == healthy ]]; then
        exit 0
    fi
    compose "$commit" up -d --no-build --wait --wait-timeout 120
    exit 0
fi
release="$state/releases/$commit"
mkdir -p "$release"
git --git-dir="$state/repo.git" archive "$commit" | tar -x -C "$release"
echo "Building and testing commit $commit"
# The Dockerfile requires tests to pass before producing the runtime image.
docker build --tag "conflictogen-bot:$commit" "$release"
if compose "$commit" up -d --no-build --wait --wait-timeout 120; then
    printf '%s\n' "$commit" > "$state/current.tmp"
    mv "$state/current.tmp" "$state/current"
    echo "Deployed commit $commit"
else
    echo "New version failed startup; attempting previous container version" >&2
    if [[ "$previous" =~ ^[a-f0-9]{40}$ ]] && test -f "$state/releases/$previous/compose.yaml"; then
        compose "$previous" up -d --no-build --wait --wait-timeout 120
    fi
    exit 1
fi
