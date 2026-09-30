// Живая панель из снимка data/obsidian/status.json (src/obsidian_status.py, схема v2; v1 тоже понимается):
// движок и P1-72H, дежурство (live-окно, сторож, guard), риск против лимитов, памп-карман, график equity.
// Только чтение файлов, без сети и без биржи.
// Кнопка «Обновить» запускает `python -m src.obsidian_status` (только чтение) — работает в Obsidian для ПК.
// Вызов: await dv.view("notes/views/live", { auto: true })   — на пульте: снимок старше AUTO_MIN обновится сам
//        await dv.view("notes/views/live", { compact: true }) — в дневнике: без графика и счётчиков

const adapter = dv.app.vault.adapter;
const SNAPSHOT = "data/obsidian/status.json";
const STALE_MIN = 15; // снимок старше — плашка «устарел»
const AUTO_MIN = 5; // { auto: true }: снимок старше — обновить при открытии
const opts = (typeof input === "object" && input) || {};

const pad = (n) => String(n).padStart(2, "0");
const fmt = (d) => `${pad(d.getDate())}.${pad(d.getMonth() + 1)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const num = (v, digits = 2) => (v === null || v === undefined ? "—" : Number(v).toLocaleString("ru-RU", { maximumFractionDigits: digits }));
const banner = (cls, html) => `<div class="okx-banner ${cls}">${html}</div>`;

// Шкала «значение против лимита»: зелёная до 50%, оранжевая до 80%, дальше красная.
function meter(label, used, limit, text) {
  const pct = limit > 0 && used !== null && used !== undefined ? Math.min(100, Math.max(0, (used / limit) * 100)) : 0;
  const cls = pct >= 80 ? "okx-danger" : pct >= 50 ? "okx-warn" : "okx-ok";
  return `<div class="okx-meter-row"><span class="okx-meter-label">${esc(label)}</span>`
    + `<span class="okx-meter"><span class="okx-meter-fill ${cls}" style="width:${pct.toFixed(1)}%"></span></span>`
    + `<span class="okx-meter-text">${text}</span></div>`;
}

// График equity: отрезается всё до последнего скачка > 20% между соседними точками (пополнение demo).
function chart(history, hwm) {
  if (!Array.isArray(history) || history.length < 2) return `<div class="okx-muted">История equity пуста.</div>`;
  let start = 0;
  for (let i = 1; i < history.length; i++) {
    if (Math.abs(history[i][1] / history[i - 1][1] - 1) > 0.2) start = i;
  }
  const pts = history.slice(start);
  if (pts.length < 2) return `<div class="okx-muted">После пополнения demo ещё мало точек для графика.</div>`;
  const W = 640, H = 150, P = 6;
  const t0 = pts[0][0], t1 = pts[pts.length - 1][0];
  const vals = pts.map((p) => p[1]);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const showHwm = hwm && hwm >= lo && hwm <= hi * 1.02;
  if (showHwm) hi = Math.max(hi, hwm);
  if (hi === lo) { hi += 1; lo -= 1; }
  const x = (t) => P + ((t - t0) / Math.max(1, t1 - t0)) * (W - 2 * P);
  const y = (v) => P + (1 - (v - lo) / (hi - lo)) * (H - 2 * P);
  const line = pts.map((p) => `${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(" ");
  const first = vals[0], last = vals[vals.length - 1];
  const change = (last / first - 1) * 100;
  return `<svg class="okx-chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Equity">`
    + (showHwm ? `<line class="okx-chart-hwm" x1="${P}" x2="${W - P}" y1="${y(hwm).toFixed(1)}" y2="${y(hwm).toFixed(1)}"/>` : "")
    + `<polyline class="okx-chart-line" points="${line}"/></svg>`
    + `<div class="okx-muted">${fmt(new Date(t0 * 1000))} — ${fmt(new Date(t1 * 1000))} · min ${num(lo, 0)} · max ${num(hi, 0)} · `
    + `за период ${change > 0 ? "+" : ""}${num(change)}%${showHwm ? " · пунктир — пик (HWM)" : ""}${start ? " · точки до пополнения demo скрыты" : ""}</div>`;
}

