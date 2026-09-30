"use strict";
const token = document.querySelector('meta[name="control-token"]').content;
const $ = id => document.getElementById(id);
let data = null;
let toastTimer = null;
let selectedTask = null;
let returnFocus = null;
let handoffMarkdown = "";
let selectedRunId = null;
const labels = {claude:"Claude Code",codex:"Codex",gemini:"Gemini CLI",muse:"Muse Code"};
function set(id, value){$(id).textContent = value == null ? "—" : String(value)}
function node(tag, cls, text){const n=document.createElement(tag);if(cls)n.className=cls;if(text!=null)n.textContent=String(text);return n}
function empty(target,text){target.replaceChildren(node("div","empty",text))}
function toast(message,error=false){const el=$("toast");el.textContent=message;el.className="toast show"+(error?" error":"");clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.className="toast",6000)}
function shortTime(value){if(!value)return "—";const d=new Date(value);return Number.isNaN(d.getTime())?String(value):d.toLocaleString("ru-RU",{day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"})}
function row(label,value,kind=""){const r=node("div","health-row");r.append(node("span","",label),node("strong",kind,value));return r}
function renderOverview(s){
  const b=s.board||{},c=b.counts||{},q=s.delegation||{},e=s.engine||{},risk=(s.snapshot||{}).risk||{},guard=((s.snapshot||{}).ops||{}).guard||{};
  set("updated","Обновлено "+shortTime(s.generated_at));set("engine-stat",e.state||"—");set("engine-detail",e.pid?"PID "+e.pid:"Процесс не указан");
  set("task-stat",c["in-progress"]||0);set("task-detail",`${b.ready_ids?.length||0} доступны · ${c["needs-user"]||0} ждут решения`);
  set("queue-stat",(q.counts||{}).inbox||0);set("queue-detail",`${(q.counts||{}).processing||0} в файловой стадии обработки · ${(q.counts||{}).outbox||0} завершены`);
  set("risk-stat",risk.equity==null?"—":Number(risk.equity).toLocaleString("ru-RU",{maximumFractionDigits:0}));set("risk-detail",risk.kill_active?"KILL активен":"Equity, USDT");
  const healthy=e.state==="жив"&&!risk.kill_active;set("system-state",healthy?"Система работает":e.state==="остановлен"?"Движок остановлен":"Проверьте состояние");
  set("system-subtitle",`${s.errors.length} ошибок чтения · ${b.duplicates?.length||0} неоднозначных ID`);
  set("engine-badge",e.state||"нет данных");$("engine-badge").className="pill "+(e.state==="жив"?"good":e.state==="остановлен"?"":"bad");
  set("engine-message",e.reason||"Нет данных сторожа");set("muse-badge",q.paused?"пауза":q.runner_pid_recorded?"PID записан":"не запущен");
  $("muse-badge").className="pill "+(q.paused?"bad":"");
  const nd=s.netdata||{};set("netdata-badge",nd.available?"ONLINE":"OFFLINE");
  $("netdata-badge").className="pill "+(nd.available?"good":"bad");
  set("netdata-message",nd.available?`Netdata ${nd.version||""} (${nd.os_name||""}): ${nd.cpu_cores||"?"} ядер CPU, алертов: ${nd.alarms_critical||0}`:`Netdata не запущен (${nd.error||"офлайн"}). Запуск: ops\\netdata.ps1 start`);
  document.querySelector('[data-action="engine.start"]').disabled=e.state==="жив";
  document.querySelector('[data-action="engine.stop"]').disabled=e.state==="остановлен";
  document.querySelector('[data-action="delegation.pause"]').disabled=Boolean(q.paused);
  document.querySelector('[data-action="delegation.stop"]').disabled=!q.runner_pid_recorded;
  set("muse-message",`В очереди ${(q.counts||{}).inbox||0}, в файловой стадии обработки ${(q.counts||{}).processing||0}. PID раннера требует проверки скриптом.`);
  const health=$("health-list");health.replaceChildren(
    row("Guard · отказы за 24 ч",guard.denies_24h??"нет данных",guard.denies_24h?"warn":"good"),
    row("Мониторинг Netdata",nd.available?`Онлайн (${nd.alarms_critical} кр.)`:"Офлайн",nd.available?"good":"warn"),
    row("KILL / breaker",risk.kill_active?"KILL":"без KILL",risk.kill_active?"warn":"good"),
    row("Тишина лога движка",e.log_age_s==null?"нет лога":Math.round(e.log_age_s)+" с",e.log_age_s>210?"warn":""),

    row("Ошибки движка",(s.snapshot?.engine||{}).errors_internal??"нет данных"),
    row("Раннер Muse",q.paused?"пауза":q.runner_pid_recorded?"PID записан · владелец не подтверждён":"PID нет"),
    row("Дубликаты ID на доске",b.duplicates?.length||0,b.duplicates?.length?"warn":"good")
  );set("health-badge",s.errors.length?"ЧАСТЬ ДАННЫХ НЕДОСТУПНА":"ДАННЫЕ ПОЛУЧЕНЫ");
}
function renderTasks(){const b=data?.board||{},tasks=b.tasks||[],text=$("task-search").value.trim().toLowerCase(),filter=$("task-filter").value;
  const shown=tasks.filter(t=>(filter==="all"||filter==="active"&&t.status!=="done"||t.status===filter)&&(!text||`${t.id} ${t.title} ${t.agent}`.toLowerCase().includes(text)));
  const body=$("tasks-body");body.replaceChildren();for(const t of shown){const tr=node("tr"),idCell=node("td"),open=node("button","task-open",t.id+(t.duplicate?" ⚠":""));open.type="button";open.addEventListener("click",()=>openTask(t.id,open));idCell.append(open);tr.append(idCell,node("td","",t.title),node("td","",t.agent));const status=node("td");status.append(node("span","status "+t.status,t.status+(t.status_detail?" · "+t.status_detail:"")));tr.append(status);body.append(tr)}
  set("tasks-count",shown.length+" / "+tasks.length);const warning=$("duplicates");warning.hidden=!b.duplicates?.length;warning.textContent=b.duplicates?.length?"Дублирующиеся ID: "+b.duplicates.join(", ")+". Не запускайте эти задачи без сверки доски.":"";
}
function closeTask(){if($("task-drawer").hidden)return;$("task-drawer").hidden=true;$("drawer-backdrop").hidden=true;document.body.style.overflow="";selectedTask=null;handoffMarkdown="";if(returnFocus?.isConnected)returnFocus.focus();else $("task-search").focus();returnFocus=null}
async function openTask(id,button){returnFocus=button;$('task-drawer').hidden=false;$('drawer-backdrop').hidden=false;document.body.style.overflow="hidden";$('drawer-close').focus();set("detail-id",id);set("detail-title","Загрузка…");set("detail-status","—");set("detail-agent","—");set("detail-criterion","—");set("detail-deps","—");set("detail-notes","—");$("plan-result").replaceChildren();$("launch-button").hidden=true;handoffMarkdown="";$("handoff-result").hidden=true;$("handoff-copy").hidden=true;
  try{const res=await fetch("/api/task?id="+encodeURIComponent(id),{headers:{"X-Control-Token":token},cache:"no-store"});const task=await res.json();if(!res.ok)throw Error(task.error||"HTTP "+res.status);if($("task-drawer").hidden||$("detail-id").textContent!==id)return;selectedTask=task;set("detail-title",task.title);set("detail-status",task.status+(task.status_detail?" · "+task.status_detail:""));$("detail-status").className="pill "+(task.status==="ready"?"good":task.status==="needs-user"?"bad":"");set("detail-agent",task.agent);set("detail-criterion",task.criterion||"—");set("detail-deps",task.deps?.length?task.deps.join(", "):"Нет");set("detail-notes",task.notes||"—");
    const roles=Object.keys(data?.roles||{}),select=$("plan-role");select.replaceChildren();for(const role of roles){const option=node("option","",role);option.value=role;select.append(option)}const suggested={"Insight Executor":"insight-executor","Crypto Insight Hunter":"crypto-insight-hunter","Project Orchestrator":"project-orchestrator","Ops Sentinel":"ops-sentinel","OKX Trader":"okx-trader","Pump Risk Taker":"pump-risk-taker"};const taskRole=task.agent.replace(/\s*\([^)]*\)\s*$/,"").trim();select.value=suggested[taskRole]||roles[0]||"";$("plan-agent").value="auto";
    if(!task.eligible){$("plan-result").append(node("div","plan-warning","Задача сейчас не готова к запуску: проверьте статус и зависимости."))}
  }catch(err){set("detail-title","Карточка недоступна");$("plan-result").append(node("div","plan-warning",err.message));toast(err.message,true)}
}
async function prepareHandoff(){if(!selectedTask)return;const id=selectedTask.id,button=$("handoff-button");button.disabled=true;set("handoff-result","Формируем снимок…");$("handoff-result").hidden=false;$("handoff-copy").hidden=true;try{const response=await fetch("/api/handoff?id="+encodeURIComponent(id),{headers:{"X-Control-Token":token},cache:"no-store"});const packet=await response.json();if(!response.ok)throw Error(packet.error||"HTTP "+response.status);if($("task-drawer").hidden||selectedTask?.id!==id)return;handoffMarkdown=packet.markdown;set("handoff-result",handoffMarkdown);$("handoff-copy").hidden=false}catch(err){set("handoff-result","Не удалось сформировать пакет: "+err.message);toast(err.message,true)}finally{button.disabled=false}}
async function copyHandoff(){if(!handoffMarkdown)return;try{await navigator.clipboard.writeText(handoffMarkdown);toast("Пакет передачи скопирован")}catch(err){toast("Буфер обмена недоступен. Выделите текст пакета вручную.",true)}}
async function planTask(){if(!selectedTask)return;const button=$("plan-button"),result=$("plan-result");button.disabled=true;$("launch-button").hidden=true;result.replaceChildren(node("div","subtle","Проверяем доступные CLI…"));try{const response=await fetch("/api/agent/plan",{method:"POST",headers:{"Content-Type":"application/json","X-Control-Token":token},body:JSON.stringify({task_id:selectedTask.id,role:$("plan-role").value,agent:$("plan-agent").value})});const plan=await response.json();if(!response.ok)throw Error(plan.error||"HTTP "+response.status);result.replaceChildren();result.append(node("div",plan.eligible?"plan-summary":"plan-warning",(plan.selected?"Выбран: "+(labels[plan.selected]||plan.selected):"Доступный CLI не найден")+" · "+plan.reason));$("launch-button").hidden=!plan.launch_enabled;for(const c of plan.candidates||[]){const line=node("div","candidate"),info=node("div"),label=labels[c.agent]||c.agent;info.append(node("strong","",label),node("small","",(c.model||"модель не задана")+" · guard: "+(c.guard_status||"не проверен")));line.append(info,node("span","",c.available&&!c.skipped?"CLI найден":"CLI недоступен"));result.append(line)}}catch(err){result.replaceChildren(node("div","plan-warning",err.message));toast(err.message,true)}finally{button.disabled=false}}
function renderRuntimes(s){const box=$("runtime-grid");box.replaceChildren();for(const id of ["codex","claude","gemini","muse"]){const r=s.runtimes?.[id]||{},card=node("div","runtime");card.append(node("div","name",labels[id]),node("div","model",r.default_model||"модель не задана"),node("div","guard","Guard: "+(r.guard_status||"не проверен")),node("div","guard",(s.capabilities?.[id]||[]).includes("start")?"Запуск через панель доступен":"Запуск через панель закрыт"));const evidence=s.guard_observation?.clients?.[id];card.append(node("div","guard",evidence?("Отказы с ID клиента в хвосте журнала: "+evidence.denies_in_tail):"Данные guard недоступны"));box.append(card)}}
function renderInventory(s){const processes=$("process-list");processes.replaceChildren();for(const p of s.processes||[]){const el=node("div","event"),left=node("div");left.append(node("strong","",p.name),node("small","","PID записан: "+p.pid+" · владелец не подтверждён"));el.append(left,node("time","",p.alive===true?"процесс жив":p.alive===false?"не найден":"статус неизвестен"));processes.append(el)}if(!processes.children.length)empty(processes,"PID-файлов нет");
  const worktrees=$("worktree-list");worktrees.replaceChildren();
  const pathKey=path=>String(path||"").replaceAll("\\","/").toLowerCase().replace(/\/$/,"");
  const leases=s.worktree_leases||[],leaseByPath=new Map(leases.map(l=>[pathKey(l.worktree),l]));
  for(const w of s.worktrees||[]){
    const el=node("div","event"),left=node("div"),lease=leaseByPath.get(pathKey(w.worktree));
    left.append(node("strong","",(w.branch||"detached").replace("refs/heads/","")),node("small","",w.worktree||""));
    left.append(node("small","",lease?`Lease панели: ${lease.task_id} · ${lease.run_id} · ${lease.state}${lease.stale?" · heartbeat устарел · нужна сверка":""} · владелец процесса не подтверждён`:"Lease панели: нет · использование другими агентами неизвестно"));
    el.append(left,node("time","",String(w.HEAD||"").slice(0,8)));worktrees.append(el)
  }
  for(const lease of leases.filter(l=>!l.registered)){
    const el=node("div","event"),left=node("div");
    left.append(node("strong","",lease.task_id+" · "+lease.run_id),node("small","",lease.worktree||""),node("small","","Lease требует сверки: worktree не зарегистрирован"));
    el.append(left,node("time","",lease.state||"unknown"));worktrees.append(el)
  }
  if(!worktrees.children.length)empty(worktrees,"Рабочих копий не найдено")}
function renderEvents(s){const rotation=$("rotation-list");rotation.replaceChildren();for(const item of (s.rotation||[]).slice(0,12)){const el=node("div","event"),left=node("div");left.append(node("strong","",`${labels[item.agent]||item.agent||"Агент"} · ${item.event||"событие"}`),node("small","",`${item.task||"без ID"} · ${item.role||"роль не указана"}`));el.append(left,node("time","",shortTime(item.ts)));rotation.append(el)}if(!rotation.children.length)empty(rotation,"Запусков в журнале нет");
  renderMuseJobs(s)}
function renderMuseJobs(s){const results=$("muse-results"),filter=$("muse-filter").value;results.replaceChildren();const names={queued:"ожидает","processing-unverified":"в обработке · не подтверждено",done:"заявка завершена",error:"ошибка",timeout:"таймаут",unknown:"неизвестно"};for(const job of s.delegation?.jobs||[]){if(filter!=="all"&&!(filter==="finished"?["done","error","timeout"].includes(job.state):job.state===filter))continue;const el=node("div","event"),left=node("div");left.append(node("strong","",job.id||"Muse"),node("small","",`${names[job.state]||names.unknown} · ${job.role||"роль не указана"} · от ${job.from||"не указано"}`),node("small","",job.provenance||"источник не указан"));if(job.elapsed_s!=null||job.exit_code!=null){const metrics=[];if(job.elapsed_s!=null)metrics.push("Время: "+Math.round(job.elapsed_s)+" с");if(job.exit_code!=null)metrics.push("Код выхода: "+job.exit_code);metrics.push("Стоимость: нет данных");left.append(node("small","",metrics.join(" · ")))}const when=job.finished||job.created;el.append(left,node("time","",when?shortTime(when):"—"));results.append(el)}if(!results.children.length)empty(results,"Заявок этой стадии нет")}
function renderRuns(s){
  const claims=(s.board?.tasks||[]).filter(t=>t.status==="in-progress");
  const claimList=$("claims-list");claimList.replaceChildren();
  for(const t of claims){
    const entry=node("div","event"),left=node("div"),open=node("button","task-open",t.id);
    open.type="button";open.addEventListener("click",()=>openTask(t.id,open));
    left.append(open,node("small","",t.title+" · "+t.agent),
      node("small","",("Назначение: "+(t.status_detail||"в работе"))+" · источник: ops/board.md · процесс не проверен"));
    entry.append(left);claimList.append(entry)
  }
  if(!claims.length)empty(claimList,"Активных claims на доске нет");
  const list=$("runs-list");list.replaceChildren();
  const runs=s.runs||[],caps=s.capabilities||{};
  set("runs-count",claims.length+" В РАБОТЕ · "+runs.length+" ЗАПИСЕЙ");
  const statuses={running:"PID найден · владелец не подтверждён",unknown:"исход неизвестен · нужна сверка",
    completed:"завершён",failed:"ошибка",cancelled:"отменён",queued:"в очереди",
    "processing-unverified":"файл в обработке · процесс не подтверждён",done:"заявка завершена",
    error:"ошибка",timeout:"таймаут"};
  for(const r of runs){
    const entry=node("div","event"),left=node("div"),right=node("div");
    const source=r.source_kind==="muse-queue"?"Очередь Muse":"Реестр панели";
    left.append(node("strong","",r.id+" · "+(labels[r.runtime]||r.runtime||"агент")),
      node("small","",(r.task_id?"Задача: "+r.task_id+" · ":"")+
        "Роль: "+(r.role||"—")+" · "+source),
      node("small","",(r.provenance||"источник не указан")+" · "+
        (r.state_quality==="file-stage"?"файловая стадия":r.state_quality==="pid-only"?"PID-проверка без подтверждения владельца":"локальная запись")));
    const metrics=[];
    if(r.elapsed_s!=null)metrics.push("Время: "+Math.round(r.elapsed_s)+" с");
    if(r.exit_code!=null)metrics.push("Код выхода: "+r.exit_code);
    metrics.push("Стоимость: "+(r.cost?.usd==null?"нет данных":r.cost.usd+" USD"));
    left.append(node("small","",metrics.join(" · ")));
    const pillCls=["completed","done"].includes(r.status)?"good":
      ["unknown","failed","error","timeout"].includes(r.status)?"bad":"";
    right.append(node("span","pill "+pillCls,statuses[r.status]||r.status));
    if(r.managed){const eventsButton=node("button","soft","События");eventsButton.type="button";eventsButton.addEventListener("click",()=>loadRunEvents(r.id));right.append(eventsButton)}
    const canCancel=r.managed&&(caps[r.runtime]||[]).includes("cancel");
    if(r.status==="running"&&canCancel){
      const button=node("button","soft","Отмена");
      button.addEventListener("click",()=>cancelRun(r.id,button));right.append(button)
    }
    const stamp=node("time","",shortTime(r.started_at));stamp.style.marginLeft="8px";
    right.append(stamp);entry.append(left,right);list.append(entry)
  }
  if(!runs.length)empty(list,"Записей запусков и заявок пока нет")
}
async function loadRunEvents(runId,after=0,append=false){
  if(!runId)return;
  selectedRunId=runId;
  const detail=$("run-detail"),list=$("run-event-list");
  detail.hidden=false;set("run-detail-title",runId);
  if(!append)empty(list,"Загружаем события…");
  try{
    const url="/api/run/events?id="+encodeURIComponent(runId)+"&after="+after;
    const response=await fetch(url,{headers:{"X-Control-Token":token},cache:"no-store"});
    const packet=await response.json();
    if(!response.ok)throw Error(packet.error||"HTTP "+response.status);
    if(selectedRunId!==runId)return;
    if(!append)list.replaceChildren();
    else list.querySelector(".events-more")?.remove();
    for(const event of packet.events||[]){
      const entry=node("div","event"),name=node("strong","",event.type||"событие");
      entry.append(name,node("time","",shortTime(event.ts)+" · #"+event.cursor));
      list.append(entry)
    }
    if(!list.children.length)empty(list,"Событий после выбранного курсора нет");
    if(packet.has_more){
      const more=node("button","soft events-more","Следующие события");
      more.type="button";
      more.addEventListener("click",()=>loadRunEvents(runId,packet.next_cursor,true));
      list.append(more)
    }
  }catch(err){if(selectedRunId===runId)empty(list,"Не удалось загрузить события: "+err.message)}
}
async function cancelRun(runId,button){if(!confirm(`Отменить запуск ${runId}?`))return;button.disabled=true;try{const res=await fetch("/api/run/cancel",{method:"POST",headers:{"Content-Type":"application/json","X-Control-Token":token},body:JSON.stringify({run_id:runId})});const result=await res.json();if(!res.ok||!result.ok)throw Error(result.error||"Не удалось отменить запуск");toast("Запуск "+runId+" отменён");await refresh()}catch(err){toast(err.message,true)}finally{button.disabled=false}}
async function launchAgent(){if(!selectedTask)return;const button=$("launch-button");if(!confirm(`Запустить агента для задачи ${selectedTask.id}?`))return;button.disabled=true;try{const response=await fetch("/api/runs",{method:"POST",headers:{"Content-Type":"application/json","X-Control-Token":token},body:JSON.stringify({task_id:selectedTask.id,role:$("plan-role").value,agent:$("plan-agent").value})});const result=await response.json();if(!response.ok||!result.ok)throw Error(result.error||"Не удалось запустить агента");toast("Запуск "+result.run.id+" успешно создан");closeTask();await refresh()}catch(err){toast(err.message,true)}finally{button.disabled=false}}
function renderLog(){set("log-output",(data?.logs?.[$("log-select").value]||[]).join("\n")||"Нет записей")}
async function refresh(){try{const res=await fetch("/api/state",{headers:{"X-Control-Token":token},cache:"no-store"});if(!res.ok)throw Error("HTTP "+res.status);data=await res.json();renderOverview(data);renderTasks();renderRuntimes(data);renderRuns(data);renderInventory(data);renderEvents(data);renderLog()}catch(err){toast("Не удалось обновить данные: "+err.message,true);set("updated","Нет связи")}}
async function runAction(name,button){const names={"engine.start":"запустить demo-движок","engine.stop":"остановить движок","delegation.pause":"поставить очередь Muse на паузу","delegation.stop":"остановить runner Muse"};if(!confirm("Подтвердите действие: "+names[name]+"?"))return;button.disabled=true;try{const res=await fetch("/api/action",{method:"POST",headers:{"Content-Type":"application/json","X-Control-Token":token},body:JSON.stringify({action:name})});const result=await res.json();if(!res.ok||!result.ok)throw Error(result.error||result.output||"Команда завершилась с ошибкой");toast(result.output||"Действие выполнено");await refresh()}catch(err){toast(err.message,true)}finally{button.disabled=false;if(data)renderOverview(data)}}
$("run-detail-close").addEventListener("click",()=>{selectedRunId=null;$("run-detail").hidden=true});$("run-detail-refresh").addEventListener("click",()=>loadRunEvents(selectedRunId));$("refresh").addEventListener("click",refresh);$("task-search").addEventListener("input",renderTasks);$("task-filter").addEventListener("change",renderTasks);$("muse-filter").addEventListener("change",()=>renderMuseJobs(data||{}));$("log-select").addEventListener("change",renderLog);$("drawer-close").addEventListener("click",closeTask);$("drawer-backdrop").addEventListener("click",closeTask);document.addEventListener("keydown",event=>{if($("task-drawer").hidden)return;if(event.key==="Escape")closeTask();if(event.key==="Tab"){const controls=[...$("task-drawer").querySelectorAll("button:not(:disabled),select:not(:disabled)")],first=controls[0],last=controls.at(-1);if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus()}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus()}}});$("plan-button").addEventListener("click",planTask);$("launch-button").addEventListener("click",launchAgent);$("handoff-button").addEventListener("click",prepareHandoff);$("handoff-copy").addEventListener("click",copyHandoff);document.querySelectorAll("[data-action]").forEach(b=>b.addEventListener("click",()=>runAction(b.dataset.action,b)));refresh();setInterval(refresh,10000);
