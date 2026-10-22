#!/usr/bin/env bash
# ДЕПЛОЙ (CD): запускается на сервере. Берёт готовый образ, поднимает приложение и мониторинг.
#
# Пример: IMAGE=ghcr.io/<user>/todo-tracker:<sha> bash scripts/deploy.sh
# Откат:  тот же вызов со старым тегом образа.
# Если образ в приватном registry: GHCR_USER=<user> и токен через stdin (так делает CI).
set -euo pipefail
cd "$(dirname "$0")/.."

: "${IMAGE:?Укажите IMAGE, например ghcr.io/<user>/todo-tracker:latest}"
APP_VERSION="${APP_VERSION:-${IMAGE##*:}}"

if [ -n "${GHCR_USER:-}" ] && [ ! -t 0 ]; then
  read -r GHCR_TOKEN || true
  if [ -n "${GHCR_TOKEN:-}" ]; then
    echo "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USER" --password-stdin
  fi
fi

# Запоминаем выбранную версию, чтобы после перезагрузки сервера поднялась она же
touch .env
grep -v -E '^(IMAGE|APP_VERSION)=' .env > .env.tmp || true
printf 'IMAGE=%s\nAPP_VERSION=%s\n' "$IMAGE" "$APP_VERSION" >> .env.tmp
mv .env.tmp .env

echo "==> Обновляем контейнеры ($IMAGE)"
docker compose pull app
docker compose up -d --remove-orphans

echo "==> Ждём health check"
for _ in $(seq 1 30); do
  status="$(docker inspect -f '{{.State.Health.Status}}' todo-app 2>/dev/null || echo starting)"
  if [ "$status" = "healthy" ]; then
    echo "Приложение healthy, деплой завершён"
    docker image prune -f >/dev/null
    exit 0
  fi
  sleep 2
done

echo "Приложение не стало healthy, последние логи:" >&2
docker compose logs --tail=50 app >&2
exit 1
