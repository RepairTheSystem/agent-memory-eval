"""Прогон того же эмулятора памяти на публичных наборах диалогов (LoCoMo, LongMemEval).

Зачем: наш набор из 35 сценариев маленький, и разница «до/после» на нём статистически слабая.
Публичные наборы дают сотни вопросов к одной и той же истории диалогов — на них парный тест
Макнемара даёт настоящую значимость.

Схема прогона (для каждого варианта логики — своя чистая база памяти):
  1) загрузка: диалоги набора «проигрываются» как история — сбор фактов окнами по 4 сообщения,
     после каждой сессии запускается разбор (аннотатор + классификатор), как фоновая обработка в проде;
  2) вопросы: каждый вопрос задаётся в НОВОМ диалоге, память читается через функцию, как в проде;
  3) проверка: ответ сверяется с эталоном моделью-судьёй, плюс считаются token-F1 и вхождение эталона.

Что НЕ меняется между вариантами: набор, промпты, модель, порядок сообщений, судья. Меняется только
логика памяти (флаги Fixes), поэтому разница в точности относится к правкам.
"""
import copy
import json
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import prompts
from . import prompts_profile as pp
from .db import MemoryDB
from .live import VARIANTS
from .pipeline import TZ, Clock, MemorySystem
from .stats import mcnemar, wilson

USER = "user-1"
CHUNK = 4  # окно сообщений для сбора фактов (в проде берутся последние 4)

LOCOMO_CATEGORIES = {1: "multi-hop", 2: "temporal", 3: "open-domain", 4: "single-hop", 5: "adversarial"}

ABSTAIN_MARKERS = ["i don't know", "i do not know", "no information", "not mentioned", "no record", "don't have",
                   "do not have", "not in my memory", "no data", "unknown", "nothing about", "can't find",
                   "cannot find", "wasn't mentioned", "no details", "unable to find", "не знаю", "нет информации"]


# ====================================================================== загрузка наборов

def detect_dataset(data):
    sample = data[0] if isinstance(data, list) else data
    if isinstance(sample, dict) and "conversation" in sample:
        return "locomo"
    if isinstance(sample, dict) and "haystack_sessions" in sample:
        return "longmemeval"
    raise SystemExit("не удалось определить набор: ожидается locomo10.json или longmemeval_*.json")


