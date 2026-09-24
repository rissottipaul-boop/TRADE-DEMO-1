// Ответы человека на needs-user из notes/decisions/, ещё не перенесённые на доску (status ≠ «перенесено»).
// Вызов: await dv.view("notes/views/decisions")

try {
  const pages = dv.pages('"notes/decisions"')
    .where((p) => String(p.status ?? "") !== "перенесено")
    .sort((p) => p.file.mtime, "desc");
  if (!pages.length) {
    dv.el("div", "Все ответы перенесены на доску.", { cls: "okx-muted" });
  } else {
    dv.table(["Ответ", "Задача", "Статус", "Записан"], pages.map((p) => [p.file.link, p.task ?? "—", p.status ?? "—", p.date ?? "—"]));
    dv.el("div", "Оркестратор переносит ответ на доску при следующем SYNC и ставит `status: перенесено`.", { cls: "okx-muted" });
  }
} catch (e) {
  dv.el("div", `Ответы не прочитаны: ${e}`, { cls: "okx-banner okx-danger" });
}
