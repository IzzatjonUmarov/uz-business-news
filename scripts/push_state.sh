#!/usr/bin/env bash
# Отправляет data/state.json в служебную ветку bot-state (одним перезаписываемым коммитом).
# Используется в GitHub Actions: нужны переменные GH_TOKEN и GITHUB_REPOSITORY.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f data/state.json ] || exit 0
dir="$(mktemp -d)"
cp data/state.json "$dir/"
cd "$dir"
git init -q
git -c user.name="news-bot" -c user.email="news-bot@users.noreply.github.com" add state.json
git -c user.name="news-bot" -c user.email="news-bot@users.noreply.github.com" commit -qm "state"
git push -qf "https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git" HEAD:bot-state
rm -rf "$dir"
