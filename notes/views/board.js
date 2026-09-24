// Доска ops/board.md по статусам — только чтение, доску правят агенты и человек.
// Разбор таблиц повторяет parse_board из ops/hooks/autopilot.py: статус — первое слово ячейки,
// «можно брать» = ready или наступивший scheduled, и все зависимости в done.
// Вызов: await dv.view("notes/views/board", { show: ["needs-user"], notes: 220 })
// Dataview не ждёт view с await и не показывает его ошибки, поэтому ошибки ловим сами.

const BOARD = "ops/board.md";
const DECISIONS = "notes/decisions";
const app = dv.app;
const opts = Object.assign(
  { show: ["needs-user", "blocked", "in-progress", "ready", "scheduled"], notes: 220 },
  input || {},
);

function parseBoard(text) {
  const tasks = new Map();
  let columns = null;
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line.startsWith("|")) {
      if (!line) columns = null;
      continue;
    }
    const cells = line.replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
    const lowered = cells.map((c) => c.toLowerCase());
    if (lowered.includes("id") && lowered.includes("статус")) {
      columns = {};
      lowered.forEach((name, i) => { if (!(name in columns)) columns[name] = i; });
      continue;
    }
    if (!columns || /^[-:\s]*$/.test(line.replace(/\|/g, ""))) continue;
    const cell = (name) => (name in columns && columns[name] < cells.length ? cells[columns[name]] : "");
    const id = cell("id").replace(/^[`* ]+|[`* ]+$/g, "");
    if (!id) continue;
    const statusCell = cell("статус").replace(/^[` ]+|[` ]+$/g, "");
    const m = statusCell.match(/^(\S+)\s*(.*)$/);
    tasks.set(id, {
      id,
      status: m ? m[1].toLowerCase() : "",
      arg: m ? m[2].replace(/^[` ]+|[` ]+$/g, "") : "",
      deps: cell("зависит от").split(/[,;\s]+/).filter((d) => /^`?[A-Z0-9]/.test(d)).map((d) => d.replace(/^[` ]+|[` ]+$/g, "")),
      agent: cell("агент"),
      title: cell("задача"),
      criterion: cell("критерий готовности"),
      notes: cell("заметки"),
    });
  }
  return tasks;
}

function parseWhen(arg) {
  const first = (arg || "").split(/\s+/)[0];
  if (!/^\d{4}-\d{2}-\d{2}/.test(first)) return null;
  const iso = /([zZ]|[+-]\d{2}:?\d{2})$/.test(first) ? first : first + "Z"; // как autopilot: без зоны — UTC
  const when = new Date(iso);
  return isNaN(when) ? null : when;
}

// Обрезка ячейки без поломанной разметки: ссылки → текст, без жирного, парные обратные кавычки.
function short(text, limit) {
  let s = (text || "").replace(/\[([^\]]*)\]\([^)]*\)/g, "$1").replace(/\*\*/g, "").replace(/~~/g, "").trim();
  if (!s || s === "—") return "";
  if (s.length <= limit) return s;
  s = s.slice(0, limit).replace(/\s+\S*$/, "");
  if ((s.match(/`/g) || []).length % 2) s += "`";
  return s + " …";
}

const pad = (n) => String(n).padStart(2, "0");
const fmt = (d) => `${pad(d.getDate())}.${pad(d.getMonth() + 1)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;

async function decisionButton(task) {
  const path = `${DECISIONS}/${task.id}.md`;
  const exists = await app.vault.adapter.exists(path);
  const btn = document.createElement("button");
  btn.className = "okx-btn";
  btn.textContent = exists ? "Открыть ответ" : "Ответить";
  btn.onclick = async () => {
    if (!(await app.vault.adapter.exists(path))) {
      if (!(await app.vault.adapter.exists(DECISIONS))) await app.vault.createFolder(DECISIONS);
      const now = new Date();
      const stamp = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}`;
      const quote = (s) => (s || "—").split("\n").map((l) => "> " + l).join("\n");
      await app.vault.create(path, [
        "---",
        `task: ${task.id}`,
        `date: ${stamp}`,
        "status: новое",
        "---",
        `# Решение по ${task.id}`,
        "",
        `**Задача:** ${task.title}`,
        "",
        "**Вопрос с доски:**",
        "",
        quote(task.notes),
        "",
        "## Решение",
        "",
        "",
        "",
        "## Почему",
        "",
        "",
      ].join("\n"));
    }
    await app.workspace.openLinkText(path, "", true);
  };
  return btn;
}