function engineHtml(e, compact) {
  if (!e) return banner("okx-warn", "⚙️ Раздел движка не прочитан — причина в ошибках снимка ниже.");
  let html;
  if (e.running === true) html = banner("okx-ok", `⚙️ <b>Движок работает</b> (PID ${e.pid}), аптайм ${num(e.uptime_h, 1)} ч.`);
  else if (e.running === false) html = banner("okx-danger", `⚙️ <b>Движок не работает</b>${e.pid ? ` — PID ${e.pid} из <code>data/engine.pid</code> остался от упавшего процесса` : ""}. Запуск: <code>ops\\engine.ps1 start</code>.`);
  else html = banner("okx-warn", "⚙️ Жив ли движок, проверить не удалось.");
  if (e.p1_progress_pct !== null && e.p1_progress_pct !== undefined) {
    const deadline = e.p1_deadline ? fmt(new Date(e.p1_deadline)) : "—";
    html += `<div class="okx-meter-row"><span class="okx-meter-label">P1-72H</span>`
      + `<span class="okx-meter"><span class="okx-meter-fill okx-info" style="width:${Math.min(100, e.p1_progress_pct)}%"></span></span>`
      + `<span class="okx-meter-text">${num(e.p1_progress_pct, 1)}% · осталось ${num(e.p1_hours_left, 1)} ч · дедлайн ${deadline}</span></div>`;
  }
  if (!compact) {
    const bad = (e.divergences || 0) + (e.errors || 0) + (e.pauses || 0) > 0;
    html += `<div class="${bad ? "okx-banner okx-warn" : "okx-muted"}">Сверок ${num(e.reconciles, 0)} · расхождений ${num(e.divergences, 0)} · `
      + `ошибок ${num(e.errors, 0)} (биржа ${num(e.errors_exchange, 0)}, внутренних ${num(e.errors_internal, 0)}) · пауз ${num(e.pauses, 0)} · `
      + `реконнекты WS ${num(e.ws_public_reconnects, 0)}/${num(e.ws_private_reconnects, 0)}</div>`;
  }
  if (e.running && e.stats_age_s > 600) html += banner("okx-warn", `Статистика движка не сохранялась ${Math.round(e.stats_age_s / 60)} мин.`);
  return html;
}

function riskHtml(r) {
  if (!r) return banner("okx-warn", "🛡 Раздел риска не прочитан — причина в ошибках снимка ниже.");
  let html = "";
  if (r.kill_active) html += banner("okx-danger", "🛑 <b>Kill-switch риск-ядра активен.</b> Сброс — только человек: <code>python -m src.ops reset …</code>.");
  if (r.global_breaker) html += banner("okx-danger", `🛑 <b>Сработал breaker просадки</b> (${r.global_dd_limit_pct}%).`);
  if (r.daily_breaker) html += banner("okx-danger", `🛑 <b>Сработал дневной breaker</b> (${r.daily_limit_pct}%).`);
  const dayEq = r.day_start_equity ? ((r.equity / r.day_start_equity - 1) * 100) : null;
  html += `<div class="okx-summary">Equity <b>${num(r.equity)}</b> USDT · пик ${num(r.hwm)}`
    + (dayEq !== null ? ` · за день ${dayEq > 0 ? "+" : ""}${num(dayEq)}%` : "")
    + ` · входов сегодня ${num(r.entries_today, 0)} из ${num(r.max_entries_per_day, 0)}</div>`;
  const dd = Math.max(0, -(r.drawdown_pct || 0));
  const dayLoss = Math.max(0, -(r.day_pnl_pct || 0));
  html += meter("Просадка", dd, r.global_dd_limit_pct, `${num(dd)}% из ${r.global_dd_limit_pct}%`);
  html += meter("Убыток дня", dayLoss, r.daily_limit_pct, `${num(dayLoss)}% из ${r.daily_limit_pct}% (PnL дня ${num(r.day_pnl)} USDT)`);
  html += meter("Риск позиций", r.portfolio_heat_pct, r.max_heat_pct, `${num(r.portfolio_heat_pct)}% из ${r.max_heat_pct}%`);
  if (r.equity_age_s > 900) html += banner("okx-warn", `Equity в риск-ядре не обновлялась ${Math.round(r.equity_age_s / 60)} мин.`);
  return html;
}

