"""Строгая чёрно-белая страница с диалогами прогона — без цифр и метрик.

Только расшифровки: что сказал пользователь, что ответил ассистент, что вернула память.
Цифры и сводки живут отдельно (reports/live.json), чтобы страницу можно было читать и печатать как документ.
"""
import datetime as dt
import html
import json
import re
import os

CATEGORY_NAMES = {
    "IE": "Извлечение факта",
    "MR": "Факты из нескольких сессий",
    "KU": "Обновление факта",
    "TR": "Время и порядок событий",
    "ABS": "Честное «не знаю»",
    "DEL": "«Забудь»",
    "SYS": "Профиль из БПС",
    "AGG": "«Что знаешь о бизнесе»",
    "FRESH": "Сразу в новом диалоге",
}

VARIANT_NAMES = {
    "before": "До правок",
    "after": "После правок",
    "nofilter": "Без фильтра по темам",
}

CSS = """
@page{margin:18mm}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:#fff;color:#111;
     font:15px/1.6 "Helvetica Neue",Helvetica,Arial,sans-serif;font-feature-settings:"liga","kern"}
.wrap{max-width:1180px;margin:0 auto;padding:48px 24px 96px}
h1{font-size:26px;font-weight:600;letter-spacing:-.01em;margin:0 0 6px}
.sub{color:#555;margin:0 0 4px;font-size:14px}
.lede{color:#333;font-size:14px;max-width:760px;margin:20px 0 0;padding-top:18px;border-top:1px solid #111}
h2{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.09em;color:#111;
   margin:56px 0 14px;padding-bottom:7px;border-bottom:1px solid #111}
.toc{columns:2;column-gap:40px;margin:0;padding:0;list-style:none;font-size:14px}
.toc li{break-inside:avoid;margin:0 0 5px;display:flex;gap:10px}
.toc .k{font:12px/1.6 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:#777;min-width:66px}
.toc a{color:#111;text-decoration:none;border-bottom:1px solid #ccc}
.toc a:hover{border-bottom-color:#111}
.case{margin:0 0 4px;padding:30px 0 0;border-top:1px solid #111;break-inside:avoid-page}
.case:first-of-type{border-top:none}
.case-head{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:4px}
.case-id{font:12px/1 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:#777}
.case-title{font-size:19px;font-weight:600;margin:0}
.case-meta{font-size:13px;color:#555;margin:0 0 22px}
.case-meta .q{color:#111}
.cols{display:grid;gap:0;border-left:1px solid #ddd}
@media(min-width:900px){.cols{grid-template-columns:repeat(var(--n),1fr)}}
.col{padding:0 20px 8px;border-right:1px solid #ddd}
.col-head{display:flex;align-items:baseline;justify-content:space-between;gap:8px;
          padding:0 0 8px;margin-bottom:14px;border-bottom:1px solid #111}
.col-name{font:11px/1.4 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
          text-transform:uppercase;letter-spacing:.07em;color:#111;font-weight:600}
.mark{font-size:13px;color:#111}
.mark.no{color:#111;font-weight:700}
.turn{margin:0 0 13px}
.who{font:10px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
     text-transform:uppercase;letter-spacing:.09em;color:#888;margin-bottom:2px}
.txt{white-space:pre-wrap;word-break:break-word}
.turn.user .txt{border-left:2px solid #111;padding-left:11px}
.turn.assistant .txt{padding-left:13px}
.turn.memory .txt,.turn.note .txt{font-size:13px;color:#555;padding-left:13px;border-left:1px dotted #bbb}
.facts{margin:3px 0 0;padding:0 0 0 27px;font-size:13px;color:#555;list-style:none;border-left:1px dotted #bbb}
.facts li{margin:0 0 1px;text-indent:-11px;padding-left:11px}
.facts li:before{content:"— ";color:#aaa}
.foot{margin-top:72px;padding-top:16px;border-top:1px solid #111;color:#555;font-size:13px;max-width:760px}
@media print{.wrap{padding:0}.toc{display:none}h2{page-break-after:avoid}.case{page-break-inside:avoid}}
"""

ROLE_LABEL = {"user": "Пользователь", "assistant": "Ассистент",
              "memory": "Обращение к памяти", "note": "Ход прогона"}

DIALOG_RE = re.compile(r"^\[([^\]]+)\]\s*")
MEMORY_RE = re.compile(r"^функция памяти: топики \[(.*?)\] → (\d+) фактов(?::\s*(.*))?$", re.S)


