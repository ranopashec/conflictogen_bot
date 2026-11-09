#!/usr/bin/env bash
set -euo pipefail
if [[ "$(id -u)" != 0 ]]; then
    echo 'Run with sudo: sudo bash deploy/install.sh' >&2
    exit 1
fi
for executable in git docker systemctl flock; do command -v "$executable" >/dev/null; done
docker compose version >/dev/null
docker info >/dev/null
test -s /etc/conflictogen-bot.env || { echo 'Create /etc/conflictogen-bot.env first; see DEPLOY.md.' >&2; exit 1; }
chmod 600 /etc/conflictogen-bot.env
install -d -m 700 /var/lib/conflictogen-bot
install -m 755 "$(dirname "$0")/update.sh" /usr/local/sbin/conflictogen-bot-update
install -m 644 "$(dirname "$0")/conflictogen-bot-update.service" /etc/systemd/system/
install -m 644 "$(dirname "$0")/conflictogen-bot-update.timer" /etc/systemd/system/
systemctl daemon-reload
# Explicitly run once so installation fails visibly if the initial deploy fails.
systemctl start conflictogen-bot-update.service
systemctl enable --now conflictogen-bot-update.timer
echo 'Installed. main is checked every minute; secrets stay in /etc/conflictogen-bot.env.'
