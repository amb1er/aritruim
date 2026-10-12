#!/usr/bin/env bash
# Обновление бота без потери позиций (save_state shutdown)
# Запуск: sudo bash deploy/update.sh
set -euo pipefail

INSTALL_DIR="/opt/funding-arb"

echo "=== Обновление Funding Rate Arbitrage Bot ==="

# Останавливаем (SIGTERM → graceful shutdown, позиции сохраняются в БД)
echo "Останавливаем сервис..."
systemctl stop funding-arb
sleep 5

# Обновляем код
echo "Обновляем код..."
cd "$INSTALL_DIR"
git pull --ff-only

# Обновляем зависимости
echo "Обновляем зависимости..."
.venv/bin/pip install --quiet -e .

# Запускаем
echo "Запускаем сервис..."
systemctl start funding-arb
sleep 3

systemctl status funding-arb --no-pager
echo ""
echo "=== Обновление завершено ==="
