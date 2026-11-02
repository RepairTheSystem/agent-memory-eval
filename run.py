#!/usr/bin/env python3
"""Agent Memory Eval: offline checks and optional public-model experiments."""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from memeval import report  # noqa: E402


def _load_env_file(name="openrouter.env"):
    """Подхватывает openrouter.env / openrouter.env рядом с run.py (KEY=VALUE): export/set в терминале не нужен."""
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file("openrouter.env")


def _write(name, html_text, data):
    os.makedirs(os.path.join(HERE, "reports"), exist_ok=True)
    path = os.path.join(HERE, "reports", name)
    with open(path + ".html", "w", encoding="utf-8") as f:
        f.write(html_text)
    with open(path + ".json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, default=str)
    print(f"\nОтчёт: {path}.html")


def cmd_mock(args):
    from memeval.mock_scenarios import run_all
    rows = run_all()
    for r in rows:
        print(f"{r['title']:<48} до: {'OK  ' if r['before']['passed'] else 'FAIL'}  "
              f"только эта правка: {'OK  ' if r['only']['passed'] else 'FAIL'}  после: {'OK' if r['after']['passed'] else 'FAIL'}")
    _write(args.out or "mock", report.render_mock(rows), rows)
    return 0 if all(r["after"]["passed"] for r in rows) else 1


def _llm(args=None):
    if getattr(args, "provider", "fake") == "fake":
        from memeval.fake_llm import FakeLLM
        return FakeLLM(model=getattr(args, "model", None))
    if getattr(args, "provider", "fake") == "openrouter":
        from memeval.openrouter import OpenRouterLLM
        return OpenRouterLLM(model=getattr(args, "model", None))
    raise ValueError("Supported providers: fake, openrouter")


def _agent_prompt(path):
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        return f.read()


def cmd_ping(args):
    llm = _llm(args)
    for line in llm.ping():
        print(line)


def cmd_live(args):
    from memeval import live
    llm = _llm(args)
    scenarios = live.load_scenarios(args.scenarios)
    variants = tuple(args.variants)
    for v in variants:
        if v not in live.VARIANTS:
            raise SystemExit(f"неизвестный вариант '{v}'; доступны: {', '.join(live.VARIANTS)}")
    judge = None if args.no_judge else llm
    echo = lambda m: print(m, flush=True)  # noqa: E731
    rows = live.run_bench(llm, scenarios, repeats=args.repeats, only=args.only,
                          agent_prompt=_agent_prompt(args.agent_prompt), variants=variants,
                          judge=judge, workers=args.workers, progress=echo)
    summ = live.summarize(rows, variants)
    echo("\nПройдено (без демонстрационных кейсов): " + ", ".join(
        f"{live.VARIANTS[v][0]} {summ[v]['passed']}/{summ[v]['n']} = {summ[v]['accuracy']:.0%} "
        f"[95% ДИ {summ[v]['ci95'][0]:.0%}–{summ[v]['ci95'][1]:.0%}]" for v in variants))
    for v in variants[1:]:
        m = summ[v]["vs_" + variants[0]]
        echo(f"{live.VARIANTS[v][0]} против «{live.VARIANTS[variants[0]][0]}»: починилось {m['fixed']}, сломалось "
             f"{m['broken']}, p = {m['p_value']:.3g} (тест Макнемара){' — значимо' if m['significant'] else ''}")
    for v in variants:
        c = summ[v]["cost"]
        echo(f"Стоимость, {live.VARIANTS[v][0]}: вызовов модели на сообщение {c['calls_per_message']:.1f} "
             f"(запись {c['write_calls_per_message']:.1f}, агент {c['chat_calls_per_message']:.1f}), "
             f"на сессию {c['calls_per_session']:.1f}; ≈ токенов промпта на сообщение {c['prompt_tokens_per_message']:.0f}")
    for v in variants:
        vol = summ[v].get("volume")
        if vol:
            echo(f"Объём, {live.VARIANTS[v][0]}: вопросов верно {vol['questions_ok']}/{vol['questions']}, сохранность "
                 f"фактов в конце {vol['retention_final']:.0%} (худшая точка {vol['retention_min']:.0%}), устаревших "
                 f"осталось {vol['stale_final']} из {vol['gone_final']}")
    # холостой прогон не затирает отчёт живого прогона
    out = args.out or ("live-fake" if args.provider == "fake" else "live")
    _write(out, report.render_live(rows, summ, {"model": llm.served_model or llm.model,
                                                               "repeats": args.repeats, "variants": variants,
                                                               "judge": None if args.no_judge else "та же модель"}),
           {"summary": summ, "rows": rows, "variants": list(variants)})


def cmd_public(args):
    """Прогон на публичном наборе (LoCoMo / LongMemEval) — для статистической значимости до/после."""
    from memeval import prompts, public_bench as pb, report_public
    topics = prompts.activate(args.lang)
    llm = _llm(args)
    judge = None
    if not args.no_judge:
        judge = llm if not args.judge_model else type(llm)(model=args.judge_model)
    if args.provider == "fake" and not args.no_judge:
        judge = llm
    groups, kind = pb.load_groups(args.data, dataset=args.dataset, limit_groups=args.groups,
                                  max_sessions=args.max_sessions, max_questions=args.max_questions, seed=args.seed)
    if not groups:
        raise SystemExit("в наборе не осталось вопросов — ослабьте --max-sessions / --max-questions")
    variants = tuple(args.variants)
    from memeval import live
    for v in variants:
        if v not in live.VARIANTS:
            raise SystemExit(f"неизвестный вариант '{v}'; доступны: {', '.join(live.VARIANTS)}")
    echo = lambda m: print(m, flush=True)  # noqa: E731
    if kind == "locomo" and args.lang == "en":
        # в LoCoMo двое собеседников: без имён в фактах вопросы про конкретного человека не отвечаемы.
        # Добавка одинакова для всех вариантов, поэтому сравнение остаётся честным.
        prompts.COLLECT_PROMPT += pb.COLLECT_ADDENDUM_TWO_SPEAKERS
    agent_prompt = _agent_prompt(args.agent_prompt) or (pb.AGENT_PROMPT_PUBLIC if args.lang == "en" else None)
    rows, gstats = pb.run_public(llm, groups, variants=variants, judge_llm=judge, workers=args.workers,
                                 chunk=args.chunk, topics=topics, progress=echo, agent_prompt=agent_prompt)
    summ = pb.summarize_public(rows, variants)
    pb.print_summary(summ, variants, echo)
    echo(f"\nзапросов к модели: {getattr(llm, 'usage', {})}")
    meta = {"dataset": kind, "path": args.data, "variants": list(variants),
            "model": llm.served_model or getattr(llm, "model", ""),
            "judge_model": None if args.no_judge else (args.judge_model or getattr(llm, "model", "")),
            "group_stats": gstats, "usage": getattr(llm, "usage", {}), "lang": args.lang}
    _write(args.out or "public", report_public.render_public(rows, summ, meta),
           {"summary": summ, "meta": meta, "rows": rows})


def cmd_transcripts(args):
    from memeval.transcripts import render_file
    src = args.src or os.path.join(HERE, "reports", "live.json")
    dst = os.path.join(HERE, "reports", (args.out or "transcripts") + ".html")
    print("Страница с диалогами:", render_file(src, dst, args.title))


def cmd_stability(args):
    from memeval import live
    llm = _llm(args)
    res = live.run_stability(llm, n=args.n)
    _write(args.out or "stability", report.render_stability(res, {"model": llm.served_model or llm.model}), res)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    pg = sub.add_parser("ping", help="проверить доступ к модели")
    pg.add_argument("--provider", default="fake", choices=["openrouter", "fake"])
    pg.add_argument("--model")
    m = sub.add_parser("mock", help="детерминированные сценарии без модели")
    m.add_argument("--out")
    lv = sub.add_parser("live", help="прогон сценариев с выбранной моделью")
    lv.add_argument("--scenarios", default=os.path.join(HERE, "scenarios", "benchmark_255.json"))
    lv.add_argument("--repeats", type=int, default=1)
    lv.add_argument("--provider", default="fake", choices=["openrouter", "fake"],
                    help="fake — холостой прогон без сети: проверить сценарии и обвязку")
    lv.add_argument("--only", nargs="*", help="категории (IE KU DEL HYPO ISO ...) или id сценариев")
    lv.add_argument("--variants", nargs="*", default=["before", "after"],
                    help="какие версии логики прогнать: before, after, nofilter, lite (шаг 2), ctx (шаг 3), "
                         "profile (шаг 4), full (шаг 5), search (шаг 6)")
    lv.add_argument("--agent-prompt", help="необязательный файл с пользовательским системным промптом")
    lv.add_argument("--workers", type=int, default=1, help="сколько сценариев гонять параллельно (у каждого своя память)")
    lv.add_argument("--no-judge", action="store_true",
                    help="без судьи: «не должно быть» и «не знаю» проверяются только по словам")
    lv.add_argument("--out")
    pb_ = sub.add_parser("public", help="прогон на публичном наборе (LoCoMo / LongMemEval) через OpenRouter")
    pb_.add_argument("--data", required=True, help="путь к locomo10.json или longmemeval_oracle.json")
    pb_.add_argument("--dataset", default="auto", choices=["auto", "locomo", "longmemeval"])
    pb_.add_argument("--provider", default="openrouter", choices=["openrouter", "fake"],
                     help="fake — холостой прогон без сети: проверить набор и обвязку")
    pb_.add_argument("--model", help="модель агента (по умолчанию из OPENROUTER_MODEL)")
    pb_.add_argument("--judge-model", help="модель-судья (по умолчанию та же)")
    pb_.add_argument("--no-judge", action="store_true", help="без судьи: проверка по вхождению эталона")
    pb_.add_argument("--variants", nargs="*", default=["before", "nofilter"])
    pb_.add_argument("--groups", type=int, help="сколько историй диалогов взять")
    pb_.add_argument("--max-sessions", type=int, help="сколько сессий каждой истории проигрывать")
    pb_.add_argument("--max-questions", type=int, help="сколько вопросов на историю (случайная выборка)")
    pb_.add_argument("--workers", type=int, default=8, help="сколько запросов к модели параллельно")
    pb_.add_argument("--chunk", type=int, default=4, help="окно сообщений для сбора фактов")
    pb_.add_argument("--lang", default="en", choices=["en", "ru"], help="язык промптов памяти")
    pb_.add_argument("--agent-prompt", help="файл с системным промптом агента (по умолчанию — промпт для публичных наборов)")
    pb_.add_argument("--seed", type=int, default=0)
    pb_.add_argument("--out")
    tr = sub.add_parser("transcripts", help="ч/б страница только с диалогами, без цифр")
    tr.add_argument("--src", help="какой прогон взять (по умолчанию reports/live.json)")
    tr.add_argument("--title", help="заголовок страницы")
    tr.add_argument("--out")
    st = sub.add_parser("stability", help="стабильность аннотатора")
    st.add_argument("--n", type=int, default=10)
    st.add_argument("--provider", default="fake", choices=["openrouter", "fake"])
    st.add_argument("--model")
    st.add_argument("--out")
    args = p.parse_args()
    return {"ping": cmd_ping, "mock": cmd_mock, "live": cmd_live, "public": cmd_public,
            "transcripts": cmd_transcripts, "stability": cmd_stability}[args.cmd](args) or 0


if __name__ == "__main__":
    sys.exit(main())