def load_groups(path, dataset="auto", limit_groups=None, max_sessions=None, max_questions=None, seed=0):
    """Возвращает список «групп»: общая история диалогов + вопросы к ней.

    Группа — единица загрузки памяти: историю достаточно проиграть один раз на вариант,
    а вопросов к ней может быть много (у LoCoMo — около 200 на разговор).
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    kind = detect_dataset(data) if dataset == "auto" else dataset
    groups = _load_locomo(data) if kind == "locomo" else _load_longmemeval(data)
    rnd = random.Random(seed)
    if limit_groups:
        groups = groups[:limit_groups]
    for g in groups:
        if max_sessions:
            g["sessions"] = _trim_sessions(g["sessions"], max_sessions)
            keep = {s["dialog"] for s in g["sessions"]}
            if kind == "locomo":  # вопросы с опорой на отрезанные сессии больше не отвечаемы — убираем
                g["questions"] = [q for q in g["questions"]
                                  if not q.get("evidence") or all(e in keep for e in q["evidence"])]
        if max_questions and len(g["questions"]) > max_questions:
            g["questions"] = rnd.sample(g["questions"], max_questions)
    return [g for g in groups if g["questions"] and g["sessions"]], kind


def _trim_sessions(sessions, limit):
    """Укорачивает историю до limit сессий, НИКОГДА не выбрасывая сессию с ответом (has_answer).

    В longmemeval нужная сессия может лежать в конце среди полусотни отвлекающих: наивное
    sessions[:limit] выбросило бы именно её, и все вопросы честно провалились бы.
    """
    if len(sessions) <= limit:
        return sessions
    needed = [s for s in sessions if s.get("has_answer")]
    rest = [s for s in sessions if not s.get("has_answer")]
    chosen = needed[:limit] + rest[:max(0, limit - len(needed))]
    order = {id(s): i for i, s in enumerate(sessions)}
    return sorted(chosen, key=lambda s: order[id(s)])   # порядок во времени сохраняем


def _load_locomo(data):
    """LoCoMo: 10 разговоров двух собеседников по ~20 сессий, ~200 вопросов на разговор."""
    groups = []
    for i, sample in enumerate(data):
        conv = sample["conversation"]
        a, b = conv.get("speaker_a", "A"), conv.get("speaker_b", "B")
        sessions, dates = [], {}
        for n in range(1, 60):
            turns_raw = conv.get(f"session_{n}")
            if not turns_raw:
                continue
            date = conv.get(f"session_{n}_date_time", "")
            dates[f"D{n}"] = date
            turns = []
            for t in turns_raw:
                speaker = t.get("speaker", "")
                text = (t.get("text") or "").strip()
                if t.get("blip_caption"):
                    text += f" [shares a photo: {t['blip_caption']}]"
                turns.append({"role": "user" if speaker == b else "assistant", "text": f"{speaker}: {text}"})
            if turns:
                turns[0]["text"] = f"({date}) " + turns[0]["text"]
                sessions.append({"dialog": f"D{n}", "date": date, "turns": turns})
        questions = []
        for j, qa in enumerate(sample.get("qa") or []):
            cat = qa.get("category")
            adversarial = cat == 5
            gold = qa.get("adversarial_answer") if adversarial else qa.get("answer")
            if gold is None:
                continue
            questions.append({
                "id": f"{sample.get('sample_id', i)}-q{j}",
                "category": LOCOMO_CATEGORIES.get(cat, str(cat)),
                "question": qa["question"], "gold": str(gold), "abstain": adversarial,
                "evidence": sorted({str(e).split(":")[0] for e in (qa.get("evidence") or [])}),
                "date": sessions[-1]["date"] if sessions else "",
            })
        groups.append({"group_id": str(sample.get("sample_id", i)), "speakers": [a, b],
                       "sessions": sessions, "questions": questions, "dates": dates})
    return groups


def _load_longmemeval(data):
    """LongMemEval: на каждый вопрос своя история (в oracle-версии — только сессии с ответом)."""
    groups = []
    for item in data:
        sessions = []
        dates = item.get("haystack_dates") or []
        for n, turns_raw in enumerate(item.get("haystack_sessions") or []):
            date = dates[n] if n < len(dates) else ""
            turns = [{"role": t.get("role", "user"), "text": (t.get("content") or "").strip()}
                     for t in turns_raw if (t.get("content") or "").strip()]
            if not turns:
                continue
            turns[0]["text"] = f"({date}) " + turns[0]["text"]
            sessions.append({"dialog": f"D{n + 1}", "date": date, "turns": turns,
                             "has_answer": any(t.get("has_answer") for t in turns_raw)})
        qid = item.get("question_id", str(len(groups)))
        groups.append({
            "group_id": qid, "speakers": ["assistant", "user"], "sessions": sessions, "dates": {},
            "questions": [{"id": qid, "category": item.get("question_type", "?"), "question": item["question"],
                           "gold": str(item.get("answer", "")), "abstain": str(qid).endswith("_abs"),
                           "evidence": [], "date": item.get("question_date", "")}],
        })
    return groups


# ====================================================================== проверка ответа

_WORD = re.compile(r"[\w']+", re.UNICODE)


def token_f1(answer, gold):
    a, g = [w.lower() for w in _WORD.findall(answer)], [w.lower() for w in _WORD.findall(gold)]
    if not a or not g:
        return 0.0
    common = 0
    pool = list(g)
    for w in a:
        if w in pool:
            pool.remove(w)
            common += 1
    if not common:
        return 0.0
    prec, rec = common / len(a), common / len(g)
    return 2 * prec * rec / (prec + rec)


JUDGE_PROMPT = """You grade the answer of a memory-augmented assistant.

