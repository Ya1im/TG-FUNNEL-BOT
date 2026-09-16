#!/usr/bin/env bash
# Деплой бота на VPS одной командой:
#   ./deploy.sh root@136.234.5.215
# Второй аргумент — папка на сервере (по умолчанию /opt/funnel-bot).
#
# Код на сервер идёт через git pull из приватного репозитория на GitHub,
# а не заливкой файлов — так сервер получает ровно то, что закоммичено,
# и на нём остаётся нормальная история. Обновляются только файлы под git;
# .env и data/ не отслеживаются и не трогаются.
set -euo pipefail

SERVER="${1:-root@136.234.5.215}"
REMOTE_DIR="${2:-/opt/funnel-bot}"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_URL="git@github.com:Ya1im/TG-FUNNEL-BOT.git"
DEPLOY_KEY="/root/.ssh/funnel_bot_deploy"

# Одно SSH-соединение на весь скрипт — пароль спросит один раз
CTRL="/tmp/funnelbot-ssh-%r@%h:%p"
SSH_OPTS=(-o ControlMaster=auto -o ControlPath="$CTRL" -o ControlPersist=10m)

if [ ! -f "$LOCAL_DIR/.env" ]; then
  echo "❌ Нет файла .env — скопируй .env.example в .env и заполни BOT_TOKEN и ADMIN_IDS"
  exit 1
fi

echo "▶️  Пушу локальные коммиты в GitHub ..."
git -C "$LOCAL_DIR" push origin main

echo "▶️  Проверяю сервер, Docker и git ..."
ssh "${SSH_OPTS[@]}" "$SERVER" '
  set -e
  if ! command -v docker >/dev/null 2>&1; then
    echo "   Docker не найден, ставлю…"
    curl -fsSL https://get.docker.com | sh
  fi
  docker --version
  docker compose version >/dev/null 2>&1 || { echo "   Ставлю docker compose plugin…"; apt-get update -qq && apt-get install -y -qq docker-compose-plugin; }
  command -v git >/dev/null 2>&1 || { echo "   git не найден, ставлю…"; apt-get update -qq && apt-get install -y -qq git; }
'

echo "▶️  Проверяю deploy-ключ для GitHub ..."
ssh "${SSH_OPTS[@]}" "$SERVER" "
  set -e
  if [ ! -f '$DEPLOY_KEY' ]; then
    ssh-keygen -t ed25519 -f '$DEPLOY_KEY' -N '' -C 'funnel-bot-deploy' -q
  fi
"
GIT_SSH_CMD="ssh -i $DEPLOY_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
if ! ssh "${SSH_OPTS[@]}" "$SERVER" "GIT_SSH_COMMAND='$GIT_SSH_CMD' git ls-remote '$REPO_URL' >/dev/null 2>&1"; then
  PUBKEY=$(ssh "${SSH_OPTS[@]}" "$SERVER" "cat '$DEPLOY_KEY.pub'")
  cat <<TXT

⚠️  Серверу нужен доступ к приватному репозиторию на GitHub — один раз.

Добавь этот ключ как Deploy key (доступ только на чтение):
  https://github.com/Ya1im/TG-FUNNEL-BOT/settings/keys → Add deploy key

Публичный ключ:
$PUBKEY

После этого запусти деплой ещё раз — этот шаг больше повторяться не будет.
TXT
  exit 1
fi

echo "▶️  Обновляю код на $SERVER:$REMOTE_DIR через git ..."
ssh "${SSH_OPTS[@]}" "$SERVER" "
  set -e
  mkdir -p '$REMOTE_DIR'
  git config --global --add safe.directory '$REMOTE_DIR'
  export GIT_SSH_COMMAND='$GIT_SSH_CMD'
  cd '$REMOTE_DIR'
  if [ ! -d .git ]; then
    echo '   Первый деплой через git — подключаю репозиторий на месте (данные и .env не трогаю)'
    git init -q -b main
    git remote add origin '$REPO_URL'
  fi
  git fetch origin main
  git reset --hard origin/main
"

echo "▶️  Готовлю папку базы (контейнер работает под uid 1000)…"
ssh "${SSH_OPTS[@]}" "$SERVER" "mkdir -p '$REMOTE_DIR/data' && chown -R 1000:1000 '$REMOTE_DIR/data'"

LOCAL_ENV_HASH=$(shasum -a 256 "$LOCAL_DIR/.env" | awk '{print $1}')
REMOTE_ENV_HASH=$(ssh "${SSH_OPTS[@]}" "$SERVER" "sha256sum '$REMOTE_DIR/.env' 2>/dev/null | awk '{print \$1}' || true")
if [ "$LOCAL_ENV_HASH" != "$REMOTE_ENV_HASH" ]; then
  echo "⚠️  Локальный .env отличается от серверного — .env не в git и через git pull не обновляется."
  echo "    Если менял .env — скопируй руками: scp .env $SERVER:$REMOTE_DIR/.env"
fi

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
  ssh $SERVER "cd $REMOTE_DIR && git log --oneline -5"                # что сейчас задеплоено

Дальше открывай бота в Telegram и жми /admin
TXT
