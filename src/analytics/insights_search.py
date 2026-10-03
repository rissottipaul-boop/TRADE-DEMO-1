"""П. 36 — ответы по базе инсайтов: поиск выводов в insights/ с датой, статусом и источниками.

Индекс строится на лету по insights/*.md (только чтение). Для файла берутся заголовок,
поля шапки «- **Дата:** …», «- **Статус:** …», «- **Задача:** …» (первые 25 строк), иначе
дата из имени файла; источники — URL и ссылки на файлы проекта в разделе, а если в разделе
их нет — источники файла с пометкой. Поиск — по разделам (между заголовками #..####):
термины запроса сравниваются по основе слова (первые 5 символов для слов длиннее 5), чтобы
находить словоформы русского текста. Каждому найденному выводу ставятся флаги
«нет даты», «нет источников», «старше N дней», «статус rejected/superseded»: заметка не
заменяет сверку с биржей (AGENTS.md §3).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from src.analytics.common import ROOT

INSIGHTS_DIR = ROOT / "insights"
STALE_DAYS = 30
_FIELD = re.compile(r"^\s*-\s+\*\*([^*:]+):\*\*\s*(.+)$")
_DATE = re.compile(r"(20\d\d)-(\d\d)-(\d\d)")
_URL = re.compile(r"https?://[^\s)\]>|`]+")
_FILE_LINK = re.compile(r"\]\(((?:\.\./|\./)?[\w./-]+\.(?:md|py|json|ps1|diff))(?:#[^)]*)?\)")
_HEADING = re.compile(r"^(#{1,4})\s+(.+)$")
_WORD = re.compile(r"[\wё-]+", re.I)
_BAD_STATUS = re.compile(r"rejected|superseded|отклон|устарел|deprecated|заменён", re.I)
_STOP = {"и", "в", "на", "по", "с", "что", "как", "для", "не", "или", "the", "a", "of", "это",
         "ли", "из", "к", "о", "а", "у", "за", "от", "до", "при", "какой", "какие", "каков"}


@dataclass
class Section:
    file: str
    heading: str
    line: int
    text: list[str] = field(default_factory=list)


@dataclass
class Doc:
    file: str
    title: str
    date: Optional[str]
    status: Optional[str]
    task: Optional[str]
    sources: list[str]
    sections: list[Section]


def _stem(word: str) -> str:
    w = word.lower().replace("ё", "е")
    return w[:5] if len(w) > 5 else w


def terms(query: str) -> list[str]:
    return [_stem(w) for w in _WORD.findall(query) if w.lower() not in _STOP and len(w) > 1]


def _sources(lines: list[str]) -> list[str]:
    found: list[str] = []
    for ln in lines:
        for u in _URL.findall(ln):
            u = u.rstrip(".,;:")
            if u not in found:
                found.append(u)
        for f in _FILE_LINK.findall(ln):
            if f not in found:
                found.append(f)
    return found


def parse_doc(path: Path) -> Doc:
    text = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    title = next((m.group(2).strip() for m in map(_HEADING.match, text) if m), path.stem)
    meta: dict[str, str] = {}
    for ln in text[:25]:
        m = _FIELD.match(ln)
        if m:
            meta.setdefault(m.group(1).strip().lower(), m.group(2).strip())
    date_raw = next((v for k, v in meta.items() if k.startswith("дата")), None)
    d = _DATE.search(date_raw or "") or _DATE.search(path.name) or \
        next((m for m in map(_DATE.search, text[:10]) if m), None)
    status = next((v for k, v in meta.items() if k.startswith("статус")), None)
    task = next((v for k, v in meta.items() if k.startswith("задача")), None)
    sections: list[Section] = []
    cur = Section(path.name, title, 1)
    for n, ln in enumerate(text, 1):
        m = _HEADING.match(ln)
        if m and n > 1:
            sections.append(cur)
            cur = Section(path.name, m.group(2).strip(), n)
        cur.text.append(ln)
    sections.append(cur)
    sources = _sources(text)
    for k, v in meta.items():
        if k.startswith(("источник", "прогон", "данные")):
            sources.insert(0, f"{k}: {v[:160]}")
    return Doc(path.name, title, d.group(0) if d else None, status, task, sources, sections)


def load_docs(directory: Path = INSIGHTS_DIR) -> list[Doc]:
    return [parse_doc(p) for p in sorted(Path(directory).glob("*.md"))]


def search(query: str, directory: Path = INSIGHTS_DIR, top: int = 5,
           today: Optional[date] = None, stale_days: int = STALE_DAYS) -> dict:
    today = today or date.today()
    q = terms(query)
    if not q:
        raise ValueError("пустой запрос: нужны значимые слова")
    hits = []
    for doc in load_docs(directory):
        title_stems = {_stem(w) for w in _WORD.findall(doc.title)}
        for sec in doc.sections:
            words = [_stem(w) for ln in sec.text for w in _WORD.findall(ln)]
            head = {_stem(w) for w in _WORD.findall(sec.heading)}
            counts = {t: sum(1 for w in words if w.startswith(t)) for t in q}
            matched = [t for t, c in counts.items() if c]
            if not matched:
                continue
            score = len(matched) * 10 + sum(min(c, 5) for c in counts.values()) + \
                3 * len(head & set(q)) + 2 * len(title_stems & set(q))
            best, best_n = "", -1
            for ln in sec.text:
                s = ln.strip()
                if not s or s.startswith("#") or s.startswith("|---") or s.startswith("| ---"):
                    continue
                n = sum(1 for w in _WORD.findall(s) if any(_stem(w).startswith(t) for t in q))
                if n > best_n:
                    best, best_n = s, n
            sec_sources = _sources(sec.text)
            flags = []
            age = None
            if doc.date:
                age = (today - datetime.strptime(doc.date, "%Y-%m-%d").date()).days
                if age > stale_days:
                    flags.append(f"старше {stale_days} дн. ({age} дн.)")
            else:
                flags.append("нет даты")
            if not sec_sources and not doc.sources:
                flags.append("нет источников")
            elif not sec_sources:
                flags.append("источники только на уровне файла")
            if doc.status and _BAD_STATUS.search(doc.status):
                flags.append(f"статус: {doc.status[:60]}")
            hits.append({"file": f"insights/{doc.file}", "title": doc.title, "section": sec.heading,
                         "line": sec.line, "date": doc.date, "age_days": age,
                         "status": doc.status, "task": doc.task, "score": score,
                         "matched": matched, "missing_terms": [t for t in q if t not in matched],
                         "snippet": best[:300], "sources": (sec_sources or doc.sources)[:8],
                         "flags": flags})
    hits.sort(key=lambda h: (-h["score"], h["age_days"] if h["age_days"] is not None else 10**6))
    return {"query": query, "terms": q, "total": len(hits), "hits": hits[:top]}


def render(res: dict) -> str:
    lines = [f"# Инсайты по запросу: «{res['query']}»", "",
             f"Найдено разделов: {res['total']}, показано {len(res['hits'])}. Термины: {', '.join(res['terms'])}", ""]
    if not res["hits"]:
        lines.append("Ничего не найдено — вопрос для Crypto Insight Hunter (новое исследование).")
    for i, h in enumerate(res["hits"], 1):
        lines += [f"## {i}. [{h['title']} — {h['section']}]({h['file']}#L{h['line']})", "",
                  f"- **Файл:** `{h['file']}` строка {h['line']}; **дата:** {h['date'] or '—'}; "
                  f"**статус:** {h['status'] or '—'}",
                  f"- **Вывод:** {h['snippet']}",
                  f"- **Источники:** " + (", ".join(h["sources"]) if h["sources"] else "—")]
        if h["flags"]:
            lines.append(f"- **Внимание:** {'; '.join(h['flags'])}")
        if h["missing_terms"]:
            lines.append(f"- Не найдены в разделе: {', '.join(h['missing_terms'])}")
        lines.append("")
    return "\n".join(lines) + "\n"