function opsHtml(o) {
  if (!o) return ""; // снимок v1: секции ops нет
  let html = "";
  const live = o.live || {};
  if (live.open === true) {
    const until = live.until ? fmt(new Date(live.until)) : "—";
    html += banner("okx-live", `💸 <b>Live-окно открыто</b> до ${until} (осталось ~${num(live.hours_left, 1)} ч): live-процессы вправе торговать реальными деньгами.`);
  } else if (live.open === false) {
    html += `<div class="okx-muted">🧪 Live-окно закрыто — торговля только на demo.</div>`;
  }
  const p = o.live_pocket || {};
  if (p.exists === false) {
    html += `<div class="okx-muted">Live-кармана нет (<code>ops/live-pocket.json</code>) — live-runner не стартует.</div>`;
  } else if (p.exists === true) {
    if (p.example) html += banner("okx-warn", `💸 В <code>ops/live-pocket.json</code> лежит шаблон (<code>example: true</code>) — runner его не примет, нужен карман человека.`);
    else html += `<div class="okx-muted">Live-карман: бюджет ${num(p.budget_usdt)} USDT · рукава: ${esc((p.sleeves || []).join(", ") || "—")}.</div>`;
  }
  if (o.autostart_off) html += banner("okx-warn", `⏸ <b>Автозапуск на паузе</b> (<code>data/AUTOSTART_OFF</code>) — сторож и старт при входе отключены, простой после перезагрузки никто не подберёт.`);
  const w = o.watch || {};
  if (w.log_exists === false) {
    html += `<div class="okx-muted">Сторож ещё ни разу не писал в <code>logs/autostart.log</code> — задача «OKX-Bot Watchdog» не зарегистрирована (ENGINE-AUTOSTART-REG).</div>`;
  } else if (w.last_age_s !== null && w.last_age_s !== undefined) {
    const min = Math.round(w.last_age_s / 60);
    if (w.last_age_s > 900) html += banner("okx-warn", `👁 <b>Сторож молчит ${min} мин</b> (последняя строка: ${esc((w.last_line || "").slice(0, 140))}).`);
    else html += `<div class="okx-muted">👁 Сторож: последняя проверка ${min} мин назад.</div>`;
  }
  if (o.engine_log_age_s !== null && o.engine_log_age_s !== undefined && o.engine_log_age_s > 180) {
    html += banner("okx-warn", `⚙️ <b>Лог движка молчит ${Math.round(o.engine_log_age_s / 60)} мин</b> — вероятно, движок остановлен. Проверка: <code>ops\\engine.ps1 status</code>.`);
  }
  const g = o.guard || {};
  if (g.denies_24h) {
    html += `<div class="okx-muted">🛡 Guard за 24 ч: блокировок ${num(g.denies_24h, 0)}`
      + `${g.last_deny_at ? ` (последняя ${fmt(new Date(g.last_deny_at))})` : ""}`
      + `${g.errors_24h ? ` · сбоев хука ${num(g.errors_24h, 0)}` : ""}.</div>`;
  } else if (g.errors_24h) {
    html += banner("okx-warn", `🛡 Guard: сбоев самого хука за 24 ч — ${num(g.errors_24h, 0)} (смотри <code>data/guard.log</code>).`);
  }
  return html;
}

function pumpHtml(p) {
  if (!p) return banner("okx-warn", "🎯 Раздел памп-кармана не прочитан — причина в ошибках снимка ниже.");
  let html = p.entry_allowed
    ? banner("okx-ok", "🎯 Памп-карман: вход разрешён.")
    : banner("okx-warn", `🎯 <b>Памп-карман: входов нет</b> — ${esc((p.blocks || []).join("; ") || "причина не указана")}.`);
  html += meter("В позициях", p.in_positions, p.budget_total, `${num(p.in_positions)} из ${num(p.budget_total)} USDT (свободно ${num(p.budget_free)})`);
  html += meter("Убыток дня", Math.max(0, -(p.day_pnl || 0)), p.day_limit, `${num(p.day_pnl)} USDT, лимит −${num(p.day_limit)}`);
  html += meter("Просадка", p.drawdown, p.drawdown_limit, `${num(p.drawdown)} из ${num(p.drawdown_limit)} USDT`);
  const open = p.open_positions || [];
  if (open.length) {
    html += `<ul class="okx-list">${open.map((o) => `<li><b>${esc(o.pair)}</b> ${num(o.size, 4)} на ${num(o.cost_usdt)} USDT · `
      + (o.stop ? `стоп ${num(o.stop, 6)}` : `<span class="okx-bad">без стопа</span>`) + `</li>`).join("")}</ul>`;
  }
  return html;
}

