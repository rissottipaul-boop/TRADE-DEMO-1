// Инсайты insights/*.md: заголовок, статус (сначала frontmatter `status`, потом строка «**Статус:**»),
// число незакрытых пунктов «Открытых вопросов», время правки — только чтение.
// Вызов: await dv.view("notes/views/insights", { limit: 12 })

const opts = Object.assign({ limit: 0 }, input || {});
const ICONS = [
  [/^(validated|подтвержд)/i, "✅"],
  [/^(implemented|реализовано)/i, "🛠"],
  [/^(running|в работе)/i, "▶️"],
  [/^idea/i, "💡"],
  [/^(rejected|отклонен)/i, "❌"],
];

const pad = (n) => String(n).padStart(2, "0");
const fmt = (d) => `${pad(d.getDate())}.${pad(d.getMonth() + 1)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;

// «**реализовано** (BT-IMPL…)» → «реализовано», «validated — отчёт…» → «validated»
function statusOf(text) {
  const m = text.match(/^\s*(?:-\s*)?\*\*Статус:\*\*\s*(.+)$/m);
  if (!m) return "";
  return m[1].replace(/\*\*/g, "").split(/\s+[—–-]\s+|\s*[(;,]|\.\s/)[0].replace(/[.:]+$/, "").trim().slice(0, 40);
}

// Число незакрытых пунктов раздела «Открытые вопросы»: null — раздела нет.
// Закрыт пункт с зачёркнутым заголовком («1. ~~…~~ — закрыт …»), остальное — открыто.
function openCount(text) {
  const m = text.match(/^##\s+.*открытые вопросы.*$/im);
  if (!m) return null;
  const section = text.slice(m.index + m[0].length).split(/^##\s/m)[0];
  const items = section.match(/^\s*(?:\d+[.)]|[-*])\s+.+$/gm) || [];
  return items.filter((l) => !/^\s*(?:\d+[.)]|[-*])\s*~~/.test(l)).length;
}

try {
  let pages = dv.pages('"insights"').sort((p) => p.file.mtime, "desc");
  if (opts.limit) pages = pages.limit(opts.limit);
  const rows = [];
  for (const p of pages) {
    const text = await dv.app.vault.adapter.read(p.file.path);
    const title = (text.match(/^#\s+(.+)$/m)?.[1] || p.file.name).trim();
    const status = String(p.status ?? "").trim() || statusOf(text);
    const icon = ICONS.find(([re]) => re.test(status))?.[1] || "▫️";
    const open = openCount(text);
    rows.push([
      dv.fileLink(p.file.path, false, title.length > 90 ? title.slice(0, 88) + "…" : title),
      status ? `${icon} ${status}` : "—",
      open === null ? "—" : open === 0 ? "✓" : `❓ ${open}`,
      fmt(new Date(p.file.mtime.toMillis())),
    ]);
  }
  dv.table(["Инсайт", "Статус", "Вопросы", "Изменён"], rows);
} catch (e) {
  dv.el("div", `Инсайты не прочитаны: ${e}`, { cls: "okx-banner okx-danger" });
}
