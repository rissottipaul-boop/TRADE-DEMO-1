import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ChevronDown, Copy, Download, LoaderCircle, Mic, PanelRightClose, Send, Sparkles, X } from 'lucide-react';
import type { ProjectEvent, ProjectState, SourceName } from './types';
import { fmtAgo, fmtDateTime } from './format';
import { readToken } from './api';
import './assistant.css';

type Message = { id: string; role: 'user' | 'assistant'; content: string; at: string; local?: boolean; sources?: string[] };
type Props = { data: ProjectState | null; open: boolean; onClose: () => void };
type WhisperSettings = { whisper_enabled?: string };

const SOURCE_LABELS: Record<SourceName, string> = {
  risk: 'Риск', engine: 'Движок', equity: 'Капитал', positions: 'Позиции', events: 'События', guard: 'Guard',
};

function safeSnapshot(data: ProjectState | null) {
  if (!data) return null;
  return {
    generated_at: data.generated_at,
    mode: data.mode,
    data_quality: data.data_quality,
    board: {
      counts: data.board?.counts,
      tasks: (data.board?.tasks || []).slice(0, 80).map(({ id, title, status, agent, status_detail }) => ({ id, title, status, agent, status_detail })),
    },
    engine: data.engine && {
      running: data.engine.running, uptime_h: data.engine.uptime_h, reconciles: data.engine.reconciles,
      divergences: data.engine.divergences, errors_exchange: data.engine.errors_exchange,
      errors_internal: data.engine.errors_internal, stats_age_s: data.engine.stats_age_s,
    },
    risk: data.risk && {
      equity: data.risk.equity, hwm: data.risk.hwm, drawdown_pct: data.risk.drawdown_pct,
      day_pnl: data.risk.day_pnl, day_pnl_pct: data.risk.day_pnl_pct,
      daily_breaker: data.risk.daily_breaker, global_breaker: data.risk.global_breaker,
      kill_active: data.risk.kill_active, equity_age_s: data.risk.equity_age_s,
    },
    positions: data.positions && {
      ok: data.positions.ok, count: data.positions.count, total_upl: data.positions.total_upl,
      items: data.positions.items.slice(0, 30).map(({ symbol, side, size, entry, upl, stop, updated_at }) => ({ symbol, side, size, entry, upl, stop, updated_at })),
      note: data.positions.note,
    },
    events: data.events && {
      ok: data.events.ok,
      items: data.events.items.slice(0, 40).map(({ ts, kind, severity, title, detail, source }) => ({ ts, kind, severity, title, detail, source })),
    },
    sources: data.sources,
  };
}