Question: {question}
Reference answer: {gold}
Assistant answer: {answer}

Reply YES if the assistant answer contains the information of the reference answer (wording may differ, extra
details are fine). Reply NO if it contradicts the reference, misses it, or says it does not know.
Reply with one word: YES or NO."""

JUDGE_ABSTAIN_PROMPT = """You grade the answer of a memory-augmented assistant.

Question: {question}
The question cannot be answered from what the user has told the assistant, so the correct behaviour is to say
that there is no such information (or to ask for it), not to invent an answer.
Assistant answer: {answer}

Reply YES if the assistant admits it does not know / has no such information / asks the user for it.
Reply NO if it states a confident factual answer.
Reply with one word: YES or NO."""


_VERDICT = re.compile(r"\b(YES|NO)\b")


def _fallback_score(q, answer):
    if q["abstain"]:
        return any(m in answer.lower() for m in ABSTAIN_MARKERS), "без судьи: маркеры «не знаю»"
    return q.get("gold", "").lower() in answer.lower(), "без судьи: вхождение эталона"


def judge_answer(judge_llm, q, answer):
    """Возвращает (correct, как решили). Без модели-судьи — по вхождению эталона и маркерам «не знаю»."""
    answer = (answer or "").strip()
    if not answer:
        return False, "пустой ответ"
    if judge_llm is None:
        return _fallback_score(q, answer)
    tmpl = JUDGE_ABSTAIN_PROMPT if q["abstain"] else JUDGE_PROMPT
    prompt = tmpl.format(question=q["question"], gold=q.get("gold", ""), answer=answer[:2000])
    for attempt in range(2):
        try:
            raw = judge_llm.plain(prompt + ("\n\nAnswer with the single word YES or NO." if attempt else ""))
        except Exception as e:  # noqa: BLE001 — судья не должен ронять прогон
            return False, f"судья недоступен: {e}"
        hit = _VERDICT.search((raw or "").upper())
        if hit:  # вердикт может быть внутри фразы, а не только в начале
            return hit.group(1) == "YES", f"судья: {hit.group(1)}"
    ok, how = _fallback_score(q, answer)   # судья не дал внятного вердикта — считаем проверкой без судьи
    return ok, how + " (судья не ответил YES/NO)"


# ====================================================================== прогон

class _Progress:
    def __init__(self, total, echo=print):
        self.total, self.done, self.lock, self.echo, self.t0 = total, 0, threading.Lock(), echo, time.time()

    def note(self, msg):
        with self.lock:
            self.echo(f"    {msg}")

    def step(self, msg):
        with self.lock:
            self.done += 1
            el = time.time() - self.t0
            eta = el / self.done * (self.total - self.done)
            self.echo(f"[{self.done}/{self.total}] {msg} (прошло {el / 60:.1f} мин, осталось ~{eta / 60:.1f} мин)")


def ingest(system, group, chunk=CHUNK, note=None):
    """Проигрывает историю диалогов: сбор фактов окнами и разбор после каждой сессии."""
    stats = {"collect_calls": 0, "handler_runs": 0, "handler_crashes": 0, "handler_skips": 0}
    total = len(group["sessions"])
    for k, sess in enumerate(group["sessions"], 1):
        msgs = [{"role": t["role"], "content": t["text"]} for t in sess["turns"]]
        windows = [msgs[i:i + chunk] for i in range(0, len(msgs), chunk)]
        for wi, window in enumerate(windows, 1):
            if not any(m["role"] == "user" for m in window):
                continue
            system.collect(USER, sess["dialog"], window)
            stats["collect_calls"] += 1
            if note:
                note(f"загрузка истории: сессия {k}/{total}, окно {wi}/{len(windows)}")
        for _ in range(2):  # разбор может пропустить пачку (чужой PROCESSING) — как в проде, пробуем дважды
            try:
                res = system.handler(USER)
            except Exception as e:  # noqa: BLE001 — страховка: сбой разбора не прекращает загрузку истории
                res = {"status": "crashed", "reason": f"{type(e).__name__}: {e}"}
            stats["handler_runs"] += 1
            stats["handler_crashes"] += res["status"] == "crashed"
            stats["handler_skips"] += res["status"] == "skipped"
            if res["status"] != "crashed":
                break
        system.clock.advance(24 * 3600)
        if note:
            note(f"загрузка истории: сессия {k}/{total}, фактов в памяти "
                 f"{len(system.db.user_facts(USER))}, падений разбора {stats['handler_crashes']}")
    facts = system.db.user_facts(USER)
    stats["facts_total"] = len(facts)
    stats["facts_hidden_by_old_reader"] = sum(
        1 for f in facts if f["status"] != "PROCESSED" or f["type"] is None or f["topic_guid"] is None)
    return stats


def parse_date(text):
    """Дата сессии или вопроса набора → время. LongMemEval: «2023/05/20 (Sat) 02:21», LoCoMo: «1:56 pm on 8 May, 2023»."""
    s = (text or "").strip()
    for fmt in ("%Y/%m/%d (%a) %H:%M", "%I:%M %p on %d %B, %Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=TZ).timestamp()
        except ValueError:
            continue
    return None


def ingest_profile(system, group, note=None):
    """Целевая схема: сессии набора проигрываются по своим датам, каждая заканчивается разбором сессии.

    Реплики ассистента берутся из набора (модель их не генерирует). Сессии идут в хронологическом порядке:
    в LongMemEval порядок в файле не всегда совпадает с датами."""
    from .live import USER as _U  # noqa: F401 — тот же пользователь, что и в живом бенче
    sessions = sorted(group["sessions"], key=lambda s: parse_date(s.get("date")) or 0)
    total = len(sessions)
    first = next((parse_date(x.get("date")) for x in sessions if parse_date(x.get("date"))), None)
    if first:
        system.clock.t = first - 3600  # часы — с даты первой сессии набора
    for k, sess in enumerate(sessions, 1):
        ts = parse_date(sess.get("date"))
        if ts:
            # сессии набора могут идти почти подряд: часы уже ушли вперёд на минуту за реплику — тогда просто дальше
            system.clock.t = max(system.clock.t, ts)
        else:
            system.clock.advance(24 * 3600)
        prefix = f"({sess.get('date')}) "
        hist = system.dialogs.setdefault(sess["dialog"], [])
        for t in sess["turns"]:
            text = t["text"][len(prefix):] if t["text"].startswith(prefix) else t["text"]
            system.msg_log.append({"user": USER, "dialog": sess["dialog"], "t": system.clock.t, "role": t["role"],
                                   "text": text})
            hist.append({"role": t["role"], "content": text})
            system.clock.advance(60)
        system.session_end(USER, sess["dialog"])
        if note:
            note(f"загрузка истории: сессия {k}/{total}, строк профиля {len(system._prof(USER)['lines'])}")
    journal = system.journal
    return {"facts_total": len(system._prof(USER)["lines"]), "archive": len(system._prof(USER)["archive"]),
            "ops_accepted": sum(j["verdict"] == "принята" for j in journal),
            "ops_rejected": sum(j["verdict"] != "принята" for j in journal),
            "handler_crashes": sum("разбор упал" in e for e in system.events), "collect_calls": 0,
            "handler_runs": total, "handler_skips": 0, "facts_hidden_by_old_reader": 0}


def ask_profile(base, llm, fixes, q, base_prompt, lock):
    """Вопрос к готовому профилю: копия профиля, часы — на дату вопроса, вопрос в новом диалоге."""
    from .profile import ProfileSystem
    with lock:
        s = ProfileSystem(llm, fixes, agent_prompt=base_prompt + (f" Today is {q['date']}." if q.get("date") else ""))
        s.profiles = copy.deepcopy(base.profiles)
        s.msg_log = list(base.msg_log)
        s.processed_until = dict(base.processed_until)
        s.clock = Clock(max(base.clock.t, parse_date(q.get("date")) or 0))
    t0 = time.time()
    try:
        turn = s.user_turn(USER, f"q-{q['id']}", q["question"], collect=False)
    except Exception as e:  # noqa: BLE001
        return {"answer": "", "error": f"{type(e).__name__}: {e}", "topics": [], "facts_returned": 0,
                "empty": True, "seconds": round(time.time() - t0, 1), "memory_called": False}
    view, info = s.read_view(USER, f"q-{q['id']}")
    calls = turn.get("commands", [])
    return {"answer": turn["text"], "error": turn.get("error"), "topics": [c["name"] for c in calls],
            "memory_called": bool(calls), "facts_returned": info.get("строк профиля", 0),
            "facts": [ln for ln in view.splitlines() if ln.startswith("- ")][:40]
            + [f"{c['name']}({c.get('text')!r}) → {json.dumps(c['result'], ensure_ascii=False)[:300]}" for c in calls],
            "empty": turn["empty"], "seconds": round(time.time() - t0, 1)}


def ask(base, llm, fixes, q, base_prompt, lock):
    """Один вопрос к готовому состоянию памяти. База копируется, чтобы вопросы шли параллельно."""
    with lock:
        db = base.db.snapshot()
    sysprompt = base_prompt + (f" Today is {q['date']}." if q.get("date") else "")
    s = MemorySystem(llm, fixes, db=db, clock=Clock(base.clock.t), agent_prompt=sysprompt)
    t0 = time.time()
    try:
        turn = s.user_turn(USER, f"q-{q['id']}", q["question"], collect=False)
    except Exception as e:  # noqa: BLE001
        return {"answer": "", "error": f"{type(e).__name__}: {e}", "topics": [], "facts_returned": 0,
                "empty": True, "seconds": round(time.time() - t0, 1), "memory_called": False}
    calls = turn["memory_calls"]
    return {"answer": turn["text"], "error": turn.get("error"),
            "topics": [t for c in calls for t in c["topics"]], "memory_called": bool(calls),
            "facts_returned": sum(len(c["facts"]) for c in calls),
            "facts": [f"{f['text']} ({f['topic']})" for c in calls for f in c["facts"]][:40],
            "empty": turn["empty"], "seconds": round(time.time() - t0, 1)}


def estimate(groups, variants, chunk=CHUNK, with_judge=True):
    """Грубая оценка числа запросов к модели — чтобы заранее понимать объём и стоимость прогона."""
    windows = sum(max(1, -(-len(sess["turns"]) // chunk)) for g in groups for sess in g["sessions"])
    sessions = sum(len(g["sessions"]) for g in groups)
    questions = sum(len(g["questions"]) for g in groups)
    per_variant = windows + 2 * sessions + 2 * questions   # сбор + (аннотатор, классификатор) + (вызов функции, ответ)
    total = per_variant * len(variants) + (questions * len(variants) if with_judge else 0)
    return (f"ожидается ~{total} запросов к модели "
            f"(загрузка истории ~{(windows + 2 * sessions) * len(variants)}, ответы ~{2 * questions * len(variants)}"
            + (f", судья ~{questions * len(variants)}" if with_judge else "") + ")")


COLLECT_ADDENDUM_TWO_SPEAKERS = (
    "\n\nIMPORTANT: this dialogue has two named participants, and every line starts with the speaker's name. "
    "Begin every fact with the name of the person it is about (for example \"Melanie moved to Portland\", "
    "\"Caroline is learning pottery\"). Never write \"The user\": with two participants it is ambiguous and the "
    "fact becomes unusable."
)

AGENT_PROMPT_PUBLIC = (
    "You are a personal assistant with a long-term memory of past conversations. "
    "ALWAYS call the memory function before answering any question about a person, an event or a preference — "
    "you have no other source of information about them. The facts you get back are named after the person they "
    "are about. Answer from those facts, briefly and concretely. "
    "If the facts do not contain the answer, say honestly that you do not know and do not make anything up."
)


def run_public(llm, groups, variants=("before", "nofilter"), judge_llm=None, workers=8, chunk=CHUNK,
               topics=None, progress=print, agent_prompt=None):
    """Прогоняет все группы на всех вариантах. Возвращает (rows, group_stats)."""
    tasks = [(g, v) for g in groups for v in variants]
    nq = sum(len(g["questions"]) for g in groups) * len(variants)
    progress(f"групп: {len(groups)}, вариантов: {len(variants)}, вопросов всего: {nq}; "
             + estimate(groups, variants, chunk, judge_llm is not None))
    bar = _Progress(len(tasks), progress)
    rows = {}            # (group_id, question_id) -> строка результата
    group_stats = {}
    rows_lock, qpool = threading.Lock(), ThreadPoolExecutor(max_workers=workers)
    stop = threading.Event()

    def heartbeat():
        """Пульс раз в 30 секунд: видно, что запросы идут, и видно, если прогон встал."""
        prev = 0
        while not stop.wait(30):
            u = getattr(llm, "usage", {}) or {}
            calls = u.get("calls", 0)
            msg = (f"пульс: запросов к модели {calls} (+{calls - prev} за 30 с), "
                   f"повторов {u.get('retries', 0)}, неудачных {u.get('failed', 0)}")
            if calls == prev:
                msg += " — ответов нет, проверьте токен, имя модели и сеть"
            prev = calls
            bar.note(msg)

    hb = threading.Thread(target=heartbeat, daemon=True)
    hb.start()

    def one(group, variant):
        fixes = VARIANTS[variant][1]()
        t0 = time.time()
        tag = f"{group['group_id']} {variant}"
        base_prompt = agent_prompt or prompts.AGENT_SYSTEM_PROMPT
        if fixes.prompt_memory and base_prompt == AGENT_PROMPT_PUBLIC:
            base_prompt = pp.AGENT_SYSTEM_PROMPT_PROFILE  # функции памяти нет — промпт «всегда вызывай функцию» не подходит
        if fixes.profile:
            from .profile import ProfileSystem
            base = ProfileSystem(llm, fixes, db=MemoryDB(topics=topics), clock=Clock())
            base.timed = True
            st = ingest_profile(base, group, note=lambda m: bar.note(f"{tag} · {m}"))
            asker = ask_profile
        else:
            base = MemorySystem(llm, fixes, db=MemoryDB(topics=topics), clock=Clock())
            st = ingest(base, group, chunk=chunk, note=lambda m: bar.note(f"{tag} · {m}"))
            asker = ask
        st["ingest_minutes"] = round((time.time() - t0) / 60, 1)
        snap_lock = threading.Lock()
        futures = [(q, qpool.submit(asker, base, llm, fixes, q, base_prompt, snap_lock)) for q in group["questions"]]
        bar.note(f"{tag} · история загружена ({st['facts_total']} фактов, {st['ingest_minutes']} мин), "
                 f"пошли вопросы: {len(futures)}")
        answered, right, last = 0, 0, time.time()
        for q, fut in futures:
            r = fut.result()
            correct, how = judge_answer(judge_llm, q, r["answer"])
            r.update({"correct": correct, "judge": how, "f1": round(token_f1(r["answer"], q.get("gold", "")), 3),
                      "contains": q.get("gold", "").lower() in (r["answer"] or "").lower()})
            answered += 1
            right += bool(correct)
            if answered % 10 == 0 or time.time() - last > 60 or answered == len(futures):
                last = time.time()
                bar.note(f"{tag} · вопросов {answered}/{len(futures)}, верных {right} ({right / answered:.0%})")
            key = (group["group_id"], q["id"])
            with rows_lock:
                row = rows.setdefault(key, {"group": group["group_id"], "id": q["id"], "category": q["category"],
                                            "question": q["question"], "gold": q.get("gold", ""),
                                            "abstain": q["abstain"], "runs": {}})
                row["runs"][variant] = r
        with rows_lock:
            group_stats[(group["group_id"], variant)] = st
        acc = sum(rows[(group["group_id"], q["id"])]["runs"][variant]["correct"] for q in group["questions"])
        bar.step(f"{group['group_id']} {variant}: {acc}/{len(group['questions'])} верных, "
                 f"фактов в памяти {st['facts_total']}, падений разбора {st['handler_crashes']}")

    def safe(task):
        try:
            one(*task)
        except Exception as e:  # noqa: BLE001 — одна упавшая группа не должна убивать прогон
            bar.step(f"{task[0]['group_id']} {task[1]}: ОШИБКА {type(e).__name__}: {e}")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(safe, tasks))
    stop.set()
    qpool.shutdown()
    ordered = [rows[k] for k in sorted(rows, key=lambda k: (str(k[0]), str(k[1])))]
    return ordered, {f"{g}|{v}": s for (g, v), s in group_stats.items()}


def summarize_public(rows, variants):
    cats = sorted({r["category"] for r in rows}, key=str)

    def agg(v):
        runs = [r["runs"][v] for r in rows if v in r["runs"]]
        n = len(runs) or 1
        ok = sum(r["correct"] for r in runs)
        lo, hi = wilson(ok, len(runs))
        topics = [len(r["topics"]) for r in runs if r["memory_called"]]
        return {"n": len(runs), "correct": ok, "accuracy": ok / n, "ci": [round(lo, 3), round(hi, 3)],
                "empty": sum(r["empty"] for r in runs), "errors": sum(bool(r.get("error")) for r in runs),
                "memory_call_rate": sum(r["memory_called"] for r in runs) / n,
                "empty_topic_calls": sum(1 for r in runs if r["memory_called"] and not r["topics"]),
                "truncated": sum(1 for r in runs if (r["answer"] or "").strip()[-1:] not in ("", ".", "!", "?", '"')),
                "avg_topics": round(sum(topics) / len(topics), 2) if topics else 0,
                "avg_facts_returned": round(sum(r["facts_returned"] for r in runs) / n, 1),
                "avg_f1": round(sum(r["f1"] for r in runs) / n, 3),
                "by_cat": {c: (sum(r["runs"][v]["correct"] for r in rows if r["category"] == c and v in r["runs"]),
                               sum(1 for r in rows if r["category"] == c and v in r["runs"])) for c in cats},
                "seconds": round(sum(r["seconds"] for r in runs), 1)}

    summ = {v: agg(v) for v in variants}
    pairs = {}
    for i, a in enumerate(variants):
        for b in variants[i + 1:]:
            both = [(r["runs"][a]["correct"], r["runs"][b]["correct"]) for r in rows
                    if a in r["runs"] and b in r["runs"]]
            pairs[f"{a} → {b}"] = mcnemar(both)
    return {"variants": summ, "mcnemar": pairs, "categories": cats, "questions": len(rows)}


def print_summary(summ, variants, echo=print):
    echo("")
    for v in variants:
        s = summ["variants"][v]
        echo(f"{VARIANTS[v][0]:<46} {s['correct']}/{s['n']} = {s['accuracy']:.1%} "
             f"(95% ДИ {s['ci'][0]:.1%}–{s['ci'][1]:.1%}), token-F1 {s['avg_f1']}, "
             f"тем за вызов {s['avg_topics']}, фактов на ответ {s['avg_facts_returned']}")
    echo("")
    for name, m in summ["mcnemar"].items():
        verdict = "значимо" if m["significant"] else "НЕ значимо"
        echo(f"Макнемар {name}: починилось {m['fixed']}, сломалось {m['broken']}, "
             f"p = {m['p_value']:.2g} ({verdict}), разница {m['delta']:+.1%}")
    echo("")
    header = "категория".ljust(18) + "".join(f"{VARIANTS[v][0][:18]:>20}" for v in variants)
    echo(header)
    for c in summ["categories"]:
        line = str(c).ljust(18)
        for v in variants:
            ok, tot = summ["variants"][v]["by_cat"][c]
            line += f"{ok}/{tot} ({ok / (tot or 1):.0%})".rjust(20)
        echo(line)
