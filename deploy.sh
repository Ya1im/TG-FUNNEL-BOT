#!/usr/bin/env bash
# Деплой бота на VPS одной командой:
#   ./deploy.sh root@136.234.5.215
# Второй аргумент — папка на сервере (по умолчанию /opt/funnel-bot).
set -euo pipefail

SERVER="${1:-root@136.234.5.215}"
REMOTE_DIR="${2:-/opt/funnel-bot}"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"

# Одно SSH-соединение на весь скрипт — пароль спросит один раз
CTRL="/tmp/funnelbot-ssh-%r@%h:%p"
SSH_OPTS=(-o ControlMaster=auto -o ControlPath="$CTRL" -o ControlPersist=10m)

if [ ! -f "$LOCAL_DIR/.env" ]; then
  echo "❌ Нет файла .env — скопируй .env.example в .env и заполни BOT_TOKEN и ADMIN_IDS"
  exit 1
fi

echo "▶️  Проверяю сервер и Docker…"
ssh "${SSH_OPTS[@]}" "$SERVER" '
  set -e
  if ! command -v docker >/dev/null 2>&1; then
    echo "   Docker не найден, ставлю…"
    curl -fsSL https://get.docker.com | sh
  fi
  docker --version
  docker compose version >/dev/null 2>&1 || { echo "   Ставлю docker compose plugin…"; apt-get update -qq && apt-get install -y -qq docker-compose-plugin; }
'

echo "▶️  Заливаю код в $SERVER:$REMOTE_DIR ..."
ssh "${SSH_OPTS[@]}" "$SERVER" "mkdir -p '$REMOTE_DIR'"
rsync -az --delete \
  -e "ssh ${SSH_OPTS[*]}" \
  --exclude '.venv' --exclude '.venv-agent' --exclude '.git' \
  --exclude 'data' --exclude '__pycache__' --exclude '.pytest_cache' \
  --exclude '.DS_Store' --exclude 'docs' \
  "$LOCAL_DIR/" "$SERVER:$REMOTE_DIR/"

echo "▶️  Готовлю папку базы (контейнер работает под uid 1000)…"
ssh "${SSH_OPTS[@]}" "$SERVER" "mkdir -p '$REMOTE_DIR/data' && chown -R 1000:1000 '$REMOTE_DIR/data'"

echo "▶️  Поднимаю контейнер…"
ssh "${SSH_OPTS[@]}" "$SERVER" "cd '$REMOTE_DIR' && docker compose up -d --build && docker compose ps"

echo "▶️  Логи (последние 30 строк):"
ssh "${SSH_OPTS[@]}" "$SERVER" "cd '$REMOTE_DIR' && docker compose logs --tail=30"

cat <<TXT

✅ Готово. Бот работает на сервере.

Полезное:
  ssh $SERVER "cd $REMOTE_DIR && docker compose logs -f --tail=50"   # смотреть логи
  ssh $SERVER "cd $REMOTE_DIR && docker compose restart"             # перезапуск
  ssh $SERVER "cd $REMOTE_DIR && docker compose exec bot python -m bot.seed"  # шаблон воронки

Дальше открывай бота в Telegram и жми /admin
TXT
