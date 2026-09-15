# Если хостер блокирует api.telegram.org

Симптом: `curl -m 15 https://api.telegram.org` отваливается по таймауту, бот висит
после строки «База подключена» и не доходит до `Start polling`.

Бот умеет обходить это двумя способами — оба включаются одной строкой в `.env`.

---

## Вариант 1. Зеркало Telegram API на Cloudflare Worker

Воркер просто проксирует запросы на api.telegram.org:

```js
export default {
  async fetch(request) {
    const url = new URL(request.url);
    url.protocol = "https:";
    url.host = "api.telegram.org";
    return fetch(new Request(url, request));
  },
};
```

В `.env` на сервере:

```
TELEGRAM_API_BASE=https://твой-воркер.workers.dev
```

Адрес — **без** `/bot<token>` на конце: бот сам достроит путь, включая скачивание файлов
(`/file/bot<token>/...`).

Проверка с сервера:

```bash
set -a && . ./.env && set +a
curl -sS -m 20 "$TELEGRAM_API_BASE/bot$BOT_TOKEN/getMe"; echo
```

Плюс: ничего не надо держать запущенным, трафик идёт через Cloudflare.
Минус: токен ходит через воркер — заводи его на своём аккаунте, не на чужом.

---

## Вариант 2. SOCKS5-туннель на сервер, с которого Telegram доступен

На заблокированном сервере поднимаем SSH-туннель до «чистого» (например, финского):

```bash
# ключ, чтобы туннель поднимался без пароля
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
ssh-copy-id root@IP_ЧИСТОГО_СЕРВЕРА     # пароль вводится один раз

# проверка руками
ssh -f -N -D 172.17.0.1:1080 root@IP_ЧИСТОГО_СЕРВЕРА
curl -sS -m 20 --socks5-hostname 172.17.0.1:1080 https://api.telegram.org | head -c 80; echo
```

`172.17.0.1` — адрес docker-моста, по нему контейнер видит хост.

Чтобы туннель поднимался сам после перезагрузки — `/etc/systemd/system/tg-tunnel.service`:

```ini
[Unit]
Description=SOCKS tunnel for Telegram
After=network-online.target

[Service]
ExecStart=/usr/bin/ssh -N -D 172.17.0.1:1080 -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes root@IP_ЧИСТОГО_СЕРВЕРА
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now tg-tunnel && systemctl status tg-tunnel --no-pager
```

В `.env`:

```
TELEGRAM_PROXY=socks5://172.17.0.1:1080
```

Плюс: весь трафик бота идёт через твой сервер, токен нигде не светится.
Минус: ещё один сервис, который должен работать.

---

## Вариант 3. Просто развернуть бота на «чистом» сервере

Если на финском сервере Telegram доступен — самый надёжный путь: развернуть бота там
и не городить прокси. Порядок тот же, что в README.

---

После любого варианта:

```bash
cd /opt/funnel-bot && docker compose up -d && docker compose logs --tail=20
```

Ищи строку `Запускаю @твой_бот`.
