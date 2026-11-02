"""HTML-отчёт по прогону на публичном наборе: точность, значимость, расхождения по вопросам."""
from .live import VARIANTS
from .report import CSS, e, now, page

NL = "\n"


def _pct(x):
    return f"{x * 100:.1f}%"


def _kpis(summ, variants):
    out = []
    for v in variants:
        s = summ["variants"][v]
        out.append(f'<div class="kpi"><div class="v">{_pct(s["accuracy"])}</div>'
                   f'<div class="l">{e(VARIANTS[v][0])}<br><span class="small">{s["correct"]} из {s["n"]}, '
                   f'95% ДИ {_pct(s["ci"][0])}–{_pct(s["ci"][1])}</span></div></div>')
    return '<div class="kpis">' + "".join(out) + "</div>"


def _significance(summ):
    rows = ["<tr><th>Сравнение</th><th>Починилось</th><th>Сломалось</th><th>p-value</th><th>Разница</th>"
            "<th>Вывод</th></tr>"]
    for name, m in summ["mcnemar"].items():
        cls = "ok" if m["significant"] else "bad"
        rows.append(f'<tr><td>{e(name)}</td><td>{m["fixed"]}</td><td>{m["broken"]}</td>'
                    f'<td>{m["p_value"]:.2g}</td><td>{m["delta"]:+.1%}</td>'
                    f'<td><span class="b {cls}">{"значимо" if m["significant"] else "не значимо"}</span></td></tr>')
    return "<table>" + "".join(rows) + "</table>"


def _by_cat(summ, variants):
    head = "<tr><th>Категория вопросов</th>" + "".join(f"<th>{e(VARIANTS[v][0])}</th>" for v in variants) + "</tr>"
    rows = [head]
    for c in summ["categories"]:
        cells = ""
        for v in variants:
            ok, tot = summ["variants"][v]["by_cat"][c]
            cells += f'<td>{ok}/{tot} <span class="small">({ok / (tot or 1):.0%})</span></td>'
        rows.append(f"<tr><td>{e(c)}</td>{cells}</tr>")
    return "<table>" + "".join(rows) + "</table>"


def _behaviour(summ, variants):
    labels = [("memory_call_rate", "Память вызвана", _pct), ("avg_topics", "Тем за вызов", str),
              ("avg_facts_returned", "Фактов в ответе функции", str), ("avg_f1", "token-F1", str),
              ("empty", "Пустых ответов", str), ("errors", "Ошибок", str)]
    rows = ["<tr><th>Показатель</th>" + "".join(f"<th>{e(VARIANTS[v][0])}</th>" for v in variants) + "</tr>"]
    for key, title, fmt in labels:
        rows.append(f"<tr><td>{title}</td>" +
                    "".join(f"<td>{e(fmt(summ['variants'][v][key]))}</td>" for v in variants) + "</tr>")
    return "<table>" + "".join(rows) + "</table>"


def _question_rows(rows, variants, only_diff=True, limit=400):
    items = []
    for r in rows:
        res = [r["runs"].get(v, {}) for v in variants]
        if only_diff and len({bool(x.get("correct")) for x in res}) == 1:
            continue
        cells = ""
        for v, x in zip(variants, res):
            cls = "ok" if x.get("correct") else "bad"
            topics = " ".join(f'<span class="chip">{e(t)}</span>' for t in (x.get("topics") or [])[:8])
            cells += (f'<td><span class="b {cls}">{"верно" if x.get("correct") else "неверно"}</span>'
                      f'<div class="msg assistant">{e(x.get("answer") or "⟨пусто⟩")}</div>'
                      f'<div class="small">запрошенные темы: {topics or "—"} · '
                      f'вернулось {x.get("facts_returned", 0)} фактов</div>'
                      + (NL.join(["<details><summary>факты из памяти</summary><pre>",
                                     e(NL.join(x.get("facts") or [])), "</pre></details>"])
                         if x.get("facts") else "")
                      + "</td>")
        items.append(f'<tr><td><div class="small">{e(r["id"])} · {e(r["category"])}</div>'
                     f'<div>{e(r["question"])}</div>'
                     f'<div class="msg note"><span class="who">эталон</span><br>{e(r["gold"])}</div></td>{cells}</tr>')
        if len(items) >= limit:
            break
    head = "<tr><th>Вопрос и эталон</th>" + "".join(f"<th>{e(VARIANTS[v][0])}</th>" for v in variants) + "</tr>"
    return "<table>" + head + "".join(items) + "</table>"


def render_public(rows, summ, meta):
    variants = list(meta["variants"])
    groups = sorted({r["group"] for r in rows})
    gs = meta.get("group_stats") or {}
    facts = {v: sum(s["facts_total"] for k, s in gs.items() if k.endswith("|" + v)) for v in variants}
    crashes = {v: sum(s["handler_crashes"] for k, s in gs.items() if k.endswith("|" + v)) for v in variants}
    body = [
        f"<h1>Память: прогон на публичном наборе {e(meta.get('dataset', ''))}</h1>",
        f'<p class="sub">{len(rows)} вопросов · {len(groups)} историй диалогов · модель {e(meta.get("model", ""))}'
        f' · судья {e(meta.get("judge_model") or "без судьи (вхождение эталона)")} · {now()}</p>',
        _kpis(summ, variants),
        "<h2>Значимость разницы (парный тест Макнемара)</h2>",
        '<p class="sub">Оба варианта отвечают на одни и те же вопросы, поэтому считаются только расхождения: '
        'сколько вопросов починилось и сколько сломалось.</p>',
        _significance(summ),
        "<h2>По категориям вопросов набора</h2>", _by_cat(summ, variants),
        "<h2>Поведение памяти</h2>", _behaviour(summ, variants),
        '<div class="card"><h3>Загрузка истории</h3><p class="small">Фактов в памяти после проигрывания всех '
        'диалогов: ' + " · ".join(f"{e(VARIANTS[v][0])} — {facts[v]}" for v in variants) +
        '. Падений разбора: ' + " · ".join(f"{e(VARIANTS[v][0])} — {crashes[v]}" for v in variants) + "</p></div>",
        "<h2>Вопросы, где варианты разошлись</h2>",
        '<p class="sub">Полные ответы, запрошенные темы и факты, которые вернула функция памяти.</p>',
        _question_rows(rows, variants),
        f'<p class="foot">Набор: {e(meta.get("path", ""))}. Промпты сбора, разбора и чтения — те же, что в проде '
        f'(переведены на английский язык набора), одинаковые для всех вариантов; различается только логика памяти.</p>',
    ]
    return page("Память: публичный набор", "".join(body))
