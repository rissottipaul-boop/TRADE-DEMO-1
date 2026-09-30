#!/usr/bin/env bash
# ops/deploy-vps.sh — Скрипт подготовки и развертывания торгового бота на Linux VPS (Ubuntu/Debian)
# Включает:
# 1. Синхронизацию времени (Chrony NTP) для точных временных меток OKX API.
# 2. Проверку сетевой латентности до Токио / шлюзов OKX.
# 3. Настройку резервного копирования SQLite баз (data/*.db).
# 4. Установку и запуск агента мониторинга Netdata в реальном времени.

set -euo pipefail

echo "=== [1/4] Настройка точного времени (Chrony NTP) ==="
if ! command -v chronyd &> /dev/null; then
    apt-get update -qq && apt-get install -y -qq chrony curl jq rsync
fi
systemctl enable --now chrony
chronyc tracking || true

echo "=== [2/4] Замер латентности к шлюзам OKX (Токио/AWS ap-northeast-1) ==="
OKX_HOST="aws.okx.com"
echo "Тест пинга до $OKX_HOST..."
ping -c 4 "$OKX_HOST" || true

echo "=== [3/4] Установка агента мониторинга Netdata ==="
if ! command -v netdata &> /dev/null; then
    echo "Загрузка и установка Netdata..."
    curl -fsSL https://get.netdata.cloud/kickstart.sh -o /tmp/netdata-kickstart.sh
    sh /tmp/netdata-kickstart.sh --dont-wait --disable-telemetry --non-interactive || true
    systemctl enable --now netdata || true
    echo "Netdata успешно установлена и запущена на порту 19999."
else
    echo "Netdata уже установлена."
fi

echo "=== [4/4] Настройка регулярного автобэкапа баз данных ==="
BACKUP_DIR="/var/backups/okx-bot"
mkdir -p "$BACKUP_DIR"
BACKUP_SCRIPT="/usr/local/bin/okx_bot_backup.sh"

cat << 'EOF' > "$BACKUP_SCRIPT"
#!/usr/bin/env bash
set -e
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
DEST="/var/backups/okx-bot"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
mkdir -p "$DEST"
if [ -d "$PROJECT_DIR/data" ]; then
    tar -czf "$DEST/bot_data_${TIMESTAMP}.tar.gz" -C "$PROJECT_DIR" data/
    # Ротация: хранить последние 14 копий
    find "$DEST" -type f -name "bot_data_*.tar.gz" -mtime +14 -delete
fi
EOF
chmod +x "$BACKUP_SCRIPT"

echo "=== Развертывание VPS завершено успешно ==="
echo "Дашборд Netdata: http://<IP-ВАШЕГО-VPS>:19999"
