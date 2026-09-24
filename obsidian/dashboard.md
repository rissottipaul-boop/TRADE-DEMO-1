# Пульт проекта

Стартовая страница проекта в Obsidian. Блоки ниже — встроенные запросы Obsidian: они сами обновляются, когда агенты меняют доску и инсайты, и работают без сторонних плагинов. На GitHub эти блоки выглядят как код — это нормально.

**Документы:** [доска задач](../ops/board.md) · [инциденты](../ops/incidents.md) · [roadmap](../insights/roadmap.md) · [прогресс Фазы 1](../insights/phase1-progress.md) · [регламент агентов](../AGENTS.md) · [README](../README.md) · [мои заметки](../notes/README.md)

## Ждёт вашего решения

Задачи со статусом `needs-user`. Клик по строке открывает её на доске, как ответить — в разделе «Как пользоваться».

```query
path:"ops/board.md" line:/^\|(?:[^|]*\|){3}\s*needs-user\b/
```

## В работе и заблокировано

```query
path:"ops/board.md" line:/^\|(?:[^|]*\|){3}\s*(?:in-progress|blocked)\b/
```

## Регулярные проверки

```query
path:"ops/board.md" line:/^\|(?:[^|]*\|){3}\s*scheduled\s+\S+/
```

## Инциденты

Новые записи — сверху.

```query
path:"ops/incidents.md" line:/^\|\s*\d{4}-\d{2}-\d{2}/
```

## Что менялось последним

Вторая вкладка таблицы — все документы проекта, не только инсайты.

```base
filters:
  and:
    - file.ext == "md"
    - '!file.inFolder("obsidian")'
properties:
  file.name:
    displayName: Документ
  file.folder:
    displayName: Папка
  file.mtime:
    displayName: Изменён
views:
  - type: table
    name: Инсайты
    filters:
      and:
        - file.inFolder("insights")
    order:
      - file.name
      - file.mtime
    sort:
      - property: file.mtime
        direction: DESC
    limit: 10
  - type: table
    name: Все документы
    order:
      - file.name
      - file.folder
      - file.mtime
    sort:
      - property: file.mtime
        direction: DESC
    limit: 15
```

## Статусы инсайтов

```query
path:insights/ line:/^-\s\*\*Статус:\*\*/
```

## Очередь агентов

Задачи `ready` — их по очереди берёт автопилот оркестратора.

```query
path:"ops/board.md" line:/^\|(?:[^|]*\|){3}\s*ready\b/
```

## Как пользоваться

- **Ответить на вопрос агента.** Откройте задачу на доске и поставьте курсор в начало ячейки «Заметки». Вставьте шаблон `board-answer` (`Ctrl+P` → «Insert template» / «Вставить шаблон»): появится `**Ответ человека <дата> <время>:**`. Допишите ответ и замените статус `needs-user` на `ready` — автопилот оркестратора подхватит задачу и разнесёт решение ([AGENTS.md](../AGENTS.md) §4).
- **Дать агентам задачу.** Поставьте курсор на пустую строку сразу под таблицей «Активные» и вставьте шаблон `board-task`. Замените ID, задачу, агента и критерий готовности. Агенты: Crypto Insight Hunter — исследования, Insight Executor — код, OKX Trader — биржа, Ops Sentinel — мониторинг, Pump Risk Taker — памп-карман.
- **Строка доски — одна строка текста.** Enter разрывает строку таблицы, а символ `|` сдвигает колонки. Заголовки колонок не меняйте, статус — первое слово ячейки: доску машинно читает автопилот. Доска большая, поэтому править её удобнее в режиме исходного текста (Source mode). Агенты пишут в доску постоянно. Obsidian подхватывает их правки сам, а свои правки делайте короткими.
- **Свои заметки** — в `notes/`: туда Obsidian кладёт новые заметки. Агенты читают их как контекст, а поручения берут только с доски.
- **Ссылки** Obsidian вставляет в формате markdown с относительным путём. Такие ссылки понимают агенты и GitHub, поэтому настройку «Use Wikilinks» (Files and links) не включайте.
