#!/usr/bin/env bash
# Перенос бота на новый сервер в два шага — без риска для текущего продакшена.
#
#   ./migrate_server.sh prep    root@OLD_IP root@NEW_IP
#   ./migrate_server.sh cutover root@OLD_IP root@NEW_IP
#
# prep    — вся подготовка на новом сервере (Docker, код, сборка образа,
#           .env, проверка прямого доступа к Telegram). Старый сервер и бот
#           при этом НЕ трогаются вообще, простоя нет. Можно перезапускать
#           сколько угодно раз, ничего не ломает.
# cutover — собственно переключение: гасим бота на старом сервере,
#           переносим базу, поднимаем на новом. Вот тут и есть простой —
#           обычно секунды-десятки секунд. Запускать только когда prep
#           отработал чисто и ты готов к простою.
#
# Третий необязательный аргумент — папка на серверах (по умолчанию /opt/funnel-bot),
# должна совпадать на старом и новом сервере.
set -euo pipefail

ACTION="${1:-}"
OLD_SERVER="${2:-}"
NEW_SERVER="${3:-}"
REMOTE_DIR="${4:-/opt/funnel-bot}"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_URL="git@github.com:Ya1im/TG-FUNNEL-BOT.git"
DEPLOY_KEY="/root/.ssh/funnel_bot_deploy"

if [[ "$ACTION" != "prep" && "$ACTION" != "cutover" ]] || [ -z "$OLD_SERVER" ] || [ -z "$NEW_SERVER" ]; then
  echo "Использование:"
  echo "  ./migrate_server.sh prep    root@OLD_IP root@NEW_IP"
  echo "  ./migrate_server.sh cutover root@OLD_IP root@NEW_IP"
  exit 1
fi

# Одно SSH-соединение на сервер за прогон — пароль спросит один раз на каждый сервер
CTRL="/tmp/funnelbot-migrate-%r@%h:%p"
SSH_OPTS=(-o ControlMaster=auto -o ControlPath="$CTRL" -o ControlPersist=10m)
ssh_old() { ssh "${SSH_OPTS[@]}" "$OLD_SERVER" "$@"; }
ssh_new() { ssh "${SSH_OPTS[@]}" "$NEW_SERVER" "$@"; }

# ============================================================ prep ==========
prep() {
  echo "▶️  Пушу локальные коммиты в GitHub ..."
  git -C "$LOCAL_DIR" push origin main

  echo "▶️  Проверяю Docker/git на новом сервере ..."
  ssh_new '
    set -e
    if ! command -v docker >/dev/null 2>&1; then
      echo "   Docker не найден, ставлю…"
      curl -fsSL https://get.docker.com | sh
    fi
    docker --version
    docker compose version >/dev/null 2>&1 || { echo "   Ставлю docker compose plugin…"; apt-get update -qq && apt-get install -y -qq docker-compose-plugin; }
    command -v git >/dev/null 2>&1 || { echo "   git не найден, ставлю…"; apt-get update -qq && apt-get install -y -qq git; }
    command -v curl >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq curl; }
  '

  echo "▶️  Проверяю deploy-ключ для GitHub на новом сервере ..."
  ssh_new "
    set -e
    if [ ! -f '$DEPLOY_KEY' ]; then
      ssh-keygen -t ed25519 -f '$DEPLOY_KEY' -N '' -C 'funnel-bot-deploy-new-server' -q
    fi
  "
  GIT_SSH_CMD="ssh -i $DEPLOY_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
  if ! ssh_new "GIT_SSH_COMMAND='$GIT_SSH_CMD' git ls-remote '$REPO_URL' >/dev/null 2>&1"; then
    PUBKEY=$(ssh_new "cat '$DEPLOY_KEY.pub'")
    cat <<TXT

⚠️  Новому серверу нужен отдельный доступ к приватному репозиторию на GitHub — один раз.
    (Старый deploy-ключ с первого сервера сюда не подходит — это отдельная пара ключей.)

Добавь этот ключ как ещё один Deploy key (доступ только на чтение):
  https://github.com/Ya1im/TG-FUNNEL-BOT/settings/keys → Add deploy key

Публичный ключ:
$PUBKEY