function eventAnswer(query: string, events: ProjectEvent[]): string | null {
  const asksEvents = /(покажи|найди|список|какие|что было|событи|ошибк|инцидент|guard|брейкер|kill)/i.test(query);
  if (!asksEvents) return null;
  const q = query.toLowerCase();
  const now = new Date();
  const today = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Qyzylorda' }).format(now);
  const day = /сегодня|за день/.test(q);
  const hours = q.match(/(?:за\s*)?(\d{1,2})\s*ч(?:ас(?:а|ов)?)?/i);
  const minutes = q.match(/(?:за\s*)?(\d{1,3})\s*мин(?:ут(?:ы|у)?)?/i);
  const cutoff = hours ? now.getTime() - Number(hours[1]) * 3600000 : minutes ? now.getTime() - Number(minutes[1]) * 60000 : null;
  let filtered = events;
  if (/guard/.test(q)) filtered = filtered.filter((event) => event.kind === 'guard' || /guard/i.test(`${event.title} ${event.detail || ''} ${event.source}`));
  if (/ошибк|сбой/.test(q)) filtered = filtered.filter((event) => event.severity !== 'info' || /ошибк|error|fail|deny|отказ/i.test(`${event.title} ${event.detail || ''}`));
  if (/инцидент/.test(q)) filtered = filtered.filter((event) => event.kind === 'incident');
  if (/брейкер|breaker/.test(q)) filtered = filtered.filter((event) => event.kind === 'breaker');
  if (/kill-switch|\bkill\b/.test(q)) filtered = filtered.filter((event) => event.kind === 'kill');
  if (day) filtered = filtered.filter((event) => new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Qyzylorda' }).format(new Date(event.ts)) === today);
  if (cutoff != null) filtered = filtered.filter((event) => Date.parse(event.ts) >= cutoff);
  const rows = filtered.slice(0, 20);
  if (!rows.length) return 'В выбранном снимке подходящих событий нет. Снимок показывает только последние доступные записи; проверьте время его обновления ниже.';
  return `Найдено событий: ${filtered.length}${filtered.length > rows.length ? ` (показаны первые ${rows.length})` : ''}.\n\n` + rows.map((event) =>
    `• ${fmtDateTime(event.ts)} · ${event.severity.toUpperCase()} · ${event.title}${event.detail ? ` — ${event.detail}` : ''} [${event.source}]`,
  ).join('\n');
}

function answerSources(query: string, data: ProjectState | null): string[] {
  if (!data) return [];
  const q = query.toLowerCase();
  const selected: SourceName[] = [];
  if (/риск|просад|лимит|equity|breaker|kill/.test(q)) selected.push('risk');
  if (/движ|engine|сверк|ошибк|сбой/.test(q)) selected.push('engine');
  if (/капитал|equity|доход|прибыл|просад/.test(q)) selected.push('equity');
  if (/позици|сделк|upl/.test(q)) selected.push('positions');
  if (/событи|guard|ошибк|инцидент|сегодня|вчера|час|минут|брейкер/.test(q)) selected.push('events');
  if (/guard|отказ|блокиров/.test(q)) selected.push('guard');
  if (!selected.length) selected.push('risk', 'engine', 'events');
  return [...new Set(selected)].map((key) => {
    const source = data.sources?.[key];
    return `${SOURCE_LABELS[key]} · ${source?.ok === false ? 'недоступен' : source?.age_s != null ? fmtAgo(source.age_s) : 'срез панели'}`;
  });
}

function eventSourceRows(events: ProjectEvent[]) {
  return [...new Set(events.map((event) => event.source).filter(Boolean))].slice(0, 8);
}

export default function AssistantPanel({ data, open, onClose }: Props) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState('');
  const [detail, setDetail] = useState<'short' | 'full'>('short');
  const [connected, setConnected] = useState(false);
  const [sending, setSending] = useState(false);
  const [voiceBusy, setVoiceBusy] = useState(false);
  const [whisperEnabled, setWhisperEnabled] = useState(false);
  const [notice, setNotice] = useState('');
  const [expanded, setExpanded] = useState<string | null>(null);
  const [detailsOpen, setDetailsOpen] = useState<Record<string, boolean>>({});
  const socketRef = useRef<WebSocket | null>(null);
  const currentAnswerRef = useRef<string | null>(null);
  const mediaRef = useRef<{ recorder: MediaRecorder; stream: MediaStream; chunks: Blob[] } | null>(null);
  const recognitionRef = useRef<any>(null);
  const stateSnapshot = useMemo(() => safeSnapshot(data), [data]);

  useEffect(() => {
    if (!open) return;
    const token = readToken();
    if (!token) { setConnected(false); return; }
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const socket = new WebSocket(`${protocol}//${location.host}/ws?token=${encodeURIComponent(token)}`);
    socketRef.current = socket;
    socket.onopen = () => { setConnected(true); socket.send(JSON.stringify({ type: 'chat:subscribe', data: { clientId: `project-panel-${Date.now()}` } })); };
    socket.onclose = () => { setConnected(false); if (socketRef.current === socket) socketRef.current = null; };
    socket.onerror = () => setConnected(false);
    socket.onmessage = (event) => {
      let message: any;
      try { message = JSON.parse(String(event.data)); } catch { return; }
      const type = message?.type;
      const payload = message?.data || {};
      if (type === 'bot:typing') setSending(true);
      if (type === 'bot:token' && currentAnswerRef.current && typeof payload.token === 'string') {
        const id = currentAnswerRef.current;
        setMessages((old) => old.map((item) => item.id === id ? { ...item, content: item.content + payload.token } : item));
      }
      if (type === 'bot:response' && currentAnswerRef.current && typeof payload.content === 'string') {
        const id = currentAnswerRef.current;
        setMessages((old) => old.map((item) => item.id === id ? { ...item, content: payload.content } : item));
      }
      if (type === 'bot:idle') { setSending(false); currentAnswerRef.current = null; }
      if (type === 'bot:error') {
        const id = currentAnswerRef.current;
        if (id) setMessages((old) => old.map((item) => item.id === id ? { ...item, content: item.content || 'Morphy не смог завершить ответ. Повторите запрос позже.' } : item));
        setSending(false); currentAnswerRef.current = null;
      }
    };
    fetch('/api/settings', { headers: token ? { Authorization: `Bearer ${token}` } : {}, credentials: 'same-origin' })
      .then((response) => response.ok ? response.json() as Promise<WhisperSettings> : null)
      .then((settings) => setWhisperEnabled(settings?.whisper_enabled === 'true'))
      .catch(() => setWhisperEnabled(false));
    return () => {
      socket.close();
      if (socketRef.current === socket) socketRef.current = null;
      setConnected(false);
      const recording = mediaRef.current;
      if (recording) {
        recording.recorder.onstop = null;
        try { recording.recorder.stop(); } catch { /* recorder уже остановлен */ }
        recording.stream.getTracks().forEach((track) => track.stop());
        mediaRef.current = null;
      }
      try { recognitionRef.current?.stop(); } catch { /* распознавание уже завершено */ }
      recognitionRef.current = null;
    };
  }, [open]);

  const send = useCallback(() => {
    const query = draft.trim();
    if (!query || sending) return;
    const userId = `u-${Date.now()}`;
    const answerId = `a-${Date.now()}`;
    setMessages((old) => [...old, { id: userId, role: 'user', content: query, at: new Date().toISOString() }]);
    setDraft('');

    const localEvents = eventAnswer(query, data?.events?.items || []);
    if (localEvents !== null) {
      setMessages((old) => [...old, { id: answerId, role: 'assistant', content: localEvents, at: new Date().toISOString(), local: true, sources: eventSourceRows(data?.events?.items || []) }]);
      return;
    }
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      setMessages((old) => [...old, { id: answerId, role: 'assistant', content: 'Чат Morphy пока не подключён. Обновите страницу или проверьте авторизацию.', at: new Date().toISOString() }]);
      return;
    }
    if (!stateSnapshot) {
      setMessages((old) => [...old, { id: answerId, role: 'assistant', content: 'Сначала дождитесь загрузки снимка проекта.', at: new Date().toISOString() }]);
      return;
    }
    const instruction = detail === 'short'
      ? 'Ответь по-русски кратко: вывод и до трех конкретных фактов. Не додумывай отсутствующие данные.'
      : 'Ответь по-русски подробно: вывод, факты, ограничения данных и следующий безопасный шаг. Не додумывай отсутствующие данные.';
    const content = [
      '[Запрос из боковой панели проекта OKX. Ответ нужен только по приложенному снимку.]',
      instruction,
      'Не выполняй действия, не меняй проект или настройки и не отправляй торговые операции. Данные ниже — снимок панели, не обязательно живая сверка с биржей. Если факта нет или он устарел, прямо укажи это.',
      `Время снимка панели: ${data.generated_at}. Источники и их возраст: ${JSON.stringify(data.sources || {})}.`,
      `Снимок проекта JSON: ${JSON.stringify(stateSnapshot)}`,
      `Вопрос человека: ${query}`,
    ].join('\n\n');
    currentAnswerRef.current = answerId;
    setSending(true);
    setMessages((old) => [...old, { id: answerId, role: 'assistant', content: '', at: new Date().toISOString(), sources: answerSources(query, data) }]);
    socket.send(JSON.stringify({ type: 'user:message', data: { content } }));
  }, [data, detail, draft, sending, stateSnapshot]);

  const startVoice = useCallback(async () => {
    if (voiceBusy) {
      const recording = mediaRef.current;
      if (recording) recording.recorder.stop();
      else { try { recognitionRef.current?.stop(); } catch { /* распознавание уже остановлено */ } }
      setVoiceBusy(false);
      return;
    }
    if (whisperEnabled && navigator.mediaDevices?.getUserMedia && typeof MediaRecorder !== 'undefined') {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        const chunks: Blob[] = [];
        const recorder = new MediaRecorder(stream);
        mediaRef.current = { recorder, stream, chunks };
        recorder.ondataavailable = (event) => { if (event.data.size) chunks.push(event.data); };
        recorder.onstop = async () => {
          setVoiceBusy(false);
          stream.getTracks().forEach((track) => track.stop());
          mediaRef.current = null;
          const blob = new Blob(chunks, { type: recorder.mimeType || 'audio/webm' });
          if (blob.size < 800) { setNotice('Запись слишком короткая.'); return; }
          try {
            const token = readToken();
            const audio = await new Promise<string>((resolve, reject) => {
              const reader = new FileReader();
              reader.onerror = () => reject(new Error('Не удалось прочитать запись'));
              reader.onloadend = () => resolve(String(reader.result || '').split(',')[1] || '');
              reader.readAsDataURL(blob);
            });
            const response = await fetch('/api/whisper/transcribe', {
              method: 'POST', credentials: 'same-origin',
              headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
              body: JSON.stringify({ audio }),
            });
            const result = await response.json();
            if (!response.ok || typeof result.transcript !== 'string') throw new Error('Whisper не вернул расшифровку');
            setDraft((old) => `${old}${old ? ' ' : ''}${result.transcript.trim()}`);
            setNotice('Голос расшифрован OpenAI Whisper. Проверьте текст перед отправкой.');
          } catch { setNotice('Не удалось расшифровать голос. Проверьте настройки Whisper Morphy и попробуйте ещё раз.'); }
        };
        recorder.start();
        setVoiceBusy(true); setNotice('Запись идёт. Нажмите микрофон ещё раз, чтобы остановить.');
        return;
      } catch { setNotice('Нет доступа к микрофону. Разрешите доступ в браузере и повторите.'); return; }
    }
    const Recognition = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
    if (!Recognition) { setNotice('Голосовой ввод не поддерживается этим браузером.'); return; }
    try {
      const recognition = new Recognition();
      recognition.lang = 'ru-RU'; recognition.interimResults = true; recognition.continuous = false;
      recognition.onresult = (event: any) => {
        const transcript = Array.from(event.results as ArrayLike<any>).map((item: any) => item[0]?.transcript || '').join('');
        setDraft(transcript);
      };
      recognition.onerror = () => setNotice('Не удалось распознать речь. Проверьте разрешение на микрофон.');
      recognition.onend = () => { setVoiceBusy(false); recognitionRef.current = null; };
      recognition.start();
      recognitionRef.current = recognition;
      setVoiceBusy(true); setNotice('Говорите по-русски. Распознанный текст можно проверить перед отправкой.');
    } catch { setNotice('Не удалось запустить голосовой ввод.'); }
  }, [voiceBusy, whisperEnabled]);

  const exportMessage = useCallback(async (message: Message) => {
    const sources = message.sources || [];
    const markdown = `# Ответ помощника проекта\n\n${message.content}\n\n## Источники\n- Снимок панели: ${data?.generated_at || 'нет данных'}\n${sources.map((source) => `- ${source}`).join('\n')}`;
    try {
      await navigator.clipboard.writeText(markdown);
      setNotice('Ответ с источниками скопирован.');
    } catch {
      const url = URL.createObjectURL(new Blob([markdown], { type: 'text/markdown;charset=utf-8' }));
      const link = document.createElement('a'); link.href = url; link.download = `project-answer-${Date.now()}.md`; link.click(); URL.revokeObjectURL(url);
      setNotice('Ответ с источниками сохранён в Markdown.');
    }
  }, [data]);

  const downloadMessage = useCallback((message: Message) => {
    const sources = message.sources || [];
    const markdown = `# Ответ помощника проекта\n\n${message.content}\n\n## Источники\n- Снимок панели: ${data?.generated_at || 'нет данных'}\n${sources.map((source) => `- ${source}`).join('\n')}`;
    const url = URL.createObjectURL(new Blob([markdown], { type: 'text/markdown;charset=utf-8' }));
    const link = document.createElement('a'); link.href = url; link.download = `project-answer-${Date.now()}.md`; link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    setNotice('Ответ с источниками сохранён в Markdown.');
  }, [data]);

  if (!open) return null;
  const sources = Object.entries(data?.sources || {}) as [SourceName, { ok: boolean; age_s?: number | null }][];

  return (
    <aside className="pj-assistant" aria-label="Помощник проекта Morphy" aria-modal="false">
      <header className="pj-assistant-head">
        <div className="pj-assistant-title"><span className="pj-assistant-mark"><Sparkles size={17} /></span><div><strong>Помощник проекта</strong><small>{connected ? 'Morphy AI подключён' : 'подключение к Morphy…'}</small></div></div>
        <div className="pj-assistant-tools">
          <button type="button" className="pj-assistant-icon" onClick={onClose} aria-label="Свернуть панель" title="Свернуть панель"><PanelRightClose size={17} /></button>
          <button type="button" className="pj-assistant-icon pj-assistant-mobile-close" onClick={onClose} aria-label="Закрыть панель"><X size={17} /></button>
        </div>
      </header>

      <div className="pj-assistant-notice">Вопрос отправится в общий чат Morphy с ограниченным снимком панели. Чат использует настроенные в Morphy возможности; не просите его выполнять действия из этой панели.</div>

      <section className="pj-assistant-freshness" aria-label="Свежесть контекста">
        <div><span>Снимок панели</span><strong>{data?.generated_at ? fmtDateTime(data.generated_at) : 'Нет данных'}</strong></div>
        <div className="pj-assistant-source-list">
          {sources.map(([name, source]) => <span key={name} data-tone={source.ok ? 'ok' : 'warn'}>{SOURCE_LABELS[name]} · {source.age_s != null ? fmtAgo(source.age_s) : source.ok ? 'доступен' : 'недоступен'}</span>)}
        </div>
      </section>

      <div className="pj-assistant-level" role="group" aria-label="Подробность ответа">
        <span>Ответ:</span>
        <button type="button" className={detail === 'short' ? 'selected' : ''} onClick={() => setDetail('short')}>Кратко</button>
        <button type="button" className={detail === 'full' ? 'selected' : ''} onClick={() => setDetail('full')}>Подробно</button>
      </div>

      <div className="pj-assistant-messages" aria-live="polite">
        {!messages.length && <div className="pj-assistant-welcome"><Sparkles size={22} /><strong>Чем помочь с проектом?</strong><p>Можно спросить о риске, капитале, задачах или попросить найти события.</p><div className="pj-assistant-prompts"><button onClick={() => setDraft('Кратко опиши состояние риска и движка')}>Состояние проекта</button><button onClick={() => setDraft('Покажи ошибки guard за сегодня')}>Ошибки guard за сегодня</button><button onClick={() => setDraft('Что изменилось в капитале?')}>Изменение капитала</button></div></div>}
        {messages.map((message) => <article className={`pj-assistant-card ${message.role === 'user' ? 'user' : 'answer'}`} key={message.id}>
          <div className="pj-assistant-card-label">{message.role === 'user' ? 'Вы' : message.local ? 'По журналу событий' : 'Morphy AI'}<time>{new Date(message.at).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Qyzylorda' })}</time></div>
          <div className="pj-assistant-card-content">{message.content
            ? message.role === 'assistant' && message.content.length > 320 && !detailsOpen[message.id]
              ? `${message.content.slice(0, 320).trimEnd()}…`
              : message.content
            : sending && message.role === 'assistant' ? <span className="pj-assistant-thinking"><LoaderCircle size={14} /> Формирую ответ…</span> : ''}</div>
          {message.role === 'assistant' && message.content && <>
            <div className="pj-assistant-card-actions">
              {message.content.length > 320 && <button type="button" onClick={() => setDetailsOpen((old) => ({ ...old, [message.id]: !old[message.id] }))}><ChevronDown size={13} /> {detailsOpen[message.id] ? 'Скрыть детали' : 'Показать детали'}</button>}
              <button type="button" onClick={() => void exportMessage(message)}><Copy size={13} /> Копировать</button>
              <button type="button" onClick={() => downloadMessage(message)}><Download size={13} /> Скачать .md</button>
              <button type="button" onClick={() => setExpanded(expanded === message.id ? null : message.id)}><ChevronDown size={13} /> {expanded === message.id ? 'Скрыть источники' : 'Источники'}</button>
            </div>
            {expanded === message.id && <div className="pj-assistant-citations"><span>Снимок: {data?.generated_at ? fmtDateTime(data.generated_at) : 'нет данных'}</span>{(message.sources || []).map((source) => <span key={source}>{source}</span>)}</div>}
          </>}
        </article>)}
        {sending && !messages.some((item) => item.id === currentAnswerRef.current) && <div className="pj-assistant-thinking"><LoaderCircle size={14} /> Жду ответ Morphy…</div>}
      </div>

      {notice && <div className="pj-assistant-status" role="status">{notice}<button type="button" onClick={() => setNotice('')} aria-label="Скрыть уведомление"><X size={12} /></button></div>}
      <form className="pj-assistant-compose" onSubmit={(event) => { event.preventDefault(); send(); }}>
        <textarea value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="Спроси о проекте или событиях…" rows={3} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); send(); } }} aria-label="Запрос помощнику" />
        <div className="pj-assistant-compose-actions">
          <button type="button" className={`pj-assistant-icon ${voiceBusy ? 'recording' : ''}`} onClick={() => void startVoice()} aria-label={voiceBusy ? 'Остановить запись' : 'Голосовой ввод'} title={whisperEnabled ? 'Голосовой ввод через OpenAI Whisper' : 'Голосовой ввод браузера'}><Mic size={16} />{voiceBusy && <span className="pj-assistant-rec-dot" />}</button>
          <small>{whisperEnabled ? 'Whisper · текст перед отправкой можно проверить' : 'Распознавание браузера · Enter отправить, Shift+Enter — новая строка'}</small>
          <button type="submit" className="pj-assistant-send" disabled={!draft.trim() || sending || !connected} aria-label="Отправить запрос"><Send size={15} /> Спросить</button>
        </div>
      </form>
    </aside>
  );
}
