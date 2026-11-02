#!/usr/bin/env bash
# Запуск бота в фоне с автоперезапуском. Логи: data/bot.log, PID: data/bot.pid
cd "$(dirname "$0")/.."
if [ -f data/bot.pid ] && kill -0 "$(cat data/bot.pid)" 2>/dev/null; then
  echo "Бот уже запущен (PID $(cat data/bot.pid))"; exit 0
fi
PY=.venv/bin/python; [ -x "$PY" ] || PY=python3
nohup setsid bash -c "while true; do $PY -m bot run --loop \${INTERVAL:-900} --with-rates; echo \"\$(date) бот остановился, перезапуск через 30 с\"; sleep 30; done" >> data/bot.log 2>&1 &
echo $! > data/bot.pid
echo "Бот запущен (PID $!), лог: data/bot.log"
