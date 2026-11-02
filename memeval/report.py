"""HTML-отчёты: один самодостаточный файл, открывается в любом браузере, можно переслать команде."""
import datetime as dt
import html

from .live import CATEGORIES
from .pipeline import FIX_TITLES

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#16191d;--muted:#5f6b7a;--line:#e3e7ec;--ok:#1f8a4c;--ok-bg:#e6f4ec;
--bad:#c23b32;--bad-bg:#fbe9e7;--accent:#21a038;--chip:#eef1f4;--mem:#f3f0ff;--memink:#4b3fa6;--note:#fff7e0}
@media (prefers-color-scheme:dark){:root{--bg:#111417;--card:#1a1e23;--ink:#e8ebef;--muted:#9aa5b1;--line:#2b3138;
--ok:#4cc27e;--ok-bg:#163223;--bad:#ef6b61;--bad-bg:#3a1c1a;--chip:#252b32;--mem:#25213d;--memink:#b9b0ff;--note:#33301f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,Segoe UI,Roboto,Arial,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:28px 16px 60px}h1{font-size:26px;margin:0 0 4px}h2{font-size:19px;margin:34px 0 12px}
.sub{color:var(--muted);margin:0 0 22px}.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin:18px 0}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}.kpi .v{font-size:28px;font-weight:700}.kpi.multi .v{font-size:21px}
.kpi .l{color:var(--muted);font-size:13px}.arrow{color:var(--muted);font-weight:400;padding:0 6px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
th,td{padding:9px 11px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{font-size:13px;color:var(--muted);font-weight:600}
tr:last-child td{border-bottom:none}.b{display:inline-block;padding:2px 9px;border-radius:999px;font-size:13px;font-weight:600;white-space:nowrap}
.ok{background:var(--ok-bg);color:var(--ok)}.bad{background:var(--bad-bg);color:var(--bad)}.chip{display:inline-block;background:var(--chip);
border-radius:6px;padding:1px 7px;font-size:12px;margin:1px 2px 1px 0}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:16px;margin:14px 0}.card h3{margin:0 0 4px;font-size:17px}.cols{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:12px}
@media(max-width:780px){.cols{grid-template-columns:1fr}}.side{border:1px solid var(--line);border-radius:10px;padding:12px}
.side h4{margin:0 0 8px;font-size:14px;display:flex;justify-content:space-between;align-items:center;gap:8px}
.verdict{font-size:14px;margin:0 0 10px}.msg{margin:6px 0;padding:7px 10px;border-radius:9px;font-size:14px;white-space:pre-wrap;word-break:break-word}
.user{background:var(--chip)}.assistant{border:1px solid var(--line)}.memory{background:var(--mem);color:var(--memink);font-size:13px}
.note{background:var(--note);font-size:13px}.who{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
details{margin-top:8px}summary{cursor:pointer;color:var(--muted);font-size:13px}pre{white-space:pre-wrap;font-size:12px;margin:6px 0;
background:var(--chip);padding:8px;border-radius:8px}.small{font-size:12px}.bar{height:8px;border-radius:4px;background:var(--chip);overflow:hidden;min-width:90px}
.bar>span{display:block;height:100%}.foot{color:var(--muted);font-size:13px;margin-top:34px}
"""


def e(x):
    return html.escape(str(x))


def badge(ok, yes="✓ работает", no="✗ ломается"):
    return f'<span class="b {"ok" if ok else "bad"}">{yes if ok else no}</span>'


def page(title, body):
    return (f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><title>{e(title)}</title><style>{CSS}</style></head>'
            f'<body><div class="wrap">{body}</div></body></html>')


def now():
    return dt.datetime.now().strftime("%d.%m.%Y %H:%M")


def transcript_html(items):
    labels = {"user": "пользователь", "assistant": "ассистент", "memory": "память", "note": "что происходит"}
    return "".join(f'<div class="msg {k}"><div class="who">{labels.get(k, k)}</div>{e(t)}</div>' for k, t in items)


def table_html(rows):
    if not rows:
        return '<p class="small">таблица memory пуста</p>'
    head = "".join(f"<th>{e(k)}</th>" for k in rows[0])
    body = "".join("<tr>" + "".join(f"<td class='small'>{e(v)}</td>" for v in r.values()) + "</tr>" for r in rows)
    return f"<table><tr>{head}</tr>{body}</table>"


# ------------------------------------------------------------------ мок

def render_mock(rows):
    n = len(rows)
    nb = sum(r["before"]["passed"] for r in rows)
    na = sum(r["after"]["passed"] for r in rows)
    body = [f"<h1>Agent Memory Eval: что починили</h1><p class='sub'>Мок-прогон: заскриптованная модель, логика кода «до» и «после» правок. {now()}</p>",
            "<div class='kpis'>"
            f"<div class='kpi'><div class='v'>{nb} из {n}</div><div class='l'>сценариев проходит на исходном коде</div></div>"
            f"<div class='kpi'><div class='v'>{na} из {n}</div><div class='l'>сценариев проходит после правок</div></div>"
            f"<div class='kpi'><div class='v'>{len({k for r in rows for k in r['fixes']})}</div><div class='l'>правок проверено</div></div></div>",
            "<h2>Сводка</h2><table><tr><th>Сценарий</th><th>Симптом</th><th>Правка</th><th>До</th><th>Только эта правка</th><th>После всех правок</th></tr>"]
    for r in rows:
        fx = "".join(f'<span class="chip">{e(FIX_TITLES[k])}</span>' for k in r["fixes"])
        body.append(f"<tr><td><a href='#{r['id']}'>{e(r['title'])}</a></td><td class='small'>{e(r['problem'])}</td><td>{fx}</td>"
                    f"<td>{badge(r['before']['passed'])}</td><td>{badge(r['only']['passed'])}</td><td>{badge(r['after']['passed'])}</td></tr>")
    body.append("</table><h2>Подробно по сценариям</h2>")
    for r in rows:
        body.append(f"<div class='card' id='{r['id']}'><h3>{e(r['title'])}</h3><div class='small'>{e(r['problem'])}</div><div class='cols'>")
        for label, name in (("before", "До правок"), ("after", "После правок")):
            x = r[label]
            metric = f"<div class='small'>{e(x['metric'])}</div>" if x.get("metric") else ""
            calls = ", ".join(f"{k}: {v}" for k, v in x.get("llm_calls", {}).items())
            body.append(
                f"<div class='side'><h4>{name} {badge(x['passed'])}</h4><p class='verdict'>{e(x['verdict'])}</p>{metric}"
                f"{transcript_html(x['transcript'])}"
                f"<details><summary>Логи обработки</summary><pre>{e(chr(10).join(x['events']) or '—')}</pre></details>"
                f"<details><summary>Таблица memory в конце</summary>{table_html(x['memory'])}</details>"
                f"<div class='small' style='margin-top:6px;color:var(--muted)'>вызовы модели: {e(calls or '—')}</div></div>")
        body.append("</div></div>")
    body.append(
        "<p class='foot'>Проверяется Python-эмулятор: исходная схема и её исправления. Ответы модели "
        "заранее заданы, включая типичные сбои. Это регрессионная проверка логики, а не оценка качества LLM. "
        "Для новой профильной архитектуры используйте модульные тесты и варианты profile/full/search "
        "в команде live. Вызовы настоящей модели нужны для проверки поведения промптов.</p>")
    return page("Agent Memory Eval — мок-прогон", "".join(body))


# ------------------------------------------------------------------ живой бенч

def _pct(x):
    return f"{round(100 * x)}%"


def _bar(frac, color):
    return f"<div class='bar'><span style='width:{round(100 * frac)}%;background:{color}'></span></div>"


def render_live(rows, summ, meta):
    from .live import VARIANTS
    names = list(meta.get("variants") or ("before", "after"))
    titles = [VARIANTS[v][0] if v in VARIANTS else v for v in names]
    first, last = summ[names[0]], summ[names[-1]]

    cls = "kpi multi" if len(names) > 2 else "kpi"

    def kpi(fmt, label):
        cells = "<span class='arrow'>→</span>".join(fmt(summ[v]) for v in names)
        return f"<div class='{cls}'><div class='v'>{cells}</div><div class='l'>{label}</div></div>"

    body = [f"<h1>Agent Memory Eval: живой прогон</h1><p class='sub'>Модель: {e(meta.get('model'))} · сценариев: {len(rows)} · "
            f"повторов: {e(meta.get('repeats'))} · версии: {e(' → '.join(titles))} · {now()}</p><div class='kpis'>",
            kpi(lambda x: _pct(x["accuracy"]), "сценариев пройдено (без демонстрационных)"),
            kpi(lambda x: f"{_pct(x['ci95'][0])}–{_pct(x['ci95'][1])}", "95% доверительный интервал (Вилсон)"),
            kpi(lambda x: str(x["empty"]), "пустых ответов пользователю"),
            kpi(lambda x: f"{x['avg_topics']:.1f}", "топиков в среднем в вызове памяти"),
            kpi(lambda x: str(x["crashes"]), "падений фонового разбора"),
            kpi(lambda x: f"{(x.get('cost') or {}).get('calls_per_message', 0):.1f}", "вызовов модели на сообщение пользователя"),
            "</div><h2>По категориям</h2><table><tr><th>Категория</th><th>Откуда</th>"
            + "".join(f"<th>{e(t)}</th><th></th>" for t in titles) + "</tr>"]
    demo_cats = {r["category"] for r in rows if not r.get("measure", True)} - \
        {r["category"] for r in rows if r.get("measure", True)}
    for cat, (name, src) in CATEGORIES.items():
        if cat not in last["by_cat"] or cat in demo_cats:
            continue
        cells = ""
        for i, v in enumerate(names):
            p, n = summ[v]["by_cat"][cat]
            color = "var(--muted)" if i == 0 else "var(--ok)"
            cells += f"<td>{p}/{n}</td><td>{_bar(p / max(n, 1), color)}</td>"
        body.append(f"<tr><td><b>{e(name)}</b></td><td class='small'>{e(src)}</td>{cells}</tr>")
    body.append("</table>")
    body.append(_stats_block(summ, names, titles, rows))
    body.append(_cost_block(summ, names, titles))
    body.append(_volume_block(summ, names, titles))
    body.append("<h2>Сценарии</h2><table><tr><th>Категория</th><th>Вопрос</th>"
                + "".join(f"<th>{e(t)}</th>" for t in titles) + "</tr>")

    def cell(runs, row=None):
        if row is not None and row["expect"].get("report"):
            picks = [", ".join(x.get("report") or ["ни один"]) for x in runs]
            return f"<span class='chip'>{e(' | '.join(picks))}</span>"
        ok = sum(x["passed"] for x in runs)
        return badge(ok == len(runs), f"✓ {ok}/{len(runs)}", f"✗ {ok}/{len(runs)}")
    for r in rows:
        cells = "".join(f"<td>{cell(r['runs'][v], r)}</td>" for v in names)
        demo = "" if r.get("measure", True) else " <span class='chip'>демо, не в средней</span>"
        body.append(f"<tr><td class='small'>{e(CATEGORIES[r['category']][0])}{demo}</td>"
                    f"<td><a href='#{e(r['id'])}'>{e(r['question'])}</a></td>{cells}</tr>")
    body.append("</table><h2>Подробно</h2>")
    width = "1fr 1fr" if len(names) < 3 else " ".join(["1fr"] * len(names))
    for r in rows:
        body.append(f"<div class='card' id='{e(r['id'])}'><h3>{e(r['title'])}</h3><div class='small'>{e(CATEGORIES[r['category']][0])} · "
                    f"проверка: {e(r['expect'])}</div><div class='cols' style='grid-template-columns:{width}'>")
        for v, title in zip(names, titles):
            x = r["runs"][v][0]
            checks = "".join(f"<div class='small'>{'✓' if ok else '✗'} {e(kind)} {e(val)}</div>" for kind, val, ok in x.get("checks", []))
            err = f"<p class='verdict' style='color:var(--bad)'>{e(x['error'])}</p>" if x.get("error") else ""
            c = x.get("cost") or {}
            cost = (f"<div class='small'>стоимость: сообщений {c['messages']}, сессий {c['sessions']}, вызовов модели "
                    f"{sum(c['calls'].values())} ({e(', '.join(f'{k} {n}' for k, n in sorted(c['calls'].items())))}), "
                    f"≈ токенов промпта {sum(c['prompt_tokens'].values())}</div>") if c else ""
            body.append(f"<div class='side'><h4>{e(title)} {badge(x['passed'])}</h4>{err}{checks}{cost}{transcript_html(x['transcript'])}"
                        f"<details><summary>Логи обработки</summary><pre>{e(chr(10).join(x['events']) or '—')}</pre></details>"
                        f"<details><summary>Память в конце (таблица memory или профиль)</summary>{table_html(x['memory'])}</details>"
                        + (f"<details><summary>Журнал правок профиля</summary>{table_html(x['journal'])}</details>"
                           if x.get("journal") else "") + "</div>")
        body.append("</div></div>")
    body.append("<p class='foot'>Сценарии: каждая сессия — отдельный диалог, вопрос задаётся в новом диалоге, поэтому ответить можно "
                "только из памяти. Категории IE/MR/KU/TR/ABS — по схеме бенчмарка LongMemEval, остальные — доменные. Проверка по "
                "ключевым словам (числа — с границами: «5» не находится в «15»); если запрещённое слово нашлось или нужно "
                "честное «не знаю», решает судья — та же модель коротким вопросом YES/NO, её вердикт виден в логах. "
                "Ответы целиком видны выше. Модель недетерминирована: для выводов используйте --repeats 3 и больше. "
                "В колонке «чтение без фильтра по темам» выбор тем моделью игнорируется: отдаются все факты пользователя "
                "(до 100, самые свежие первыми).</p>")
    return page("Agent Memory Eval — живой прогон", "".join(body))


def _cost_block(summ, names, titles):
    """Метрики стоимости: вызовы модели на сообщение и на сессию, примерные токены промпта."""
    if not all(summ[v].get("cost") for v in names):
        return ""
    gap = summ[names[0]]["cost"].get("session_gap_min", 30)
    spec = [("Сообщений пользователя", "messages", "{:.0f}"), ("Сессий (пауза в диалоге ≥ %d мин)" % gap, "sessions", "{:.0f}"),
            ("Вызовов модели на сообщение", "calls_per_message", "{:.2f}"),
            ("— из них запись (сбор, аннотатор, классификатор)", "write_calls_per_message", "{:.2f}"),
            ("— из них агент (ответ и повтор после функции памяти)", "chat_calls_per_message", "{:.2f}"),
            ("Вызовов модели на сессию", "calls_per_session", "{:.2f}"),
            ("Вызовов записи на сессию", "write_calls_per_session", "{:.2f}"),
            ("≈ Токенов промпта на сообщение", "prompt_tokens_per_message", "{:.0f}"),
            ("≈ Токенов промпта в запросе агента", "chat_prompt_tokens_per_call", "{:.0f}"),
            ("≈ Размер памяти в конце сценария, токенов", "profile_tokens_avg", "{:.0f}")]
    out = ["<h2>Стоимость</h2><table><tr><th>Метрика</th>" + "".join(f"<th>{e(t)}</th>" for t in titles) + "</tr>"]
    for label, key, fmt in spec:
        out.append(f"<tr><td>{e(label)}</td>" + "".join(f"<td>{fmt.format(summ[v]['cost'][key])}</td>" for v in names) + "</tr>")
    out.append("</table><p class='small'>Считаются вызовы модели внутри эмулятора, без судьи. Токены — оценка по числу "
               "символов промпта (≈ 3,5 символа на токен), а не счётчик модели. Сообщения включают вопрос сценария.</p>")
    return "".join(out)


def _volume_block(summ, names, titles):
    """VOLUME: верные вопросы, сохранность фактов в конце и минимум по ходу, кривые по контрольным точкам."""
    vols = [summ[v].get("volume") for v in names]
    if not any(vols):
        return ""
    def frac(x):
        return "—" if x is None else _pct(x)
    spec = [("Верных ответов на вопросы к истории", lambda x: f"{x['questions_ok']}/{x['questions']}"),
            ("Сохранность фактов в конце истории", lambda x: frac(x["retention_final"])),
            ("Худшая сохранность на контрольной точке", lambda x: frac(x["retention_min"])),
            ("Устаревших значений осталось в памяти", lambda x: f"{x['stale_final']} из {x['gone_final']}")]
    out = ["<h2>Объём и долгая история</h2><table><tr><th>Метрика</th>" + "".join(f"<th>{e(t)}</th>" for t in titles) + "</tr>"]
    for label, fn in spec:
        out.append(f"<tr><td>{e(label)}</td>" + "".join(f"<td>{fn(x) if x else '—'}</td>" for x in vols) + "</tr>")
    out.append("</table><h3>Сохранность по контрольным точкам (первый повтор)</h3><table><tr><th>Сценарий</th>"
               + "".join(f"<th>{e(t)}</th>" for t in titles) + "</tr>")
    ids = sorted({sid for x in vols if x for sid in x["curves"]})
    for sid in ids:
        cells = ""
        for x in vols:
            pts = (x or {}).get("curves", {}).get(sid, [])
            cells += "<td class='small'>" + " → ".join(
                f"{round(100 * p['kept'] / p['expected']) if p['expected'] else 100}%" for p in pts) + \
                (f"<br>в конце ≈ {pts[-1]['tokens']} ток. памяти" if pts else "") + "</td>"
        out.append(f"<tr><td><a href='#{e(sid)}'>{e(sid)}</a></td>{cells}</tr>")
    out.append("</table><p class='small'>Контрольная точка — конец каждой сессии: код сверяет память со списком всех "
               "сказанных фактов (ledger) по ключевым словам, без модели. Заменённые и забытые факты после точки замены в "
               "текущем тексте памяти быть не должны; старое значение в «было» не считается.</p>")
    return "".join(out)


def _stats_block(summ, names, titles, rows):
    out = []
    if len(names) > 1:
        out.append("<h2>Значимость разницы</h2><table><tr><th>Сравнение</th><th>Починилось</th><th>Сломалось</th>"
                   "<th>p (Макнемар, точный)</th><th>Вывод</th></tr>")
        for v, t in zip(names[1:], titles[1:]):
            m = summ[v].get("vs_" + names[0])
            if not m:
                continue
            verdict = "значимо (p < 0,05)" if m["significant"] else "пока не значимо — нужно больше сценариев или повторов"
            out.append(f"<tr><td>{e(titles[0])} → {e(t)}</td><td>{m['fixed']}</td><td>{m['broken']}</td>"
                       f"<td>{m['p_value']:.3g}</td><td class='small'>{e(verdict)}</td></tr>")
        out.append("</table><p class='small'>Считаются только сценарии, где версии разошлись: «починилось» — до правок "
                   "провал, после — успех; «сломалось» — наоборот. Демонстрационные кейсы в расчёт не входят.</p>")
    pairs = summ[names[-1]].get("pairs") or []
    if pairs:
        out.append("<h2>Парные кейсы</h2><p class='small'>Пара засчитывается, только если пройдены оба сценария: "
                   "например, «если перейдём на ОСНО» не должно стать фактом, а «мы перешли на ОСНО» — должно.</p>"
                   "<table><tr><th>Пара</th>" + "".join(f"<th>{e(t)}</th>" for t in titles) + "</tr>")
        for i, p in enumerate(pairs):
            cells = "".join(f"<td>{summ[v]['pairs'][i]['ok']}/{summ[v]['pairs'][i]['n']}</td>" for v in names)
            out.append(f"<tr><td>{e(p['pair'])}</td>{cells}</tr>")
        out.append("</table>")
    if any(not r.get("measure", True) for r in rows):
        out.append("<h2>Демонстрационные кейсы</h2><p class='small'>Не входят в среднюю точность. PROOF — механические "
                   "кейсы, где исходный код не может найти факт по построению; SRC — нет правильного ответа, пока продукт "
                   "не решит, какой источник главнее: фиксируем, что выбрал ассистент.</p>"
                   "<table><tr><th>Сценарий</th>" + "".join(f"<th>{e(t)}</th>" for t in titles) + "</tr>")
        for r in rows:
            if r.get("measure", True):
                continue
            cells = ""
            for v in names:
                vals = summ[v]["demo"].get(r["id"], [])
                cells += "<td>" + e(" | ".join(", ".join(x) if isinstance(x, list) else ("✓" if x else "✗") for x in vals)) + "</td>"
            out.append(f"<tr><td><a href='#{e(r['id'])}'>{e(r['title'])}</a></td>{cells}</tr>")
        out.append("</table>")
    return "".join(out)


# ------------------------------------------------------------------ стабильность

def render_stability(res, meta):
    n = res["n"]
    ob, oa = res["outcome"]["before"], res["outcome"]["after"]

    def frac(o, k):
        return o.get(k, 0) / max(o.get("runs", 1), 1)
    body = [f"<h1>Аннотатор памяти: стабильность</h1><p class='sub'>Модель: {e(meta.get('model'))} · {n} прогонов одного и того же набора "
            f"фактов · {now()}</p><div class='kpis'>"
            f"<div class='kpi'><div class='v'>{_pct(frac(ob, 'forget_ok'))}<span class='arrow'>→</span>{_pct(frac(oa, 'forget_ok'))}</div>"
            f"<div class='l'>«забудь» реально удалил факт (те же ответы модели, код до → после)</div></div>"
            f"<div class='kpi'><div class='v'>{ob.get('crashed', 0)}<span class='arrow'>→</span>{oa.get('crashed', 0)}</div><div class='l'>падений разбора</div></div>"
            f"<div class='kpi'><div class='v'>{ob.get('hidden', 0)}<span class='arrow'>→</span>{oa.get('hidden', 0)}</div><div class='l'>фактов осталось невидимыми (без типа / не разобраны)</div></div>"
            "</div><h2>Как модель решает по каждому факту</h2><table><tr><th>Факт</th><th>Ожидается</th><th>Что ответила модель</th>"
            "<th>Согласованность</th><th>Верно</th></tr>"]
    for f in res["facts"]:
        dist = " ".join(f'<span class="chip">{e(k)}: {v}</span>' for k, v in sorted(f["dist"].items(), key=lambda kv: -kv[1]))
        body.append(f"<tr><td>{e(f['fact'])}{' <span class=chip>новый</span>' if f['new'] else ''}<div class='small'>{e(f['why'])}</div></td>"
                    f"<td>{e(' / '.join(f['gold']))}</td><td>{dist}</td><td>{_pct(f['agreement'])}</td><td>{_pct(f['correct'])}</td></tr>")
    body.append("</table><p class='foot'>Согласованность — доля прогонов, где модель приняла самое частое для этого факта решение (100% — "
                "модель стабильна). Блок «до → после» считается на одних и тех же ответах модели: меняется только логика эмулятора, "
                "которая эти ответы применяет.</p>")
    return page("Agent Memory Eval — стабильность аннотатора", "".join(body))