try {
  const text = await app.vault.adapter.read(BOARD);
  const tasks = parseBoard(text);
  const now = new Date();
  const done = (id) => tasks.get(id)?.status === "done";
  const by = (status) => [...tasks.values()].filter((t) => t.status === status);

  const ready = by("ready");
  const readyFree = ready.filter((t) => t.deps.every(done));
  const scheduled = by("scheduled").map((t) => ({ ...t, when: parseWhen(t.arg) }))
    .sort((a, b) => (a.when?.getTime() ?? Infinity) - (b.when?.getTime() ?? Infinity));
  const due = scheduled.filter((t) => t.when && t.when <= now);
  const known = new Set(["needs-user", "blocked", "in-progress", "ready", "scheduled", "done"]);
  const other = [...tasks.values()].filter((t) => !known.has(t.status));

  const summary = [
    `🔴 needs-user **${by("needs-user").length}**`,
    `⛔ blocked **${by("blocked").length}**`,
    `🟡 in-progress **${by("in-progress").length}**`,
    `🟢 ready **${ready.length}** (можно брать ${readyFree.length})`,
    `🕒 scheduled **${scheduled.length}** (наступило ${due.length})`,
    `✅ done **${by("done").length}**`,
  ];
  if (other.length) summary.push(`❔ прочие **${other.length}**: ${other.map((t) => t.id).join(", ")}`);
  dv.el("div", summary.join(" · "), { cls: "okx-summary" });

  const show = new Set(opts.show);
  const section = (title) => dv.el("div", title, { cls: "okx-section" });

  if (show.has("needs-user")) {
    const rows = by("needs-user");
    if (rows.length) {
      section("🔴 Ждёт решения человека");
      const cells = [];
      for (const t of rows) cells.push([t.id, t.title, short(t.notes, Math.max(opts.notes, 500)), await decisionButton(t)]);
      dv.table(["ID", "Задача", "Вопрос и варианты", ""], cells);
    } else if (opts.show.length === 1) {
      dv.el("div", "Решений от человека сейчас не ждут.", { cls: "okx-muted" });
    }
  }
  if (show.has("blocked") && by("blocked").length) {
    section("⛔ Заблокировано");
    dv.table(["ID", "Задача", "Агент", "Причина"], by("blocked").map((t) => [t.id, t.title, t.agent, short(t.notes, opts.notes)]));
  }
  if (show.has("in-progress") && by("in-progress").length) {
    section("🟡 В работе");
    dv.table(["ID", "Задача", "Кто, с", "Критерий готовности"], by("in-progress").map((t) => [t.id, t.title, t.arg || t.agent, short(t.criterion, opts.notes)]));
  }
  if (show.has("ready") && ready.length) {
    if (readyFree.length) {
      section("🟢 Можно брать");
      dv.table(["ID", "Задача", "Агент"], readyFree.map((t) => [t.id, t.title, t.agent]));
    }
    const waiting = ready.filter((t) => !t.deps.every(done));
    if (waiting.length) {
      section("⏳ Ждут зависимостей");
      dv.table(["ID", "Задача", "Агент", "Ждёт"], waiting.map((t) => [t.id, t.title, t.agent, t.deps.filter((d) => !done(d)).join(", ")]));
    }
  }
  if (show.has("scheduled") && scheduled.length) {
    section("🕒 По расписанию");
    const when = (t) => {
      if (!t.when) return t.arg;
      if (t.when > now) return fmt(t.when);
      const waits = t.deps.filter((d) => !done(d));
      return waits.length ? `${fmt(t.when)} — наступило, ждёт ${waits.join(", ")}` : `⏰ **${fmt(t.when)}** — наступило`;
    };
    dv.table(["ID", "Задача", "Агент", "Когда"], scheduled.map((t) => [t.id, t.title, t.agent, when(t)]));
  }
  dv.el("div", `Источник — ${dv.fileLink(BOARD)}, прочитано в ${fmt(now)}`, { cls: "okx-muted" });
} catch (e) {
  dv.el("div", `Доска не прочитана: ${e}`, { cls: "okx-banner okx-danger" });
}
