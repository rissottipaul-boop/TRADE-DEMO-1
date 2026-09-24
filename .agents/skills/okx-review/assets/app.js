        const BOOTSTRAP = document.getElementById("bootstrap");
        const DATA = JSON.parse(BOOTSTRAP.dataset.positions);
        const ALL_TAGS = JSON.parse(BOOTSTRAP.dataset.allTags);
        let tagFilter = null; // 当前筛选的标签名（点击 chip 激活）

        function groupByMonth(rows) {
          const m = {};
          for (const r of rows) {
            if (!r.closed_at) continue;
            const k = r.closed_at.slice(0, 7);
            m[k] = (m[k] || 0) + (r.realized_pnl || 0);
          }
          const keys = Object.keys(m).sort();
          return { labels: keys, values: keys.map((k) => m[k]) };
        }
        function cumulative(rows) {
          const sorted = [...rows].sort((a, b) =>
            (a.closed_at || "").localeCompare(b.closed_at || "")
          );
          let acc = 0;
          const labels = [],
            values = [];
          for (const r of sorted) {
            acc += r.realized_pnl || 0;
            labels.push((r.closed_at || "").slice(0, 10));
            values.push(+acc.toFixed(4));
          }
          return { labels, values };
        }
        const cum = cumulative(DATA);
        new Chart(document.getElementById("cumChart"), {
          type: "line",
          data: {
            labels: cum.labels,
            datasets: [
              {
                label: "累计 PnL",
                data: cum.values,
                borderColor: "#4f8cff",
                backgroundColor: "rgba(79,140,255,0.15)",
                fill: true,
                tension: 0.2,
                pointRadius: 0,
              },
            ],
          },
          options: {
            scales: {
              x: { ticks: { color: "#8a93a0" }, grid: { color: "#262b33" } },
              y: { ticks: { color: "#8a93a0" }, grid: { color: "#262b33" } },
            },
            plugins: { legend: { display: false } },
          },
        });
        const mo = groupByMonth(DATA);
        new Chart(document.getElementById("monthChart"), {
          type: "bar",
          data: {
            labels: mo.labels,
            datasets: [
              {
                label: "月度 PnL",
                data: mo.values,
                backgroundColor: mo.values.map((v) =>
                  v >= 0 ? "#10b981" : "#ef4444"
                ),
              },
            ],
          },
          options: {
            scales: {
              x: { ticks: { color: "#8a93a0" }, grid: { color: "#262b33" } },
              y: { ticks: { color: "#8a93a0" }, grid: { color: "#262b33" } },
            },
            plugins: { legend: { display: false } },
          },
        });

        // Table
        const DEFAULT_SORT_KEY = "closed_at",
          DEFAULT_SORT_DIR = -1;
        let sortKey = DEFAULT_SORT_KEY,
          sortDir = DEFAULT_SORT_DIR,
          sortUserSet = false,
          page = 1;
        const sym = document.getElementById("filterSymbol");
        const dir = document.getElementById("filterDir");
        const pnlF = document.getElementById("filterPnl");
        const tagF = document.getElementById("filterTag");
        const noteF = document.getElementById("filterNote");
        const pageSize = document.getElementById("pageSize");

        function filtered() {
          return DATA.filter((r) => {
            if (
              sym.value &&
              !(r.symbol || "").toUpperCase().includes(sym.value.toUpperCase())
            )
              return false;
            if (dir.value && r.direction !== dir.value) return false;
            if (pnlF.value === "win" && !((r.realized_pnl || 0) > 0))
              return false;
            if (pnlF.value === "loss" && !((r.realized_pnl || 0) < 0))
              return false;
            const tc = (r.tags || []).length;
            if (tagF.value === "__has__" && tc === 0) return false;
            if (tagF.value === "__none__" && tc > 0) return false;
            if (tagFilter && !(r.tags || []).some((t) => t.name === tagFilter))
              return false;
            if (noteF.value === "__has__" && !r.has_reflection) return false;
            if (noteF.value === "__none__" && r.has_reflection) return false;
            return true;
          }).sort((a, b) => {
            const av = a[sortKey],
              bv = b[sortKey];
            if (av == null) return 1;
            if (bv == null) return -1;
            if (typeof av === "number" && typeof bv === "number")
              return (av - bv) * sortDir;
            return String(av).localeCompare(String(bv)) * sortDir;
          });
        }
        function fmtNum(v, d = 4) {
          return v == null ? "-" : Number(v).toFixed(d);
        }
        function fmtTime(v) {
          return v ? v.replace("T", " ").slice(5, 16) : "-";
        }
        function fmtNotional(v) {
          const n = Number(v);
          if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
          if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
          return n.toFixed(0);
        }
        function fmtFee(v) {
          if (v == null) return "-";
          return Math.abs(v) < 1 ? Number(v).toFixed(2) : Number(v).toFixed(0);
        }
        function fmtCapital(notional, lev) {
          if (notional == null) return "-";
          const base = fmtNotional(notional);
          return lev == null ? base : `${base} · ${lev}x`;
        }
        function renderTags(tags) {
          if (!tags || !tags.length) return "";
          return `<span class="chips">${tags
            .map((t) => {
              const color = t.color || "";
              const style = color
                ? ` style="background:${escapeHtml(color)}"`
                : "";
              const dc = ` data-color="${escapeHtml(color)}"`;
              return `<span class="chip"${style}${dc}>${escapeHtml(
                t.name
              )}</span>`;
            })
            .join("")}</span>`;
        }
        function escapeHtml(s) {
          return String(s).replace(
            /[&<>"']/g,
            (c) =>
              ({
                "&": "&amp;",
                "<": "&lt;",
                ">": "&gt;",
                '"': "&quot;",
                "'": "&#39;",
              }[c])
          );
        }
        function render() {
          const rows = filtered();
          const ps = +pageSize.value;
          const pages = Math.max(1, Math.ceil(rows.length / ps));
          if (page > pages) page = pages;
          const start = (page - 1) * ps;
          const slice = rows.slice(start, start + ps);
          const tb = document.getElementById("tbody");
          tb.innerHTML = slice
            .map((r) => {
              const pnlCls = (r.realized_pnl || 0) >= 0 ? "pnl-pos" : "pnl-neg";
              const note = r.has_reflection
                ? '<span class="note-mark" title="已有复盘">📝</span>'
                : "";
              return `<tr class="row" data-id="${r.id}" draggable="true"
        hx-get="/positions/${
          r.id
        }" hx-target="#detail-panel" hx-swap="innerHTML">
      <td>${fmtTime(r.closed_at)}</td>
      <td>${escapeHtml(r.symbol || "-")}${note}</td>
      <td>${escapeHtml(r.direction || "-")}</td>
      <td>${fmtCapital(r.notional_usd, r.leverage)}</td>
      <td class="${pnlCls}">${fmtNum(r.realized_pnl, 2)}</td>
      <td>${fmtFee(r.total_fee)}</td>
      <td>${renderTags(r.tags)}</td>
    </tr>`;
            })
            .join("");
          // process HTMX for newly injected rows
          if (window.htmx) htmx.process(tb);
          document.getElementById(
            "tableCount"
          ).textContent = `${rows.length} 条`;
          document.getElementById(
            "pageInfo"
          ).textContent = ` ${page} / ${pages} `;
          document.getElementById("prev").disabled = page <= 1;
          document.getElementById("next").disabled = page >= pages;
        }
        for (const el of [sym, dir, pnlF, tagF, noteF, pageSize])
          el.addEventListener("input", () => {
            page = 1;
            render();
          });
        document.getElementById("prev").addEventListener("click", () => {
          page--;
          render();
        });
        document.getElementById("next").addEventListener("click", () => {
          page++;
          render();
        });
        function updateSortIndicators() {
          document.querySelectorAll("th[data-key]").forEach((th) => {
            th.classList.remove("sort-asc", "sort-desc");
            if (sortUserSet && th.dataset.key === sortKey) {
              th.classList.add(sortDir === 1 ? "sort-asc" : "sort-desc");
            }
          });
        }
        document.querySelectorAll("th[data-key]").forEach((th) => {
          th.addEventListener("click", () => {
            const k = th.dataset.key;
            // 三态循环：降序 → 升序 → 回默认
            if (!sortUserSet || sortKey !== k) {
              sortKey = k;
              sortDir = -1;
              sortUserSet = true;
            } else if (sortDir === -1) {
              sortDir = 1;
            } else {
              sortKey = DEFAULT_SORT_KEY;
              sortDir = DEFAULT_SORT_DIR;
              sortUserSet = false;
            }
            updateSortIndicators();
            render();
          });
        });

        // Highlight selected row + open drawer after detail load
        document.body.addEventListener("htmx:afterSwap", (e) => {
          if (e.detail.target.id === "detail-panel") {
            const id =
              e.detail.requestConfig?.path?.match(/positions\/(\d+)/)?.[1];
            document
              .querySelectorAll("tr.row.selected")
              .forEach((r) => r.classList.remove("selected"));
            if (id) {
              document
                .querySelector(`tr.row[data-id="${id}"]`)
                ?.classList.add("selected");
              document.body.classList.add("drawer-open");
            }
          }
          syncDataFromDetail();
        });

        // Drawer form POST/DELETE (tags, reflection) replace .detail outerHTML — sync DATA back
        function syncDataFromDetail() {
          const detail = document.querySelector("#detail-panel .detail");
          if (!detail) return;
          const idMatch = detail
            .querySelector(".sub")
            ?.textContent.match(/#(\d+)/);
          if (!idMatch) return;
          const pid = +idMatch[1];
          const row = DATA.find((r) => r.id === pid);
          if (!row) return;
          const chips = detail.querySelectorAll(".tag-row .chip");
          row.tags = [...chips].map((c) => {
            const clone = c.cloneNode(true);
            clone.querySelectorAll("button").forEach((b) => b.remove());
            // 读 data-color 拿 hex 原值；c.style.backgroundColor 会被浏览器序列化为 rgb(),
            // 一旦回流到 attachTag → POST /tags 就会污染数据库里的 tag.color 格式
            return {
              name: clone.textContent.trim(),
              color: c.dataset.color || null,
            };
          });
          row.has_reflection = !!detail.querySelector(".refl-meta");
          renderTagStrip();
          renderTagSummary();
          render();
        }

        function closeDrawer() {
          document.body.classList.remove("drawer-open");
          document
            .querySelectorAll("tr.row.selected")
            .forEach((r) => r.classList.remove("selected"));
        }
        document
          .querySelector(".drawer-close")
          .addEventListener("click", closeDrawer);
        document
          .getElementById("drawerBackdrop")
          .addEventListener("click", closeDrawer);
        document.addEventListener("keydown", (e) => {
          if (
            e.key === "Escape" &&
            document.body.classList.contains("drawer-open")
          )
            closeDrawer();
        });

        // Charts collapse — remember state
        const chartsCollapse = document.getElementById("chartsCollapse");
        if (localStorage.getItem("okx-charts-collapsed") === "1")
          chartsCollapse.removeAttribute("open");
        chartsCollapse.addEventListener("toggle", () => {
          localStorage.setItem(
            "okx-charts-collapsed",
            chartsCollapse.open ? "0" : "1"
          );
        });

        // Tag summary collapse — remember state
        const tagSummaryCollapse =
          document.getElementById("tagSummaryCollapse");
        if (localStorage.getItem("okx-tag-summary-collapsed") === "1")
          tagSummaryCollapse.removeAttribute("open");
        tagSummaryCollapse.addEventListener("toggle", () => {
          localStorage.setItem(
            "okx-tag-summary-collapsed",
            tagSummaryCollapse.open ? "0" : "1"
          );
        });

        // ---- Tag summary board ----
        let tagSort = { key: "pnl", dir: -1 };
        function computeTagSummary() {
          const taggedTotal =
            DATA.filter((r) => (r.tags || []).length > 0).length || 1;
          const byName = new Map();
          // 预置所有已注册标签，保证未使用的标签也能出现在汇总里（便于编辑/删除）
          for (const t of ALL_TAGS)
            byName.set(t.name, {
              name: t.name,
              color: t.color,
              count: 0,
              pnl: 0,
              fee: 0,
              wins: 0,
              losses: 0,
              winSum: 0,
              lossSum: 0,
            });
          for (const r of DATA) {
            for (const t of r.tags || []) {
              if (!byName.has(t.name))
                byName.set(t.name, {
                  name: t.name,
                  color: t.color,
                  count: 0,
                  pnl: 0,
                  fee: 0,
                  wins: 0,
                  losses: 0,
                  winSum: 0,
                  lossSum: 0,
                });
              const s = byName.get(t.name);
              s.count += 1;
              const p = r.realized_pnl || 0;
              s.pnl += p;
              s.fee += r.total_fee || 0;
              if (p > 0) {
                s.wins += 1;
                s.winSum += p;
              } else if (p < 0) {
                s.losses += 1;
                s.lossSum += p;
              }
            }
          }
          return [...byName.values()].map((s) => {
            const avgWin = s.wins ? s.winSum / s.wins : 0;
            const avgLoss = s.losses ? Math.abs(s.lossSum) / s.losses : 0;
            return {
              ...s,
              share: s.count / taggedTotal,
              winRate: s.count ? s.wins / s.count : 0,
              avgPnl: s.count ? s.pnl / s.count : 0,
              plRatio: avgWin > 0 && avgLoss > 0 ? avgWin / avgLoss : null,
            };
          });
        }
        function fmtSignedNum(v, d = 0) {
          if (v == null || Number.isNaN(v)) return "—";
          return (v >= 0 ? "+" : "") + Number(v).toFixed(d);
        }
        function renderTagSummary() {
          const rows = computeTagSummary();
          const tb = document.getElementById("tagSummaryBody");
          const empty = document.getElementById("tagSummaryEmpty");
          if (!rows.length) {
            tb.innerHTML = "";
            empty.style.display = "block";
            return;
          }
          empty.style.display = "none";
          rows.sort((a, b) => {
            if (tagSort.key === "rank") return 0; // rank follows current sort of other cols; treat as stable
            const av = a[tagSort.key],
              bv = b[tagSort.key];
            if (av == null && bv == null) return 0;
            if (av == null) return 1;
            if (bv == null) return -1;
            if (typeof av === "number") return (av - bv) * tagSort.dir;
            return String(av).localeCompare(String(bv)) * tagSort.dir;
          });
          tb.innerHTML = rows
            .map((s, i) => {
              const pnlCls = s.pnl >= 0 ? "pnl-pos" : "pnl-neg";
              const avgCls = s.avgPnl >= 0 ? "pnl-pos" : "pnl-neg";
              const active = tagFilter === s.name ? "selected" : "";
              const shareW = Math.max(2, Math.min(100, s.share * 100)).toFixed(
                0
              );
              const tagMeta = ALL_TAGS.find((t) => t.name === s.name);
              const tagId = tagMeta ? tagMeta.id : "";
              return `<tr class="ts-row ${active}" data-tag="${escapeHtml(
                s.name
              )}" data-tag-id="${tagId}">
      <td>${i + 1}</td>
      <td><span class="ts-dot" style="background:${
        s.color || "#8a93a0"
      }"></span>${escapeHtml(s.name)}</td>
      <td>${s.count}</td>
      <td>${(s.share * 100).toFixed(
        0
      )}%<span class="ts-share-bar"><span style="width:${shareW}%"></span></span></td>
      <td>${(s.winRate * 100).toFixed(0)}%</td>
      <td class="${pnlCls}">${fmtSignedNum(s.pnl, 0)}</td>
      <td class="${avgCls}">${fmtSignedNum(s.avgPnl, 1)}</td>
      <td>${s.plRatio == null ? "—" : s.plRatio.toFixed(2)}</td>
      <td>${fmtFee(s.fee)}</td>
      <td class="ts-actions-cell">${
        tagId
          ? `<span class="ts-actions">
        <button type="button" class="ts-edit" title="改名 / 改色">编辑</button>
        <button type="button" class="ts-delete danger" title="删除此标签（会从所有交易上移除）">删</button>
      </span>`
          : ""
      }</td>
    </tr>`;
            })
            .join("");
          document
            .querySelectorAll("table.tag-summary th[data-skey]")
            .forEach((th) => {
              th.classList.remove("sort-asc", "sort-desc");
              if (th.dataset.skey === tagSort.key)
                th.classList.add(tagSort.dir === 1 ? "sort-asc" : "sort-desc");
            });
        }
        document
          .querySelectorAll("table.tag-summary th[data-skey]")
          .forEach((th) => {
            th.addEventListener("click", () => {
              const k = th.dataset.skey;
              if (k === "rank") return;
              if (tagSort.key === k) tagSort.dir = -tagSort.dir;
              else {
                tagSort.key = k;
                tagSort.dir = k === "name" ? 1 : -1;
              }
              renderTagSummary();
            });
          });
        document
          .getElementById("tagSummaryBody")
          .addEventListener("click", (e) => {
            const editBtn = e.target.closest("button.ts-edit");
            const delBtn = e.target.closest("button.ts-delete");
            const tr = e.target.closest("tr.ts-row");
            if (!tr) return;
            if (editBtn) {
              e.stopPropagation();
              enterEditMode(tr);
              return;
            }
            if (delBtn) {
              e.stopPropagation();
              confirmDeleteTag(tr);
              return;
            }
            if (e.target.closest(".ts-edit-form")) return; // 编辑态下点 input 不触发筛选
            const name = tr.dataset.tag;
            tagFilter = tagFilter === name ? null : name;
            page = 1;
            renderTagStrip();
            renderTagSummary();
            render();
          });

        function enterEditMode(tr) {
          const tagId = tr.dataset.tagId;
          if (!tagId) return;
          const meta = ALL_TAGS.find((t) => String(t.id) === tagId);
          const curName = tr.dataset.tag;
          const curColor = (meta && meta.color) || "#4f8cff";
          // 替换第 2 列（标签名）和操作列为编辑表单
          const nameCell = tr.children[1];
          const actionsCell = tr.children[tr.children.length - 1];
          nameCell.innerHTML = `<span class="ts-edit-form">
    <input type="color" class="ts-edit-color" value="${curColor}" />
    <input type="text" class="ts-edit-name" value="${escapeHtml(
      curName
    )}" maxlength="32" />
  </span>`;
          actionsCell.innerHTML = `<span class="ts-actions">
    <button type="button" class="ts-save">保存</button>
    <button type="button" class="ts-cancel">取消</button>
  </span>`;
          const input = nameCell.querySelector(".ts-edit-name");
          input.focus();
          input.select();
          const save = async () => {
            const newName = input.value.trim();
            const newColor = nameCell.querySelector(".ts-edit-color").value;
            if (!newName) {
              input.focus();
              return;
            }
            const fd = new FormData();
            fd.append("name", newName);
            fd.append("color", newColor);
            try {
              const res = await fetch(`/tags/${tagId}`, {
                method: "PATCH",
                body: fd,
              });
              if (!res.ok) {
                const txt = await res.text();
                alert(`改标签失败: ${res.status} ${txt}`);
                return;
              }
              const j = await res.json();
              applyTagRename(j.old_name, j.tag.name, j.tag.color);
            } catch (err) {
              alert("请求失败: " + err.message);
            }
          };
          actionsCell.querySelector(".ts-save").addEventListener("click", save);
          actionsCell
            .querySelector(".ts-cancel")
            .addEventListener("click", () => renderTagSummary());
          input.addEventListener("keydown", (ev) => {
            if (ev.key === "Enter") {
              ev.preventDefault();
              save();
            } else if (ev.key === "Escape") renderTagSummary();
          });
        }

        function applyTagRename(oldName, newName, color) {
          // 更新 ALL_TAGS
          const meta = ALL_TAGS.find((t) => t.name === oldName);
          if (meta) {
            meta.name = newName;
            meta.color = color;
          }
          // 更新 DATA 里所有 position 的 tags
          for (const r of DATA) {
            for (const t of r.tags || []) {
              if (t.name === oldName) {
                t.name = newName;
                t.color = color;
              }
            }
          }
          // 保持筛选态一致
          if (tagFilter === oldName) tagFilter = newName;
          renderTagStrip();
          renderTagSummary();
          render();
        }

        async function confirmDeleteTag(tr) {
          const tagId = tr.dataset.tagId;
          const name = tr.dataset.tag;
          if (!tagId) return;
          if (
            !confirm(
              `删除标签「${name}」？\n该标签会从所有已打标签的交易上移除，无法撤销。`
            )
          )
            return;
          try {
            const res = await fetch(`/tags/${tagId}`, { method: "DELETE" });
            if (!res.ok) {
              alert(`删除失败: ${res.status}`);
              return;
            }
            // 本地移除
            const idx = ALL_TAGS.findIndex(
              (t) => String(t.id) === String(tagId)
            );
            if (idx >= 0) ALL_TAGS.splice(idx, 1);
            for (const r of DATA) {
              if (r.tags) r.tags = r.tags.filter((t) => t.name !== name);
            }
            if (tagFilter === name) tagFilter = null;
            renderTagStrip();
            renderTagSummary();
            render();
          } catch (err) {
            alert("请求失败: " + err.message);
          }
        }

        document
          .getElementById("tsNewTagForm")
          .addEventListener("submit", async (e) => {
            e.preventDefault();
            const nameInput = document.getElementById("tsNewTagName");
            const colorInput = document.getElementById("tsNewTagColor");
            const name = nameInput.value.trim();
            if (!name) {
              nameInput.focus();
              return;
            }
            if (ALL_TAGS.some((t) => t.name === name)) {
              alert("已有同名标签");
              return;
            }
            const fd = new FormData();
            fd.append("name", name);
            fd.append("color", colorInput.value);
            try {
              const res = await fetch("/tags", { method: "POST", body: fd });
              if (!res.ok) {
                alert(`新建失败: ${res.status}`);
                return;
              }
              const j = await res.json();
              ALL_TAGS.push(j.tag);
              nameInput.value = "";
              renderTagStrip();
              renderTagSummary();
            } catch (err) {
              alert("请求失败: " + err.message);
            }
          });

        // ---- Tag stats strip + drag & drop + click-to-filter ----
        function computeTagStats() {
          // 合并 ALL_TAGS 和数据里出现过的标签（防止 user 自己加的临时 tag 漏掉）
          const byName = new Map();
          for (const t of ALL_TAGS)
            byName.set(t.name, {
              name: t.name,
              color: t.color,
              count: 0,
              pnl: 0,
              wins: 0,
            });
          for (const r of DATA) {
            for (const t of r.tags || []) {
              if (!byName.has(t.name))
                byName.set(t.name, {
                  name: t.name,
                  color: t.color,
                  count: 0,
                  pnl: 0,
                  wins: 0,
                });
              const s = byName.get(t.name);
              s.count += 1;
              s.pnl += r.realized_pnl || 0;
              if ((r.realized_pnl || 0) > 0) s.wins += 1;
            }
          }
          return [...byName.values()];
        }
        function renderTagStrip() {
          const stats = computeTagStats();
          const strip = document.getElementById("tagStrip");
          const label = strip.querySelector(".label");
          // 保留 label，清空其余
          [...strip.querySelectorAll(".tag-stat, .clear-filter")].forEach((n) =>
            n.remove()
          );
          for (const s of stats) {
            const el = document.createElement("div");
            el.className = "tag-stat" + (tagFilter === s.name ? " active" : "");
            el.dataset.tag = s.name;
            el.dataset.color = s.color || "";
            el.draggable = true;
            const wr =
              s.count > 0 ? ((s.wins / s.count) * 100).toFixed(0) + "%" : "—";
            const pnlCls = s.pnl >= 0 ? "pos" : "neg";
            const pnlTxt =
              s.count > 0 ? (s.pnl >= 0 ? "+" : "") + s.pnl.toFixed(0) : "—";
            el.innerHTML = `
      <span class="dot" style="background:${s.color || "#8a93a0"}"></span>
      <span class="name">${escapeHtml(s.name)}</span>
      <span class="meta">${
        s.count
      } · ${wr} · <span class="pnl ${pnlCls}">${pnlTxt}</span></span>`;
            // click: toggle filter
            el.addEventListener("click", () => {
              tagFilter = tagFilter === s.name ? null : s.name;
              page = 1;
              renderTagStrip();
              renderTagSummary();
              render();
            });
            // drop target (row -> tag)
            el.addEventListener("dragover", (e) => {
              if (!e.dataTransfer.types.includes("text/position-id")) return;
              e.preventDefault();
              el.classList.add("drop-over");
            });
            el.addEventListener("dragleave", () =>
              el.classList.remove("drop-over")
            );
            el.addEventListener("drop", async (e) => {
              e.preventDefault();
              el.classList.remove("drop-over");
              const pid = e.dataTransfer.getData("text/position-id");
              if (!pid) return;
              await attachTag(pid, s.name, s.color);
            });
            // drag source (tag -> row)
            el.addEventListener("dragstart", (e) => {
              e.dataTransfer.setData("text/tag-name", s.name);
              if (s.color) e.dataTransfer.setData("text/tag-color", s.color);
              e.dataTransfer.effectAllowed = "copy";
              el.classList.add("dragging");
              document.body.classList.add("tag-dragging");
            });
            el.addEventListener("dragend", () => {
              el.classList.remove("dragging");
              document.body.classList.remove("tag-dragging");
            });
            strip.appendChild(el);
          }
          if (tagFilter) {
            const clr = document.createElement("button");
            clr.type = "button";
            clr.className = "clear-filter";
            clr.style.cssText =
              "background:none;border:none;cursor:pointer;color:var(--muted);font-size:11px;margin-left:4px;padding:0;";
            clr.textContent = "清除标签筛选";
            clr.addEventListener("click", () => {
              tagFilter = null;
              renderTagStrip();
              renderTagSummary();
              render();
            });
            strip.appendChild(clr);
          }
        }

        async function attachTag(pid, name, color) {
          const fd = new FormData();
          fd.append("name", name);
          if (color) fd.append("color", color);
          try {
            const res = await fetch(`/positions/${pid}/tags`, {
              method: "POST",
              body: fd,
            });
            if (!res.ok) {
              alert("打标签失败: " + res.status);
              return;
            }
            // 本地更新 DATA，避免整页刷新
            const row = DATA.find((r) => r.id == pid);
            if (row) {
              row.tags = row.tags || [];
              if (!row.tags.some((t) => t.name === name))
                row.tags.push({ id: null, name, color });
            }
            renderTagStrip();
            renderTagSummary();
            render();
          } catch (e) {
            alert("请求失败: " + e.message);
          }
        }

        // 行拖拽：记录 position id
        const tbody = document.getElementById("tbody");
        tbody.addEventListener("dragstart", (e) => {
          const tr = e.target.closest("tr.row");
          if (!tr) return;
          e.dataTransfer.setData("text/position-id", tr.dataset.id);
          e.dataTransfer.effectAllowed = "copy";
          tr.classList.add("dragging");
        });
        tbody.addEventListener("dragend", (e) => {
          const tr = e.target.closest("tr.row");
          if (tr) tr.classList.remove("dragging");
        });

        // 标签拖到行上
        tbody.addEventListener("dragover", (e) => {
          if (!e.dataTransfer.types.includes("text/tag-name")) return;
          e.preventDefault();
          const tr = e.target.closest("tr.row");
          tbody.querySelectorAll("tr.row.drop-row").forEach((r) => {
            if (r !== tr) r.classList.remove("drop-row");
          });
          if (tr) tr.classList.add("drop-row");
        });
        tbody.addEventListener("dragleave", (e) => {
          const tr = e.target.closest("tr.row");
          if (tr && !tr.contains(e.relatedTarget))
            tr.classList.remove("drop-row");
        });
        tbody.addEventListener("drop", async (e) => {
          const name = e.dataTransfer.getData("text/tag-name");
          if (!name) return;
          e.preventDefault();
          const tr = e.target.closest("tr.row");
          tbody
            .querySelectorAll("tr.row.drop-row")
            .forEach((r) => r.classList.remove("drop-row"));
          if (!tr) return;
          const color = e.dataTransfer.getData("text/tag-color") || null;
          await attachTag(tr.dataset.id, name, color);
        });

        renderTagStrip();
        renderTagSummary();
        render();
