#!/usr/bin/env bash
# СБОРКА (CI): ставит зависимости, гоняет тесты, упаковывает приложение в артефакт dist/todo-app.tar.gz
# Запуск: ./scripts/build.sh   (нужны python3 и python3-venv)
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> Зависимости и тесты"
python3 -m venv .venv-build
. .venv-build/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r app/requirements.txt pytest
(cd app && python -m pytest -q)

echo "==> Артефакт сборки"
mkdir -p dist
tar -czf dist/todo-app.tar.gz \
  --exclude='__pycache__' --exclude='tests' --exclude='*.db' app
ls -lh dist/todo-app.tar.gz 