async function refresh() {
  const req = typeof window !== "undefined" && window.require ? window.require : null;
  if (!req) throw new Error("кнопка работает только в Obsidian для ПК; в терминале: python -m src.obsidian_status");
  const { execFile } = req("child_process");
  const fs = req("fs");
  const path = req("path");
  const base = adapter.getBasePath();
  const win = path.join(base, ".venv", "Scripts", "python.exe");
  const py = fs.existsSync(win) ? win : path.join(base, ".venv", "bin", "python");
  await new Promise((resolve, reject) => execFile(py, ["-m", "src.obsidian_status"], { cwd: base, timeout: 60000, windowsHide: true },
    (err, out, errOut) => (err ? reject(new Error(String(errOut || err.message).trim())) : resolve(out))));
}

async function refreshAndRerender(btn) {
  if (globalThis.__okxRefreshing) return;
  globalThis.__okxRefreshing = true;
  if (btn) { btn.disabled = true; btn.textContent = "⏳ Обновляю…"; }
  try {
    await refresh();
    dv.app.commands.executeCommandById("dataview:dataview-force-refresh-views");
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = "🔄 Обновить"; }
    dv.el("div", `Снимок не обновлён: ${e.message || e}`, { cls: "okx-banner okx-danger" });
  } finally {
    globalThis.__okxRefreshing = false;
  }
}

try {
  const now = Date.now();
  const text = (await adapter.exists(SNAPSHOT)) ? await adapter.read(SNAPSHOT) : null;
  const s = text ? JSON.parse(text) : null;
  const ageMin = s ? Math.floor((now / 1000 - s.generated_ts) / 60) : null;

  const btn = dv.el("button", "🔄 Обновить", { cls: "okx-btn" });
  btn.addEventListener("click", () => refreshAndRerender(btn));

  if (!s) {
    dv.el("div", `Снимка <code>${SNAPSHOT}</code> ещё нет — нажми «Обновить» или выполни <code>python -m src.obsidian_status</code>.`, { cls: "okx-banner okx-warn" });
  } else if (s.v !== 1 && s.v !== 2) {
    dv.el("div", `Снимок версии v${s.v}, а панель понимает v1–v2 — обнови <code>notes/views/live.js</code>.`, { cls: "okx-banner okx-danger" });
  } else {
    const box = dv.el("div", "", { cls: "okx-panel" });
    let html = `<div class="${ageMin >= STALE_MIN ? "okx-banner okx-warn" : "okx-muted"}">Снимок ${fmt(new Date(s.generated_ts * 1000))} (${ageMin} мин назад)`
      + `${ageMin >= STALE_MIN ? " — <b>устарел</b>, нажми «Обновить»" : ""}. Точные цифры — <code>python -m src.ops status</code>.</div>`;
    html += engineHtml(s.engine, opts.compact);
    const ops = opsHtml(s.ops);
    if (ops) html += `<div class="okx-section">Дежурство</div>` + ops;
    html += `<div class="okx-section">Риск</div>` + riskHtml(s.risk);
    html += `<div class="okx-section">Памп-карман</div>` + pumpHtml(s.pump);
    if (!opts.compact) html += `<div class="okx-section">Equity за 48 ч</div>` + chart(s.equity_history, s.risk && s.risk.hwm);
    if (s.errors && s.errors.length) html += banner("okx-warn", `Ошибки снимка: ${s.errors.map(esc).join("; ")}`);
    box.innerHTML = html;
  }
  if (opts.auto && (ageMin === null || ageMin >= AUTO_MIN)) refreshAndRerender(btn);
} catch (e) {
  dv.el("div", `Живая панель не построена: ${e}`, { cls: "okx-banner okx-danger" });
}
