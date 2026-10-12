#!/usr/bin/env bash
# VPS setup: Ubuntu 22.04+
# Запуск: sudo bash deploy/setup.sh
set -euo pipefail

BOT_USER="arb"
INSTALL_DIR="/opt/funding-arb"
PYTHON_MIN="3.11"

echo "=== Funding Rate Arbitrage Bot — VPS Setup ==="

# ── Системные зависимости ──────────────────────────────────────────────────
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv git curl

PYTHON_BIN=$(command -v python3.11 || command -v python3.12 || command -v python3)
PYTHON_VER=$($PYTHON_BIN --version 2>&1 | awk '{print $2}')
echo "Python: $PYTHON_VER"

# ── Пользователь ──────────────────────────────────────────────────────────
if ! id "$BOT_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$BOT_USER"
    echo "Пользователь '$BOT_USER' создан"
fi

# ── Директория ────────────────────────────────────────────────────────────
mkdir -p "$INSTALL_DIR"/{data,logs}
chown -R "$BOT_USER:$BOT_USER" "$INSTALL_DIR"
chmod 750 "$INSTALL_DIR"

echo "Директория: $INSTALL_DIR"

# ── Виртуальное окружение ─────────────────────────────────────────────────
if [ ! -d "$INSTALL_DIR/.venv" ]; then
    $PYTHON_BIN -m venv "$INSTALL_DIR/.venv"
    echo "venv создан"
fi

"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install --quiet -e "$INSTALL_DIR[dev]" 2>/dev/null \
    || "$INSTALL_DIR/.venv/bin/pip" install --quiet -e "$INSTALL_DIR"

echo "Зависимости установлены"

# ── .env ──────────────────────────────────────────────────────────────────
if [ ! -f "$INSTALL_DIR/.env" ]; then
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    chmod 600 "$INSTALL_DIR/.env"
    chown "$BOT_USER:$BOT_USER" "$INSTALL_DIR/.env"
    echo ""
    echo "⚠️  Заполни $INSTALL_DIR/.env перед запуском:"
    echo "    HYPERLIQUID_PRIVATE_KEY=..."
    echo "    NADO_PRIVATE_KEY=..."
    echo "    TELEGRAM_TOKEN=..."
    echo "    TELEGRAM_CHAT_ID=..."
    echo ""
fi

# ── Systemd ───────────────────────────────────────────────────────────────
cp "$INSTALL_DIR/deploy/funding-arb.service" /etc/systemd/system/funding-arb.service
systemctl daemon-reload
systemctl enable funding-arb

echo "Systemd сервис установлен и включён"
echo ""
echo "=== Готово ==="
echo "Управление сервисом:"
echo "  sudo systemctl start   funding-arb   # запустить"
echo "  sudo systemctl stop    funding-arb   # остановить (graceful)"
echo "  sudo systemctl restart funding-arb   # перезапустить"
echo "  sudo systemctl status  funding-arb   # статус"
echo "  journalctl -u funding-arb -f         # логи в реальном времени"
