// Флаги безопасности и признаки жизни движка — только чтение файлов, без сети и без биржи.
// data/KILL и data/STOP_ENGINE — флаги движка (src/engine.py), logs/engine.log пишется раз в 60 с (сверка),
// logs/autostart.log — сторож (раз в ~5 мин), data/AUTOSTART_OFF — его пауза,
// ops/live-policy.json — окно live-торговли. Точный статус — `python -m src.ops status`.
// Вызов: await dv.view("notes/views/safety")

const adapter = dv.app.vault.adapter;
const ENGINE_LOG = "logs/engine.log";
const STALE_MIN = 3; // три пропущенные сверки подряд

const pad = (n) => String(n).padStart(2, "0");
const fmt = (d) => `${pad(d.getDate())}.${pad(d.getMonth() + 1)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
const banner = (cls, text) => dv.el("div", text, { cls: `okx-banner ${cls}` });

async function readIf(path) {
  try {
    return (await adapter.exists(path)) ? await adapter.read(path) : null;
  } catch (e) {
    return null;
  }
}

try {
  const now = new Date();

  const kill = await readIf("data/KILL");
  if (kill !== null) banner("okx-danger", `🛑 **Флаг KILL выставлен** — движок останавливает торговлю. ${kill.trim().slice(0, 200)}`);
  const stop = await readIf("data/STOP_ENGINE");
  if (stop !== null) banner("okx-warn", `⏹ **Флаг STOP_ENGINE выставлен.** ${stop.trim().slice(0, 200)}`);

  const log = await adapter.stat(ENGINE_LOG).catch(() => null);
  if (!log) {
    banner("okx-warn", `⚙️ Движок: \`${ENGINE_LOG}\` не найден — движок не запускался на этой машине.`);
  } else {
    const ageMin = Math.floor((now - log.mtime) / 60000);
    if (ageMin <= STALE_MIN) banner("okx-ok", `⚙️ Движок пишет лог: последняя запись ${fmt(new Date(log.mtime))} (${ageMin} мин назад).`);
    else banner("okx-warn", `⚙️ **Лог движка молчит ${ageMin} мин** (последняя запись ${fmt(new Date(log.mtime))}) — вероятно, движок остановлен. Проверка: \`ops\\engine.ps1 status\`.`);
  }

  const off = await readIf("data/AUTOSTART_OFF");
  if (off !== null) banner("okx-warn", `⏸ **Автозапуск на паузе** — файл \`data/AUTOSTART_OFF\`: сторож и старт при входе отключены без удаления задач.`);

  const watchText = await readIf("logs/autostart.log");
  if (watchText === null) {
    banner("okx-info", `👁 Сторож не писал в \`logs/autostart.log\` — задача «OKX-Bot Watchdog» не зарегистрирована (ENGINE-AUTOSTART-REG).`);
  } else {
    const lines = watchText.split(/\r?\n/).filter((l) => l.trim());
    const ts = new Date((lines[lines.length - 1] || "").split(" ")[0]);
    if (isNaN(ts)) {
      banner("okx-warn", `👁 \`logs/autostart.log\` есть, но последняя строка без даты — сторож пишет не в своём формате.`);
    } else {
      const ageMin = Math.floor((now - ts) / 60000);
      if (ageMin <= 15) banner("okx-ok", `👁 Сторож на дежурстве: последняя проверка ${fmt(ts)} (${ageMin} мин назад).`);
      else banner("okx-warn", `👁 **Сторож молчит ${ageMin} мин** (последняя строка ${fmt(ts)}) — простой движка никто не подберёт.`);
    }
  }

  const policyText = await readIf("ops/live-policy.json");
  if (policyText !== null) {
    const policy = JSON.parse(policyText);
    const until = policy.enabled_until ? new Date(policy.enabled_until) : null;
    if (policy.live_enabled && until && until > now) {
      const days = Math.floor((until - now) / 86400000);
      const hours = Math.floor(((until - now) % 86400000) / 3600000);
      banner("okx-live", `💸 **Live-окно открыто** до ${fmt(until)} (осталось ${days} д ${hours} ч): live-процессы вправе торговать реальными деньгами — \`ops/live-policy.json\`.`);
    } else {
      banner("okx-info", "🧪 Live-окно закрыто — торговля только на demo.");
    }
  }
} catch (e) {
  dv.el("div", `Флаги не прочитаны: ${e}`, { cls: "okx-banner okx-danger" });
}
