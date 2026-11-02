#!/usr/bin/env bash
cd "$(dirname "$0")/.."
[ -f data/bot.pid ] || { echo "Бот не запущен"; exit 0; }
kill -- -"$(cat data/bot.pid)" 2>/dev/null || kill "$(cat data/bot.pid)" 2>/dev/null
rm -f data/bot.pid; echo "Бот остановлен"