def e(x):
    return html.escape(str(x))


def _plural(n, one, few, many):
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


def _memory_turn(text):
    """«функция памяти: топики ['a','b'] → 3 фактов: X (a); Y (b)» → читаемый вид."""
    m = MEMORY_RE.match(text.strip())
    if not m:
        return f'<div class="txt">{e(text)}</div>'
    topics = [t.strip().strip("'\"") for t in m.group(1).split(",") if t.strip()]
    count = int(m.group(2))
    facts = [f.strip() for f in (m.group(3) or "").split(";") if f.strip()]
    head = ("запрошенные темы: " + ", ".join(topics)) if topics else "темы не указаны"
    head += f" · вернулось {count} {_plural(count, 'факт', 'факта', 'фактов')}"
    body = "".join(f"<li>{e(f)}</li>" for f in facts)
    return f'<div class="txt">{e(head)}</div>' + (f'<ul class="facts">{body}</ul>' if body else "")


def _turns(items):
    out = []
    for role, text in items:
        label = ROLE_LABEL.get(role, role)
        text = str(text)
        if role in ("user", "assistant"):
            m = DIALOG_RE.match(text)
            if m:
                label += f" · диалог {m.group(1)}"
                text = DIALOG_RE.sub("", text, count=1)
        inner = _memory_turn(text) if role == "memory" else f'<div class="txt">{e(text)}</div>'
        out.append(f'<div class="turn {role}"><div class="who">{e(label)}</div>{inner}</div>')
    return "".join(out)


def render(data, title="Память ассистента — расшифровки диалогов"):
    rows = data["rows"]
    variants = data.get("variants") or ["before", "after"]
    names = [VARIANT_NAMES.get(v, v) for v in variants]
    stamp = dt.datetime.now().strftime("%d.%m.%Y")

    toc = "".join(
        f'<li><span class="k">{e(r["id"])}</span><a href="#{e(r["id"])}">{e(r["title"])}</a></li>' for r in rows)

    cases = []
    for r in rows:
        cols = []
        for v, name in zip(variants, names):
            run = r["runs"][v][0]
            mark = "соответствует ожиданию" if run["passed"] else "не соответствует"
            cls = "" if run["passed"] else " no"
            cols.append(f'<div class="col"><div class="col-head"><span class="col-name">{e(name)}</span>'
                        f'<span class="mark{cls}">{mark}</span></div>{_turns(run["transcript"])}</div>')
        cases.append(
            f'<section class="case" id="{e(r["id"])}">'
            f'<div class="case-head"><span class="case-id">{e(r["id"])}</span>'
            f'<h3 class="case-title">{e(r["title"])}</h3></div>'
            f'<p class="case-meta">{e(CATEGORY_NAMES.get(r["category"], r["category"]))} · '
            f'вопрос в новом диалоге: <span class="q">«{e(r["question"])}»</span></p>'
            f'<div class="cols" style="--n:{len(variants)}">{"".join(cols)}</div></section>')

    body = f"""<div class="wrap">
<h1>{e(title)}</h1>
<p class="sub">Прогон {stamp} · {len(rows)} сценариев · версии логики: {e(", ".join(names))}</p>
<p class="lede">Ниже — полные расшифровки. В каждом сценарии пользователь сначала рассказывает о себе
в одном или нескольких диалогах, затем задаёт вопрос в новом диалоге, где истории переписки нет:
ответить можно только из памяти. Колонки — одна и та же модель на разных версиях кода памяти.
Строка «обращение к памяти» показывает, какие темы запросил ассистент и какие факты вернулись.
Числовые итоги вынесены отдельно и в этот документ не входят.</p>
<h2>Содержание</h2>
<ul class="toc">{toc}</ul>
<h2>Расшифровки</h2>
{"".join(cases)}
<p class="foot">Сценарии составлены вручную под задачи бизнес-помощника; разбивка категорий следует
схеме бенчмарка LongMemEval. Пометка «соответствует ожиданию» означает совпадение ответа
с заранее заданными ключевыми словами сценария.</p>
</div>"""
    return (f'<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{e(title)}</title><style>{CSS}</style></head><body>{body}</body></html>')


def render_file(src, dst, title=None):
    with open(src, encoding="utf-8") as f:
        data = json.load(f)
    htm = render(data, title) if title else render(data)
    with open(dst, "w", encoding="utf-8") as f:
        f.write(htm)
    return os.path.abspath(dst)
