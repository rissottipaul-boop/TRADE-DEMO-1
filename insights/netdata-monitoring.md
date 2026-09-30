# Мониторинг инфраструктуры и торгового бота: Netdata

Статус: **реализовано и протестировано** (30.09.2026)  
Компоненты: `ops/netdata/docker-compose.yml`, `ops/netdata.ps1`, `src/netdata_monitor.py`, `tests/test_netdata.py`, `ops/control-panel/`

---

## 1. Назначение и архитектура

**Netdata** — высокопроизводительная распределенная система мониторинга в реальном времени с минимальным потреблением ресурсов (<1% одного ядра CPU, ~100 МБ RAM).
В проекте OKX-бота Netdata выполняет:
1. **Надзор за хостом и контейнерами:** мониторинг CPU, оперативной памяти, swap, дискового пространства и I/O задержек.
2. **Сетевой мониторинг:** отслеживание сетевых интерфейсов, TCP-сокетов, потерь пакетов и латентности до биржевых шлюзов OKX (Токио / Гонконг).
3. **Обнаружение аномалий:** автоматические алерты при исчерпании файловых дескрипторов, утечках памяти или скачках очереди I/O.
4. **Интеграция с пультом «Контур»:** передача агрегированного статуса здоровья (alarms, cores, OS) в локальный интерфейс управления (порт 8765).

---

## 2. Локальный запуск (Windows / Docker)

Для локального запуска используется оптимизированный контейнер Docker:

```powershell
# Запуск контейнера в фоне
ops\netdata.ps1 start

# Проверка статуса контейнера и API
ops\netdata.ps1 status

# Остановка
ops\netdata.ps1 stop

# Логи
ops\netdata.ps1 logs
```

* **Web UI Dashboard:** `http://127.0.0.1:19999`
* **API Healthcheck:** `http://127.0.0.1:19999/api/v1/info`
* **Конфигурация compose:** `ops/netdata/docker-compose.yml` (монтирование системных счетчиков, автоперезапуск `unless-stopped`).

---

## 3. Развертывание на боевом VPS (Ubuntu/Debian)

При переносе бота на 24/7 VPS агент Netdata устанавливается через штатный скрипт `ops/deploy-vps.sh`:

```bash
# Установка и запуск официального легковесного агента
curl https://get.netdata.cloud/kickstart.sh > /tmp/netdata-kickstart.sh
sh /tmp/netdata-kickstart.sh --dont-wait --disable-telemetry

# Проверка статуса службы
systemctl status netdata
```

Порт 19999 по умолчанию защищается локальным файрволом (`ufw allow from <YOUR_IP> to any port 19999` или туннелированием через SSH/VPN), чтобы метрики не торчали в публичный интернет.

---

## 4. Программный интерфейс (`src/netdata_monitor.py`)

Модуль предоставляет чистый Python API без внешних тяжелых зависимостей:

```python
from src.netdata_monitor import check_netdata_health

status = check_netdata_health(base_url="http://127.0.0.1:19999", timeout=2.0)
if status["available"]:
    print(f"Netdata {status['version']}, ядер: {status['cpu_cores']}, алертов: {status['alarms_critical']}")
else:
    print(f"Netdata офлайн: {status['error']}")
```

CLI для быстрой диагностики:
```bash
python -m src.netdata_monitor --json
```

---

## 5. Интеграция в пульт «Контур»

В веб-пульте «Контур» (`http://127.0.0.1:8765`):
* В блоке «Системный мониторинг» отображается бейдж доступности Netdata (`ONLINE` / `OFFLINE`).
* Выводится количество активных предупреждений и критических алертов.
* Добавлена прямая ссылка на локальный дашборд `http://127.0.0.1:19999`.