После этого запусти "prep" ещё раз — этот шаг больше повторяться не будет.
TXT
    exit 1
  fi

  echo "▶️  Клонирую код на $NEW_SERVER:$REMOTE_DIR через git ..."
  ssh_new "
    set -e
    mkdir -p '$REMOTE_DIR'
    git config --global --add safe.directory '$REMOTE_DIR'
    export GIT_SSH_COMMAND='$GIT_SSH_CMD'
    cd '$REMOTE_DIR'
    if [ ! -d .git ]; then
      git init -q -b main
      git remote add origin '$REPO_URL'
    fi
    git fetch origin main
    git reset --hard origin/main
  "

  echo "▶️  Готовлю папку базы на новом сервере (контейнер работает под uid 1000)…"
  ssh_new "mkdir -p '$REMOTE_DIR/data' && chown -R 1000:1000 '$REMOTE_DIR/data'"

  echo "▶️  Забираю боевой .env со старого сервера (он не в git, копирую как есть) ..."
  OLD_ENV="$(ssh_old "cat '$REMOTE_DIR/.env' 2>/dev/null" || true)"
  if [ -z "$OLD_ENV" ]; then
    echo "❌ Не нашёл $REMOTE_DIR/.env на старом сервере $OLD_SERVER — проверь путь/сервер."
    exit 1
  fi

  echo "▶️  Проверяю, достаёт ли новый сервер до Telegram напрямую ..."
  # Код бота больше не умеет ходить через зеркало (TELEGRAM_API_BASE убран 29.09.2026) —
  # переменную просто выкидываем из .env в любом случае, она ни на что не влияет.
  BOT_TOKEN="$(printf '%s\n' "$OLD_ENV" | grep -m1 '^BOT_TOKEN=' | cut -d= -f2- | tr -d '"'\''\r')"
  DIRECT_OK="no"
  if [ -n "$BOT_TOKEN" ]; then
    HTTP_CODE="$(ssh_new "curl -s -o /dev/null -w '%{http_code}' --max-time 8 'https://api.telegram.org/bot${BOT_TOKEN}/getMe'" || echo "000")"
    if [ "$HTTP_CODE" = "200" ]; then
      DIRECT_OK="yes"
      echo "   ✅ Прямой доступ к api.telegram.org работает (HTTP 200)."
    else
      echo "   ❌ Прямого доступа нет (HTTP $HTTP_CODE) — бот на этом сервере работать НЕ будет:"
      echo "      код больше не поддерживает зеркало API, единственный запасной вариант —"
      echo "      TELEGRAM_PROXY=socks5://... (вписать в .env на сервере вручную)."
    fi
  else
    echo "   ⚠️  Не нашёл BOT_TOKEN в .env — пропускаю проверку, оставляю .env как есть."
  fi

  NEW_ENV="$(printf '%s\n' "$OLD_ENV" | grep -v '^TELEGRAM_API_BASE=')"
  printf '%s\n' "$NEW_ENV" | ssh_new "cat > '$REMOTE_DIR/.env'"
  echo "   .env записан на новый сервер (TELEGRAM_API_BASE, если был, убран — код его больше не читает)."

  echo "▶️  Собираю образ на новом сервере (НЕ запускаю — старый бот продолжает работать) ..."
  ssh_new "cd '$REMOTE_DIR' && docker compose build"

  cat <<TXT

✅ Подготовка готова. Старый сервер всё это время работал без единой секунды простоя.

Когда будешь готов к переключению (это и есть окно простоя, обычно секунды):
  ./migrate_server.sh cutover $OLD_SERVER $NEW_SERVER $REMOTE_DIR
TXT
}

# =========================================================== cutover ========
cutover() {
  echo "▶️  Останавливаю бота на старом сервере ($OLD_SERVER) — отсюда начинается простой ..."
  T0=$(date +%s)
  ssh_old "cd '$REMOTE_DIR' && docker compose stop"

  echo "▶️  Переношу базу (data/) со старого сервера на новый через эту машину ..."
  TMP_DATA="$(mktemp -d)"
  trap 'rm -rf "$TMP_DATA"' EXIT
  scp "${SSH_OPTS[@]}" -q -r "$OLD_SERVER:$REMOTE_DIR/data/." "$TMP_DATA/"
  scp "${SSH_OPTS[@]}" -q -r "$TMP_DATA/." "$NEW_SERVER:$REMOTE_DIR/data/"
  ssh_new "chown -R 1000:1000 '$REMOTE_DIR/data'"

  echo "▶️  Поднимаю контейнер на новом сервере ..."
  ssh_new "cd '$REMOTE_DIR' && docker compose up -d"
  T1=$(date +%s)

  echo "▶️  Логи нового сервера (последние 30 строк):"
  ssh_new "cd '$REMOTE_DIR' && docker compose logs --tail=30"

  cat <<TXT

✅ Переключение готово за $((T1 - T0)) секунд простоя.

Проверь прямо сейчас в Telegram: /start и /admin должны отвечать как обычно.

Старый сервер ($OLD_SERVER) остановлен, но НЕ тронут — данные там целы, это план отката:
  ssh $OLD_SERVER "cd $REMOTE_DIR && docker compose start"
(если откатываешься — учти, что сообщения, обработанные новым сервером после
переключения, при откате на старую базу потеряются, так что делай это только
если новый сервер прямо сейчас явно не работает).

Когда убедишься, что новый сервер стабильно работает (например через день-два),
можно снести старый сервер у хостера — он для бота больше не нужен.
TXT
}

case "$ACTION" in
  prep) prep ;;
  cutover) cutover ;;
esac
