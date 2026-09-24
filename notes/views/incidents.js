// Последние записи ops/incidents.md (новые — сверху) — только чтение.
// Вызов: await dv.view("notes/views/incidents", { limit: 3, chars: 260 })

const INCIDENTS = "ops/incidents.md";
const opts = Object.assign({ limit: 3, chars: 260 }, input || {});

function short(text, limit) {
  let s = (text || "").replace(/\[([^\]]*)\]\([^)]*\)/g, "$1").replace(/\*\*/g, "").replace(/<br\s*\/?>/gi, " ").trim();
  if (s.length <= limit) return s;
  s = s.slice(0, limit).replace(/\s+\S*$/, "");
  if ((s.match(/`/g) || []).length % 2) s += "`";
  return s + " …";
}

try {
  const text = await dv.app.vault.adapter.read(INCIDENTS);
  const rows = [];
  let header = null;
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line.startsWith("|")) { header = null; continue; }
    const cells = line.replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
    if (cells[0].toLowerCase() === "время") { header = cells; continue; }
    if (!header || /^[-:\s]*$/.test(line.replace(/\|/g, ""))) continue;
    rows.push(cells);
  }
  if (!rows.length) {
    dv.el("div", "Инцидентов нет.", { cls: "okx-muted" });
  } else {
    dv.table(
      ["Время", "Что обнаружено", "Что осталось"],
      rows.slice(0, opts.limit).map((c) => [c[0], short(c[1], opts.chars), short(c[3], Math.round(opts.chars * 0.7))]),
    );
  }
  dv.el("div", `Всего записей: ${rows.length} · ${dv.fileLink(INCIDENTS)}`, { cls: "okx-muted" });
} catch (e) {
  dv.el("div", `Журнал инцидентов не прочитан: ${e}`, { cls: "okx-banner okx-danger" });
}
