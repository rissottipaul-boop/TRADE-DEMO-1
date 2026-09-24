"""Карта кодов ошибок OKX (Фаза 0, задача p0-error-map).

Источники кодов: insights/okx-api.md, прогоны smoke-теста
(см. insights/phase0-smoke-test.md — 51020 пойман вживую).

Использование:
    from .errors import explain_error, extract_error_code
    try:
        ...
    except ccxt.ExchangeError as exc:
        code = extract_error_code(exc)
        log.error("%s", explain_error(code))
        raise
"""
import json
import re
from typing import Optional

# code -> (человекочитаемое описание, рекомендуемое действие)
ERROR_MAP: dict[str, tuple[str, str]] = {
    # --- Время / подпись ---
    "50102": (
        "Timestamp request expired — метка времени запроса просрочена",
        "Синхронизировать системные часы по NTP и повторить запрос; "
        "проверять дрейф через check_time_sync() до старта бота",
    ),
    "50112": (
        "Invalid OK-ACCESS-TIMESTAMP — некорректная метка времени в заголовке",
        "Синхронизировать часы по NTP; убедиться, что время в миллисекундах UTC",
    ),
    "50036": (
        "Дедлайн expTime уже прошёл — биржа отбросила запрос, ордер НЕ размещён",
        "Проверить дрейф часов (check_time_sync) и задержку сети; повторять "
        "с новым clOrdId и новым expTime (connector.exp_time_ms)",
    ),
    # --- Лимиты ---
    "50011": (
        "Too Many Requests — превышен rate limit эндпоинта "
        "(per-endpoint, лимит и счётчик свои у каждого эндпоинта)",
        "Это НЕ фатально: повторить с экспоненциальным backoff (старт ~0.5 с, "
        "кап ~8 с) — одиночный ретрай через 0.5 с обычно успешен. Лимитер "
        "ведёт себя как token bucket, заголовков Retry-After биржа не присылает "
        "— паузу подбирать самим (замерено на demo, insights/okx-api.md §3)",
    ),
    "50061": (
        "Rate limit суб-аккаунта исчерпан — слишком частые ордер-запросы "
        "(place/amend, лимит 1000 запросов/2с на суб-аккаунт, batch считается поштучно)",
        "Снизить частоту ордеров; enableRateLimit=True уже включён в CCXT, "
        "при повторении — экспоненциальный backoff 0.5→8 с; для устойчиво "
        "высокой частоты — несколько суб-аккаунтов (insights/okx-api.md §3)",
    ),
    # --- Ключи / домен ---
    "50119": (
        "API-ключ не существует или неверный домен (OKX_DOMAIN)",
        "Проверить ключ в .env и OKX_DOMAIN: www.okx.com (global) / eea.okx.com (EEA) / "
        "us.okx.com (US) — ключ работает только в своём регионе; "
        "для demo использовать demo-ключи (OKX_DEMO_*)",
    ),
    # --- Режим аккаунта ---
    "51010": (
        "Запрос не поддерживается в текущем режиме аккаунта "
        "(spot/contract/multi-currency)",
        "Проверить режим аккаунта в настройках OKX; режим фиксируется до старта "
        "бота и не меняется при открытых позициях (insights/okx-api.md)",
    ),
    "50038": (
        "Функция недоступна в demo trading (например, Simple Earn: "
        "earn savings balance / lending-rate-history под demo-ключом)",
        "Не ретраить: это ограничение demo, а не сбой. Проверку перенести на live "
        "(только чтение) или взять публичный эндпоинт без demo-заголовка — "
        "lending-rate-history отдаётся без авторизации (insights/idle-cash-earn.md §1)",
    ),
    # --- Средства / размер ордера ---
    "51008": (
        "Недостаточно средств на счёте для ордера (insufficient balance)",
        "Проверить свободный баланс (часть может быть заморожена в ордерах "
        "или grid-ботах); уменьшить размер позиции (insights/okx-api.md §5)",
    ),
    "51020": (
        "Стоимость ордера ниже минимальной (minimum order amount)",
        "Проверять sz * px >= minNotional ДО отправки; минимум считается по "
        "стоимости, а не по minSz (см. insights/phase0-smoke-test.md)",
    ),
    # --- Аутентификация ---
    "50111": (
        "Invalid OK-ACCESS-KEY — неверный API-ключ",
        "Проверить API_KEY в .env (demo-ключи не работают на live и наоборот)",
    ),
    "50113": (
        "Invalid signature — подпись запроса не сошлась",
        "Проверить SECRET и PASSPHRASE в .env; passphrase передаётся как "
        "password в CCXT; убедиться, что ключ не скопирован с пробелами",
    ),
}

_FALLBACK_ACTION = (
    "Зафиксировать sCode/sMsg в логе и остановиться; не глушить исключение"
)


def explain_error(code: Optional[str]) -> str:
    """Человекочитаемое описание ошибки OKX по коду.

    Возвращает строку «код — описание. Действие: ...».
    Неизвестный код не скрывается: возвращается как есть.
    """
    if not code:
        return "Код ошибки не извлечён — смотреть сырой ответ API в логе"
    code = str(code).strip()
    if code in ERROR_MAP:
        description, action = ERROR_MAP[code]
        return f"OKX {code}: {description}. Действие: {action}"
    return f"OKX {code}: код не в карте ошибок. Действие: {_FALLBACK_ACTION}"


# Код OKX в сообщении CCXT: "okx {"code":"51020",...}" или bare-код
_CODE_RE = re.compile(r'"code"\s*:\s*"(\d{5})"|\b(\d{5})\b')


def extract_error_code(exc: BaseException) -> Optional[str]:
    """Извлечь код ошибки OKX (5 цифр) из исключения CCXT.

    CCXT кладёт сырой JSON ответа в текст исключения; пробуем распарсить
    JSON, затем — регэксп по тексту.
    """
    for arg in exc.args:
        if not isinstance(arg, str):
            continue
        # Попытка достать JSON из текста исключения
        start = arg.find("{")
        if start != -1:
            try:
                payload = json.loads(arg[start:])
                # Верхнеуровневый code может быть "0" при отказе в data[].sCode
                # (см. insights/phase0-smoke-test.md, находка №3)
                for item in payload.get("data", []):
                    s_code = str(item.get("sCode", ""))
                    if s_code.isdigit() and s_code != "0":
                        return s_code
                code = str(payload.get("code", ""))
                if code.isdigit() and code != "0":
                    return code
            except (json.JSONDecodeError, AttributeError, TypeError):
                pass
        match = _CODE_RE.search(arg)
        if match:
            return match.group(1) or match.group(2)
    return None
