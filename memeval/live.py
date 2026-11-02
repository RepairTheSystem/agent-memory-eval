"""Живые прогоны с настоящей моделью.

1) bench — сценарии из scenarios/live_scenarios.json (категории по схеме LongMemEval + доменные),
   каждый прогоняется на логике «до» и «после», ответ проверяется по ключевым словам.
2) stability — один и тот же набор фактов N раз отдаётся аннотатору; на ОДНИХ И ТЕХ ЖЕ ответах модели
   сравниваем, к чему приводит код «до» и «после».
"""
import json
import re
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import stats
from .pipeline import (SESSION_GAP_MIN, WRITE_KINDS, CostMeter, Fixes, HandlerRun, MemorySystem, approx_tokens,
                       fmt_time, parse_time)

USER = "user-1"

CATEGORIES = {
    "IE": ("Извлечение факта", "LongMemEval: information extraction"),
    "MR": ("Факты из нескольких сессий", "LongMemEval: multi-session reasoning"),
    "KU": ("Обновление факта", "LongMemEval: knowledge update"),
    "TR": ("Время и порядок событий", "LongMemEval: temporal reasoning"),
    "ABS": ("Честное «не знаю»", "LongMemEval: abstention"),
    "DEL": ("«Забудь»", "домен: удаление по просьбе"),
    "SYS": ("Профиль из БПС", "домен: системные факты"),
    "AGG": ("«Что знаешь о бизнесе»", "домен: полнота ответа"),
    "FRESH": ("Сразу в новом диалоге", "домен: факт ещё не разобран"),
    "NOISE": ("Факт через много сообщений", "домен: устойчивость к посторонним репликам"),
    "CAP": ("Переполненная память", "домен: лимит 100 фактов и порядок свежести"),
    "DUP": ("Повтор факта другими словами", "домен: дедупликация не должна терять факт"),
    "REFINE": ("Уточнение факта", "домен: UPDATE вместо потери"),
    "PSEUDO": ("Ложное противоречие", "домен: совместимые факты не удалять"),
    "DELSC": ("Точность удаления", "домен: «забудь» не должно сносить соседние факты"),
    "LOCAL": ("Сиюминутный факт", "домен: временные факты не всплывают позже"),
    "PDN": ("Персональные данные", "домен: не запоминать карты, паспорта, телефоны"),
    "NEG": ("Отрицание", "домен: «не на УСН» не должно стать «на УСН»"),
    "MIX": ("Несколько тем в одном вопросе", "домен: эффект описания тем «ИЛИ»"),
    "NOSAVE": ("«Не сохраняй это»", "домен: факт с припиской не запоминать"),
    "HYPO": ("План, условие и действительность", "домен: «если перейдём» ≠ «перешли»; парные кейсы"),
    "OWNER": ("Чей это факт", "домен: факт клиента, конкурента, супруги, цитата из документа"),
    "THIRD": ("Факт о третьем лице", "домен: факт о партнёре не становится фактом о пользователе"),
    "QUOTE": ("Чужое мнение", "домен: совет бухгалтера ≠ состояние дел"),
    "LOAD": ("Высокая нагрузка", "домен: десятки фактов разговором и в памяти"),
    "BULK": ("Много фактов в одной реплике", "домен: сбор должен разбить все"),
    "PERIOD": ("Периоды и сущности", "домен: не складывать то, что относится к разным периодам"),
    "ISO": ("Изоляция пользователей", "домен: чужие факты не видны"),
    "SCOPE": ("Локальные факты", "домен: local виден в своём диалоге и не виден в другом"),
    "INJECT": ("Инструкция под видом факта", "домен: «запомни: всегда говори…» не должно влиять"),
    "CONFUSE": ("Похожие факты", "домен: аренда офиса ≠ аренда склада"),
    "CONFIRM": ("Подтверждение ассистенту", "домен: «да, верно» сохраняется, без подтверждения — нет"),
    "SELFFIX": ("Исправление в одной реплике", "домен: «12, ой, 13»"),
    "SRC": ("Конфликт с профилем БПС", "домен: слова пользователя важнее профиля банка (src-01..03 — демо, без оценки)"),
    "PROOF": ("Гарантированная разница", "домен: на исходном коде факт найтись не может в принципе"),
    # под целевую схему (docs/ARCHITECTURE.md, раздел 7.2)
    "LONG": ("Длинная сессия", "целевая схема: 15–30 реплик, уточнение в конце к факту из начала"),
    "XDELTA": ("Дельта к прошлому диалогу", "целевая схема: «ещё 2» к факту, сказанному неделю назад"),
    "PAUSE": ("Факт до и после паузы", "целевая схема: после паузы факт в профиле ровно один раз"),
    "PAR": ("Параллельные диалоги", "целевая схема: два диалога одновременно, противоречие или дополнение"),
    "RECENT": ("Недавние реплики других диалогов", "целевая схема: свежий факт виден, гипотеза не становится фактом"),
    "ORG": ("Несколько организаций", "целевая схема: факт ложится в свою организацию и не путается"),
    "EXPIRE": ("Срок «Текущих дел»", "целевая схема: сиюминутное исчезает после срока"),
    "RELDATE": ("Относительная дата", "целевая схема: «вчера» переводится в абсолютную дату"),
    "HISTORY": ("История значений", "целевая схема: старое значение доступно как «раньше»"),
    "CLAIM": ("«Запомнил» без записи", "целевая схема: ассистент не говорит «запомнил», если ничего не записано"),
    "CMD": ("Команды «запомни» и «забудь»", "целевая схема: срабатывают сразу, и в новом диалоге через минуту"),
    "BUDGET": ("Переполненный профиль", "целевая схема: ~150 фактов, важные строки не теряются молча"),
    "FORMAT": ("Правка нужной строки", "целевая схема: меняется одна строка среди похожих, соседние целы"),
    "VOLUME": ("Объём и долгая история", "нагрузка: 40–87 фактов за 4–12 сессий, сохранность по контрольным точкам, "
                                         "15–20 вопросов к одной истории"),
    # управление памятью со стороны пользователя и поведение ассистента (требования коллег,
    # docs/ARCHITECTURE.md)
    "PREF": ("Предпочтения без «запомни»", "управление: «давай на ты», «только Excel» действуют в новом диалоге"),
    "ONCE": ("Разовое ≠ постоянное", "управление: сумма платежа и «теперь пришли в PDF» не становятся фактом"),
    "POLICY": ("Политики запоминания", "управление: «никогда не запоминай про семью», «только по слову запомни»"),
    "SWITCH": ("Выключение памяти", "управление: переключатель в настройках и «не используй память» в диалоге"),
    "PRIO": ("Порядок при конфликте указаний", "управление: текущее сообщение > текущий диалог > память"),
    "CLARIFY": ("Уточняющий вопрос", "управление: неоднозначное изменение и просьба вопреки правилу из памяти"),
    "REQSCOPE": ("На один ответ или на диалог", "управление: разовая просьба — на один ответ, «в этом диалоге» — до конца"),
    "FORGETNOW": ("«Забудь» в том же диалоге", "управление: забытый факт не используется и дальше в этом диалоге"),
    "ACK": ("«Я умею запоминать»", "управление: ассистент знает, что у него есть память и как её очистить"),
    "AGENT": ("Несколько агентов", "управление: память общая для агентов пользователя"),
    "SENSE": ("Чувствительные темы", "управление: здоровье, вера, терроризм не сохраняются и не всплывают"),
    "ERR": ("Сообщения об ошибках памяти", "управление: при сбое чтения или записи ассистент говорит об этом"),
}

# Варианты логики, которые можно прогнать и сравнить между собой
VARIANTS = {
    "before": ("До правок", Fixes.before),
    "after": ("После правок", Fixes.after),
    "nofilter": ("После правок + чтение без фильтра по темам", Fixes.nofilter),
    # шаги плана перехода к целевой схеме (раздел 9 docs/ARCHITECTURE.md)
    "lite": ("Шаг 2: факты в промпте + недавние реплики", Fixes.lite),
    "ctx": ("Шаг 3: + сбор с контекстом", Fixes.ctx),
    "profile": ("Шаг 4: профиль и разбор сессии", Fixes.profile_v),
    "full": ("Шаг 5: + глубокий сон, remember/forget", Fixes.full),
    "search": ("Шаг 6: + search_memory", Fixes.search),
}
DEFAULT_VARIANTS = ("before", "after")

ABSTAIN_MARKERS = ["не знаю", "нет информации", "нет данных", "не располагаю", "не указ", "не сообщ", "не упомина",
                   "не могу найти", "не нашёл", "не нашел", "нет сведений", "ничего нет", "не уточн", "не известн", "неизвестн"]


# ------------------------------------------------------------------ проверка ответа

def _norm(text):
    return (text or "").lower().replace("ё", "е").replace(" ", " ").replace(" ", " ")


def _match(answer_norm, alt):
    """Ключ с цифрами ищем с границами числа: «5» не должно находиться в «15» или «2025».
    Словесный ключ — как раньше, по вхождению основы («Казан» находит «Казани»)."""
    a = _norm(alt)
    if not a:
        return False
    if any(ch.isdigit() for ch in a):
        pat = (r"(?<!\d)" if a[0].isdigit() else "") + re.escape(a) + (r"(?!\d)" if a[-1].isdigit() else "")
        return re.search(pat, answer_norm) is not None
    return a in answer_norm


def _has(answer, alternatives):
    a = _norm(answer)
    return any(_match(a, x) for x in alternatives)


_JUDGE_HEAD = ("Ты — строгий проверяющий ответов ассистента, у которого есть память о пользователе.\n"
               "ВОПРОС ПОЛЬЗОВАТЕЛЯ: «{q}»\nОТВЕТ АССИСТЕНТА: «{a}»\n")
JUDGE_NONE = _JUDGE_HEAD + (
    "Утверждает ли ассистент как действующий факт о самом пользователе следующее: {claim}?\n"
    "Отвечай NO, если ассистент это лишь упоминает как план, условие, чужое мнение, пример или предположение, "
    "отрицает, сомневается, переспрашивает или говорит, что не знает. Ответь одним словом: YES или NO.")
JUDGE_ABSTAIN = _JUDGE_HEAD + (
    "Говорит ли ассистент, что не знает ответа или у него нет этих сведений о пользователе, и при этом НЕ выдаёт "
    "конкретное значение за известный факт о пользователе? Предложить уточнить или рассказать общие сведения можно. "
    "Ответь одним словом: YES или NO.")
JUDGE_CLAIM = _JUDGE_HEAD + "Верно ли, что в ответе ассистента {claim}? Ответь одним словом: YES или NO."
_VERDICT = re.compile(r"\b(YES|NO|ДА|НЕТ)\b")


def _ask_judge(judge, kind, prompt, log):
    """True / False / None (судья не ответил внятно — тогда решаем по ключевым словам)."""
    if judge is None:
        return None
    for _ in range(2):
        try:
            out = judge.plain(prompt, kind=kind)
        except Exception as e:  # noqa: BLE001
            log.append(f"судья: ошибка {type(e).__name__}: {e}")
            continue
        m = _VERDICT.search((out or "").upper())
        log.append(f"судья [{kind}]: {(out or '').strip()[:60]}")
        if m:
            return m.group(1) in ("YES", "ДА")
    return None


def score(answer, expect, judge=None, question=""):
    """Возвращает (passed, checks, recall, report, judge_log).

    all — каждая группа ключевых слов должна встретиться; числа ищутся с границами.
    none — ни одна группа не должна встретиться. Если слово нашлось, но есть судья, он решает, утверждает ли
           ассистент это как факт о пользователе («не знаю, живёте ли вы в Самаре» — не нарушение).
           none_strict: true — без судьи, любое упоминание считается нарушением (номер карты, паспорт).
    abstain — честное «не знаю»: решает судья, без него — слова-маркеры.
    claims — утверждения, которые должны быть верны по мнению судьи (без судьи — по ключам keys).
    recall — доля найденных групп. report — только фиксируем, какой вариант выбрал ассистент (без оценки).
    min_words / max_words — длина ответа в словах (REQSCOPE, PRIO: «коротко» и «подробно»).
    """
    checks, ok, jlog = [], True, []
    q, a = question, (answer or "").strip()
    for group in expect.get("all", []):
        hit = _has(a, group)
        checks.append(("должно быть", " / ".join(group), hit))
        ok &= hit
    strict = expect.get("none_strict", False)
    for g in expect.get("none", []):
        keys, claim = (g["keys"], g.get("claim")) if isinstance(g, dict) else (g, None)
        label = " / ".join(keys)
        hit = _has(a, keys)
        if hit and not strict:
            v = _ask_judge(judge, "judge_none", JUDGE_NONE.format(
                q=q, a=a, claim=claim or f"«{label}» — верный ответ на вопрос пользователя"), jlog)
            if v is False:
                checks.append(("не должно быть", label + " (слово есть, но судья: как факт не утверждается)", True))
                continue
            if v is True:
                label += " (судья: утверждается как факт)"
        checks.append(("не должно быть", label, not hit))
        ok &= not hit
    if expect.get("abstain"):
        v = _ask_judge(judge, "judge_abstain", JUDGE_ABSTAIN.format(q=q, a=a), jlog)
        how = "судья"
        if v is None:
            v, how = _has(a, ABSTAIN_MARKERS), "по словам-маркерам"
        checks.append(("честно сказал «не знаю»", how, v))
        ok &= v
    for c in expect.get("claims", []):
        v = _ask_judge(judge, "judge_claim", JUDGE_CLAIM.format(q=q, a=a, claim=c["claim"]), jlog)
        how = "судья"
        if v is None:
            v, how = _has(a, c.get("keys", [])), "по ключевым словам"
        checks.append(("утверждение", f"{c['claim']} ({how})", v))
        ok &= v
    n_words = len(re.findall(r"\w+", a))
    if expect.get("min_words") is not None:
        hit = n_words >= expect["min_words"]
        checks.append(("длина ответа", f"{n_words} слов (нужно ≥ {expect['min_words']})", hit))
        ok &= hit
    if expect.get("max_words") is not None:
        hit = bool(a) and n_words <= expect["max_words"]
        checks.append(("длина ответа", f"{n_words} слов (нужно ≤ {expect['max_words']})", hit))
        ok &= hit
    recall = None
    if expect.get("recall"):
        groups = expect["recall"]
        got = sum(_has(a, g) for g in groups)
        recall = got / len(groups)
        need = expect.get("min_recall", 0.6)
        checks.append(("полнота", f"{got} из {len(groups)} фактов (нужно ≥ {int(need * 100)}%)", recall >= need))
        ok &= recall >= need
    report = None
    if expect.get("report"):
        report = [name for name, keys in expect["report"].items() if _has(a, keys)]
        checks.append(("какой источник выбрал ассистент", ", ".join(report) or "ни один", True))
    return bool(ok), checks, recall, report, jlog


# ------------------------------------------------------------------ проверка профиля (что записано в память)

SAVE_MARKERS = ["запомнил", "запомню", "сохранил", "сохраню", "записал", "запишу", "занес", "внес в память",
                "добавил в память", "учел", "учту"]
JUDGE_SAVED = _JUDGE_HEAD + ("Сообщает ли ассистент, что сохранил, запомнил или записал эти сведения "
                             "({claim})? Ответь одним словом: YES или NO.")
_TABLE_NOTE = " [раздел не проверяется: в таблице memory разделов нет]"


def _groups_item(item):
    """Элемент profile_has / profile_lacks: список групп ключей или {"keys", "section", "count", "label"}."""
    if isinstance(item, dict):
        return item["keys"], item.get("section"), item.get("count"), item.get("label")
    return item, None, None, None


def _line_has(text, groups):
    t = _norm(text)
    return all(any(_match(t, x) for x in g) for g in groups)


def _in_section(line, section):
    return not section or line["section"] is None or line["section"] == section or \
        line["section"].startswith(section.rstrip("/") + "/")


def _label(groups, section, label, lines):
    out = label or " + ".join(" / ".join(g) for g in groups)
    if section:
        out += f" в разделе {section}"
        if lines and all(x["section"] is None for x in lines):
            out += _TABLE_NOTE
    return out


def score_profile(lines, expect):
    """Проверки профиля напрямую, а не по ответу. lines — MemorySystem.profile_lines().

    profile_has — есть строка, где встречается каждая группа ключей (count — ровно столько строк);
    profile_lacks — нет ни одной такой строки (смотрится только текущий текст, не «было»);
    prev_value — новое значение есть, а старое сохранено как «было». В таблице memory истории нет:
    там засчитывается, если старое значение вообще осталось в какой-то строке.
    """
    checks, ok = [], True
    for item in expect.get("profile_has", []):
        groups, section, count, label = _groups_item(item)
        n = sum(_line_has(x["text"], groups) for x in lines if _in_section(x, section))
        hit = n == count if count is not None else n > 0
        what = _label(groups, section, label, lines) + (f" — строк {n}, нужно ровно {count}" if count is not None else "")
        checks.append(("в профиле есть", what, hit))
        ok &= hit
    for item in expect.get("profile_lacks", []):
        groups, section, _, label = _groups_item(item)
        bad = [x["text"] for x in lines if _in_section(x, section) and _line_has(x["text"], groups)]
        checks.append(("в профиле нет", _label(groups, section, label, lines)
                       + (f" — нашлось: «{bad[0][:80]}»" if bad else ""), not bad))
        ok &= not bad
    for item in expect.get("prev_value", []):
        section = item.get("section")
        scoped = [x for x in lines if _in_section(x, section)]
        if any(x["was"] is not None for x in lines):
            hit = any(_line_has(x["text"], item["now"]) and x["was"] and _line_has(x["was"], item["was"]) for x in scoped)
            how = "строка с новым значением хранит старое как «было»"
        else:
            hit = any(_line_has(x["text"], item["now"]) for x in scoped) and \
                any(_line_has(x["text"], item["was"]) for x in scoped)
            how = "таблица memory: новое значение есть и старое осталось в какой-то строке"
        now_l = " + ".join(" / ".join(g) for g in item["now"])
        was_l = " + ".join(" / ".join(g) for g in item["was"])
        checks.append(("история значения", f"{now_l}, раньше {was_l} ({how})", hit))
        ok &= hit
    return bool(ok), checks


def score_claim_saved(answer, lines, expect, judge=None, question=""):
    """CLAIM: если ассистент говорит «запомнил», в памяти должна быть строка с ключами keys."""
    spec = expect.get("claim_saved")
    if not spec:
        return True, [], []
    jlog = []
    groups = spec["keys"]
    said = _has(answer, SAVE_MARKERS)
    if said and judge is not None:
        v = _ask_judge(judge, "judge_claim", JUDGE_SAVED.format(q=question, a=(answer or "").strip(),
                                                              claim=spec.get("claim", "то, что просил пользователь")), jlog)
        said = said if v is None else v
    saved = any(_line_has(x["text"], groups) for x in lines)
    label = " + ".join(" / ".join(g) for g in groups)
    if not said:
        return True, [("«запомнил» без записи", "ассистент не говорит, что запомнил", True)], jlog
    return saved, [("«запомнил» без записи",
                    f"ассистент говорит, что запомнил; в памяти {'есть' if saved else 'НЕТ'} строки с {label}", saved)], jlog


# ------------------------------------------------------------------ прогон сценария

def seed_memory(s, sc):
    """Факты, положенные в память напрямую, без вызовов модели.

    seed — конкретные факты (user — чей факт, по умолчанию наш пользователь; для проверки изоляции кладём
    факт другого пользователя), seed_filler — пачка однотипных (count штук по шаблону с %d).
    age_hours задаёт возраст факта: при чтении без фильтра факты отдаются свежими вперёд.
    """
    for f in sc.get("seed", []):
        s.seed_fact(f.get("user", USER), f["fact"], dialog=f.get("dialog"), type_=f.get("type", "global"),
                    topic=f.get("topic"), status=f.get("status", "PROCESSED"),
                    age_sec=int(f.get("age_hours", 0) * 3600))
    filler = sc.get("seed_filler")
    if filler:
        for i in range(1, filler["count"] + 1):
            s.seed_fact(USER, filler["template"] % i, type_="global", topic=filler.get("topic", "user_traits"),
                        status="PROCESSED", age_sec=int(filler.get("age_hours", 0) * 3600) - i)


def _turn_parts(t):
    """Реплика сценария: строка (говорит пользователь, отвечает модель) или
    {"user": ..., "assistant": ...} — ответ ассистента задан сценарием, модель не вызывается."""
    if isinstance(t, dict):
        return t["user"], t.get("assistant")
    return t, None


def _timeline(sc):
    """Все реплики сценария на одной шкале времени.

    sessions — диалоги подряд (как раньше); timeline — реплики разных диалогов вперемешку, у каждой свой dialog.
    Время: at («2026-10-12 10:25» или «10:25» в тот же день) или pause_min — минут после предыдущей реплики.
    """
    if "timeline" in sc:
        for t in sc["timeline"]:
            if "checkpoint" in t or "probe" in t:  # VOLUME: контрольная точка или вопрос по ходу истории
                yield {"kind": "checkpoint" if "checkpoint" in t else "probe", "label": t.get("checkpoint"),
                       "probe": t.get("probe"), "at": t.get("at"), "pause_min": t.get("pause_min")}
                continue
            phrase, scripted = _turn_parts(t)
            yield {"dialog": t["dialog"], "text": phrase, "scripted": scripted, "process": t.get("process", True),
                   "at": t.get("at"), "pause_min": t.get("pause_min"), "org": t.get("org"), "agent": t.get("agent"),
                   "memory_switch": t.get("memory_switch"), "fail": t.get("fail")}
        return
    for sess in sc.get("sessions", []):
        for i, raw in enumerate(sess["turns"]):
            phrase, scripted = _turn_parts(raw)
            extra = raw if isinstance(raw, dict) else {}
            yield {"dialog": sess["dialog"], "text": phrase, "scripted": scripted, "process": sess.get("process", True),
                   "at": extra.get("at", sess.get("at") if i == 0 else None), "pause_min": extra.get("pause_min"),
                   "org": sess.get("org"), "agent": sess.get("agent"),
                   "memory_switch": extra.get("memory_switch", sess.get("memory_switch") if i == 0 else None),
                   "fail": extra.get("fail"),
                   "pause_hours": sess.get("pause_hours") if i == len(sess["turns"]) - 1 else None,
                   "session_end": i == len(sess["turns"]) - 1}


def _move_clock(s, ev, transcript):
    """Сдвиг часов перед репликой; для сценариев со шкалой времени — пометка в стенограмме."""
    if ev.get("at"):
        s.clock.jump(parse_time(ev["at"], s.clock))
    if ev.get("pause_min"):
        s.clock.advance(ev["pause_min"] * 60)
        transcript.append(("note", f"пауза {ev['pause_min']} мин"))


def _set_org(s, sc, dialog, org):
    if not s.orgs:
        return
    if org:
        s.dialog_org[dialog] = str(org)
    elif dialog not in s.dialog_org:
        s.dialog_org[dialog] = str(next((o["inn"] for o in sc["orgs"] if o.get("current")), sc["orgs"][0]["inn"]))


def _set_env(s, ev, dialog, transcript):
    """Окружение реплики: агент диалога, переключатель памяти в настройках, сбои памяти на эту реплику."""
    if ev.get("agent"):
        s.dialog_agent[dialog] = ev["agent"]
    if ev.get("memory_switch"):
        s.memory_on[USER] = ev["memory_switch"] == "on"
        transcript.append(("note", f"переключатель памяти в настройках: {'включена' if s.memory_on[USER] else 'выключена'}"))
    fail = set(ev.get("fail") or ())
    s.fail_memory_read, s.fail_memory_write = "read" in fail, "write" in fail
    if fail:
        transcript.append(("note", "сбой памяти на этой реплике: " + ", ".join(
            {"read": "чтение", "write": "запись"}[x] for x in sorted(fail))))


def _clear_fail(s):
    s.fail_memory_read = s.fail_memory_write = False


def _who(s, dialog):
    """Подпись реплики в стенограмме: диалог, а для сценариев со временем и организациями — время и ИНН."""
    tag = dialog
    if s.dialog_agent.get(dialog):
        tag += " · агент " + s.dialog_agent[dialog]
    if s.timed:
        tag += " · " + fmt_time(s.clock.t)
    if s.dialog_org.get(dialog):
        tag += " · " + s.orgs[s.dialog_org[dialog]]["name"]
    return f"[{tag}]"


def _memory_notes(turn):
    """Что агент видел о памяти и какие команды памяти вызвал — строки стенограммы."""
    out = []
    if turn.get("prompt_memory"):
        out.append(("memory", "память в системном промпте: " + ", ".join(f"{k} {v}" for k, v in turn["prompt_memory"].items())))
    for c in turn.get("commands", []):
        out.append(("memory", f"функция {c['name']}({c.get('text')!r}) → {json.dumps(c['result'], ensure_ascii=False)}"))
    return out


def run_scenario(sc, llm, fixes, agent_prompt=None, judge=None):
    meter = CostMeter(llm)
    if fixes.profile:
        from .profile import ProfileSystem
        s = ProfileSystem(meter, fixes, agent_prompt=agent_prompt)
    else:
        s = MemorySystem(meter, fixes, agent_prompt=agent_prompt)
    if sc.get("start"):
        s.timed = True
        s.clock.jump(parse_time(sc["start"], s.clock))
    if sc.get("orgs"):
        s.set_orgs(USER, sc["orgs"])
    if sc.get("bps"):
        s.save_bps(USER, sc["bps"])
    s.agents = dict(sc.get("agents", {}))
    seed_memory(s, sc)
    transcript, crashes = [], 0
    vol = {"questions": [], "retention": []} if sc.get("questions") else None
    order = {label: i for i, label in enumerate(t["checkpoint"] for t in sc.get("timeline", []) if "checkpoint" in t)}
    for ev in _timeline(sc):
        if ev.get("kind") == "checkpoint":
            _move_clock(s, ev, transcript)
            s.tick(USER)  # пауза закрыла сессии: целевая схема успевает разобрать их до проверки
            r = _retention(s.profile_lines(USER), sc.get("ledger", []), order, ev["label"])
            vol["retention"].append(r)
            transcript.append(("note", f"контрольная точка «{r['label']}»: в памяти {r['kept']} из {r['expected']} фактов"
                               + (f", потеряны: {', '.join(r['lost'])}" if r["lost"] else "")
                               + (f"; устаревших осталось: {', '.join(r['stale'])}" if r["stale"] else "")))
            continue
        if ev.get("kind") == "probe":
            q = dict(ev["probe"], at=ev.get("at"), pause_min=ev.get("pause_min"))
            q.setdefault("dialog", f"probe{len(vol['questions']) + 1}")
            vol["questions"].append(_scored_question(s, sc, q, transcript, judge, label="по ходу"))
            continue
        _move_clock(s, ev, transcript)
        _set_org(s, sc, ev["dialog"], ev.get("org"))
        s.tick(USER)
        _set_env(s, ev, ev["dialog"], transcript)
        who = _who(s, ev["dialog"])
        turn = s.user_turn(USER, ev["dialog"], ev["text"], process=ev["process"], scripted_reply=ev["scripted"])
        _clear_fail(s)
        transcript += [("user", f"{who} {ev['text']}")] + [x for x in _memory_notes(turn) if not x[1].startswith("память в")] + \
                      [("assistant", (turn["text"] or "⟨пустой ответ⟩") + (" ⟨реплика задана сценарием⟩" if ev["scripted"] else ""))]
        if turn.get("handler", {}).get("status") == "crashed":
            crashes += 1
        if ev.get("session_end") and not s.timed and ev["process"]:
            s.end_session(USER, ev["dialog"])  # без шкалы времени вопрос задаётся «после разбора» сессии
        if ev.get("pause_hours"):
            s.clock.advance(ev["pause_hours"] * 3600)
            transcript.append(("note", f"пауза {ev['pause_hours']} ч"))
    if vol is not None:
        return _volume_result(s, sc, vol, transcript, judge, meter, crashes)
    q = sc["question"]
    _move_clock(s, q, transcript)
    _set_org(s, sc, q["dialog"], q.get("org"))
    s.tick(USER)
    _set_env(s, q, q["dialog"], transcript)
    who = _who(s, q["dialog"])
    # collect: true — вопрос сам несёт факт (CLAIM: «запомни …»), после ответа он идёт в сбор, как обычная реплика
    turn = s.user_turn(USER, q["dialog"], q["text"], collect=q.get("collect", False))
    _clear_fail(s)
    transcript.append(("user", f"{who} {q['text']}"))
    transcript += _memory_notes(turn)
    for c in turn["memory_calls"]:
        transcript.append(("memory", f"функция памяти: топики {c['topics']} → {len(c['facts'])} фактов"
                           + (": " + "; ".join(f"{x['text']} ({x['topic']})" for x in c["facts"][:40]) if c["facts"] else "")
                           + (" …" if len(c["facts"]) > 40 else "")))
    transcript.append(("assistant", turn["text"] or "⟨пустой ответ⟩"))
    passed, checks, recall, report, jlog = score(turn["text"], sc["expect"], judge=judge, question=q["text"])
    lines = s.profile_lines(USER)
    p_ok, p_checks = score_profile(lines, sc["expect"])
    c_ok, c_checks, c_log = score_claim_saved(turn["text"], lines, sc["expect"], judge=judge, question=q["text"])
    passed, checks, jlog = passed and p_ok and c_ok, checks + p_checks + c_checks, jlog + c_log
    facts = s.db.user_facts(USER)
    invisible = [f for f in facts if f["status"] != "PROCESSED" or f["type"] is None or f["topic_guid"] is None]
    return {
        "cost": _cost(meter, s, lines),
        "passed": passed, "checks": checks, "recall": recall, "report": report, "answer": turn["text"],
        "empty": turn["empty"], "memory_called": bool(turn["memory_calls"]),
        "topics_per_call": [len(c["topics"]) for c in turn["memory_calls"]],
        "facts_returned": sum(len(c["facts"]) for c in turn["memory_calls"]),
        "handler_crashes": crashes, "transcript": transcript, "events": s.events + jlog,
        "memory": s.memory_table(USER),
        "journal": getattr(s, "journal", None),
        "facts_total": len(facts), "facts_hidden_by_old_reader": len(invisible),
    }


# ------------------------------------------------------------------ VOLUME: много фактов, контрольные точки, много вопросов

def _retention(lines, ledger, order, label):
    """Сохранность на контрольной точке: какие факты из ledger должны быть в памяти и какие есть.

    Факт ждём с точки said до точки gone (не включая): после gone он заменён или забыт, и его ключей в текущем
    тексте памяти быть не должно (старое значение в «было» не считается — у строки профиля это отдельное поле)."""
    i = order[label]
    expected = [f for f in ledger if order[f["said"]] <= i and (not f.get("gone") or order[f["gone"]] > i)]
    gone = [f for f in ledger if f.get("gone") and order[f["gone"]] <= i]
    has = lambda f: any(_line_has(x["text"], f["keys"]) for x in lines)  # noqa: E731
    lost = [f["id"] for f in expected if not has(f)]
    return {"label": label, "expected": len(expected), "kept": len(expected) - len(lost), "lost": lost,
            "stale": [f["id"] for f in gone if has(f)], "gone_total": len(gone), "lines": len(lines),
            "tokens": approx_tokens(sum(len(x["text"]) for x in lines))}


def _scored_question(s, sc, q, transcript, judge, label):
    """Вопрос к истории в отдельном диалоге, без следов: ни в журнале реплик (блок недавних реплик), ни в учёте
    сессий. Так 20 вопросов к одной истории не влияют друг на друга и не уходят в разбор."""
    _move_clock(s, q, transcript)
    _set_org(s, sc, q["dialog"], q.get("org"))
    s.tick(USER)
    who = _who(s, q["dialog"])
    n_log = len(s.msg_log)
    turn = s.user_turn(USER, q["dialog"], q["text"], collect=False)
    del s.msg_log[n_log:]
    for d in (s.dialogs, s.sessions, s.dialog_org, s.dialog_agent):
        d.pop(q["dialog"], None)
    transcript.append(("user", f"{who} {q['text']} ⟨вопрос {label}⟩"))
    transcript += _memory_notes(turn)
    for c in turn["memory_calls"]:
        transcript.append(("memory", f"функция памяти: топики {c['topics']} → {len(c['facts'])} фактов"))
    transcript.append(("assistant", turn["text"] or "⟨пустой ответ⟩"))
    passed, checks, _, _, jlog = score(turn["text"], q["expect"], judge=judge, question=q["text"])
    s.events.extend(jlog)
    return {"label": label, "text": q["text"], "passed": passed, "answer": turn["text"], "turn": turn,
            "failed": [f"{k} {v}" for k, v, ok in checks if not ok]}


def _volume_result(s, sc, vol, transcript, judge, meter, crashes):
    for k, q in enumerate(sc["questions"]):  # все итоговые вопросы — в один момент, у первого задано время
        q = dict(q)
        q.setdefault("dialog", f"q{k + 1}")
        vol["questions"].append(_scored_question(s, sc, q, transcript, judge, label="в конце"))
    lines = s.profile_lines(USER)
    exp = sc["expect"]
    min_pass, min_ret = exp.get("min_pass", 0.8), exp.get("min_retention", 0.9)
    qs = vol["questions"]
    q_ok = sum(x["passed"] for x in qs)
    share = q_ok / len(qs) if qs else 1.0
    final = vol["retention"][-1] if vol["retention"] else None
    ret = final["kept"] / final["expected"] if final and final["expected"] else 1.0
    checks = [("вопросы", f"верно {q_ok} из {len(qs)} ({share:.0%}, нужно ≥ {min_pass:.0%})", share >= min_pass)]
    if final:
        checks.append(("сохранность в конце", f"{final['kept']} из {final['expected']} фактов ({ret:.0%}, нужно ≥ "
                       f"{min_ret:.0%}); устаревших осталось {len(final['stale'])} из {final['gone_total']}", ret >= min_ret))
    for r in vol["retention"]:
        frac = r["kept"] / r["expected"] if r["expected"] else 1.0
        checks.append(("точка", f"«{r['label']}»: {r['kept']}/{r['expected']} ({frac:.0%}), строк памяти {r['lines']}, "
                       f"≈ {r['tokens']} ток." + (f"; потеряны {', '.join(r['lost'])}" if r["lost"] else ""), frac >= min_ret))
    for x in qs:
        checks.append(("вопрос " + x["label"], f"«{x['text']}»" + (f" — {'; '.join(x['failed'])}" if x["failed"] else ""),
                       x["passed"]))
    turns = [x["turn"] for x in qs]
    facts = s.db.user_facts(USER)
    return {
        "cost": _cost(meter, s, lines),
        "passed": share >= min_pass and ret >= min_ret, "checks": checks, "recall": None, "report": None,
        "answer": "\n\n".join(f"— {x['text']}\n{x['answer']}" for x in qs),
        "empty": any(t["empty"] for t in turns), "memory_called": any(t["memory_calls"] for t in turns),
        "topics_per_call": [len(c["topics"]) for t in turns for c in t["memory_calls"]],
        "facts_returned": sum(len(c["facts"]) for t in turns for c in t["memory_calls"]),
        "handler_crashes": crashes, "transcript": transcript, "events": s.events,
        "memory": s.memory_table(USER), "journal": getattr(s, "journal", None),
        "facts_total": len(facts), "facts_hidden_by_old_reader": 0,
        "volume": {"questions": [{k: x[k] for k in ("label", "text", "passed", "failed")} for x in qs],
                   "question_share": share, "retention": vol["retention"]},
    }


def _cost(meter, s, lines):
    """Стоимость одного прогона: вызовы модели и примерные токены промпта по видам, сообщения, сессии, профиль."""
    return {"messages": s.user_messages, "sessions": s.session_count(),
            "calls": dict(meter.calls), "prompt_tokens": {k: approx_tokens(v) for k, v in meter.chars.items()},
            "profile_lines": len(lines), "profile_tokens": approx_tokens(sum(len(x["text"]) for x in lines))}


def _error_run(e):
    return {"passed": False, "error": f"{type(e).__name__}: {e}", "checks": [], "answer": "", "empty": True,
            "memory_called": False, "topics_per_call": [], "facts_returned": 0, "handler_crashes": 0,
            "transcript": [], "events": [], "memory": [], "recall": None, "report": None, "cost": None}


def summarize_cost(runs):
    """Метрики стоимости по набору прогонов: вызовы модели на сообщение и на сессию, токены промпта.

    write — сбор, аннотатор, классификатор (путь записи); chat — запросы агента, включая повтор после функции памяти.
    """
    costs = [r["cost"] for r in runs if r.get("cost")]
    msgs = sum(c["messages"] for c in costs)
    sess = sum(c["sessions"] for c in costs)
    calls, toks = Counter(), Counter()
    for c in costs:
        calls.update(c["calls"])
        toks.update(c["prompt_tokens"])
    write = sum(calls[k] for k in WRITE_KINDS)
    total = sum(calls.values())
    return {
        "runs": len(costs), "messages": msgs, "sessions": sess, "calls": dict(calls), "prompt_tokens": dict(toks),
        "calls_per_message": total / msgs if msgs else 0, "calls_per_session": total / sess if sess else 0,
        "write_calls_per_message": write / msgs if msgs else 0, "write_calls_per_session": write / sess if sess else 0,
        "chat_calls_per_message": calls["chat"] / msgs if msgs else 0,
        "prompt_tokens_per_message": sum(toks.values()) / msgs if msgs else 0,
        "chat_prompt_tokens_per_call": toks["chat"] / calls["chat"] if calls["chat"] else 0,
        "profile_tokens_avg": sum(c["profile_tokens"] for c in costs) / len(costs) if costs else 0,
        "session_gap_min": SESSION_GAP_MIN,
    }


def _question_title(sc):
    if sc.get("questions"):
        probes = sum(1 for t in sc.get("timeline", []) if "probe" in t)
        return f"{len(sc['questions'])} вопросов к истории" + (f" + {probes} по ходу" if probes else "")
    return sc["question"]["text"]


def run_bench(llm, scenarios, repeats=1, only=None, agent_prompt=None, progress=print, variants=DEFAULT_VARIANTS,
              judge=None, workers=1):
    todo = [sc for sc in scenarios if not only or sc["category"] in only or sc["id"] in only]
    rows = [{"id": sc["id"], "category": sc["category"], "title": sc.get("title", ""), "question": _question_title(sc),
             "expect": sc["expect"], "measure": sc.get("measure", True), "pair": sc.get("pair"),
             "runs": {v: [None] * repeats for v in variants}} for sc in todo]
    jobs = [(i, rep, v) for i in range(len(todo)) for rep in range(repeats) for v in variants]
    lock, done = threading.Lock(), [0]

    def work(i, rep, v):
        sc = todo[i]
        t0 = time.time()
        try:
            r = run_scenario(sc, llm, VARIANTS[v][1](), agent_prompt, judge)
        except Exception as e:  # noqa: BLE001 — сеть/модель: фиксируем и идём дальше
            r = _error_run(e)
        r["seconds"] = round(time.time() - t0, 1)
        rows[i]["runs"][v][rep] = r
        with lock:
            done[0] += 1
            mark = "—" if not sc.get("measure", True) else ("OK" if r["passed"] else "FAIL")
            progress(f"[{done[0]}/{len(jobs)}] {sc['id']} {v} #{rep + 1}: {mark}"
                     + (f" ({r['error']})" if r.get("error") else "") + f" · {r['seconds']} c")

    if workers <= 1:
        for job in jobs:
            work(*job)
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for f in as_completed([ex.submit(work, *job) for job in jobs]):
                f.result()
    return rows


def summarize(rows, variants=DEFAULT_VARIANTS):
    measured = [row for row in rows if row.get("measure", True)]
    scored = [row for row in rows if not row["expect"].get("report")] or rows

    def agg(label):
        runs = [r for row in measured for r in row["runs"][label]]
        all_runs = [r for row in rows for r in row["runs"][label]]
        calls = [n for r in all_runs for n in r["topics_per_call"]]
        n = len(runs) or 1
        ok = sum(r["passed"] for r in runs)
        return {
            "accuracy": ok / n, "passed": ok, "n": len(runs), "ci95": stats.wilson(ok, len(runs)),
            "empty": sum(r["empty"] for r in all_runs),
            "errors": sum(bool(r.get("error")) for r in all_runs),
            "memory_call_rate": sum(r["memory_called"] for r in all_runs) / (len(all_runs) or 1),
            "avg_topics": (sum(calls) / len(calls)) if calls else 0,
            "crashes": sum(r["handler_crashes"] for r in all_runs),
            # кейсы report (без правильного ответа, src-01..03) в разбивку не идут: в SRC есть и оцениваемые
            "by_cat": {cat: (sum(r["passed"] for row in scored if row["category"] == cat for r in row["runs"][label]),
                             sum(len(row["runs"][label]) for row in scored if row["category"] == cat))
                       for cat in CATEGORIES if any(row["category"] == cat for row in scored)},
            "cost": summarize_cost(all_runs),
            "cost_by_cat": {cat: summarize_cost([r for row in rows if row["category"] == cat for r in row["runs"][label]])
                            for cat in CATEGORIES if any(row["category"] == cat for row in rows)},
            "demo": {row["id"]: [r["passed"] if not row["expect"].get("report") else (r.get("report") or ["ни один"])
                                 for r in row["runs"][label]] for row in rows if not row.get("measure", True)},
            "pairs": _pairs(rows, label),
            "volume": _volume_summary(rows, label),
        }
    out = {v: agg(v) for v in variants}
    # парное сравнение с первой версией: одни и те же сценарии и повторы
    base = variants[0]
    for v in variants[1:]:
        pairs = [(a["passed"], b["passed"]) for row in measured
                 for a, b in zip(row["runs"][base], row["runs"][v])]
        out[v]["vs_" + base] = stats.mcnemar(pairs)
    return out


def _volume_summary(rows, label):
    """VOLUME: доля верных вопросов, сохранность фактов в конце и минимум по ходу, кривые по контрольным точкам."""
    runs = [(row["id"], r) for row in rows if row["category"] == "VOLUME" for r in row["runs"][label] if r.get("volume")]
    if not runs:
        return None
    qs = [q for _, r in runs for q in r["volume"]["questions"]]
    finals = [r["volume"]["retention"][-1] for _, r in runs if r["volume"]["retention"]]
    points = [p for _, r in runs for p in r["volume"]["retention"] if p["expected"]]
    curves = {}
    for sid, r in runs:  # кривая первого повтора каждого сценария
        curves.setdefault(sid, [{"label": p["label"], "kept": p["kept"], "expected": p["expected"],
                                 "stale": len(p["stale"]), "tokens": p["tokens"]} for p in r["volume"]["retention"]])
    return {
        "runs": len(runs), "questions_ok": sum(q["passed"] for q in qs), "questions": len(qs),
        "retention_final": (sum(f["kept"] for f in finals) / sum(f["expected"] for f in finals)) if finals else None,
        "retention_min": min((p["kept"] / p["expected"] for p in points), default=None),
        "stale_final": sum(len(f["stale"]) for f in finals), "gone_final": sum(f["gone_total"] for f in finals),
        "curves": curves,
    }


def _pairs(rows, label):
    """Парные кейсы: сценарий с полем pair и его партнёр засчитываются, только если пройдены оба."""
    by_id = {row["id"]: row for row in rows}
    res = []
    for row in rows:
        mate = by_id.get(row.get("pair") or "")
        if not mate:
            continue
        both = [a["passed"] and b["passed"] for a, b in zip(row["runs"][label], mate["runs"][label])]
        res.append({"pair": f"{mate['id']} + {row['id']}", "ok": sum(both), "n": len(both)})
    return res


# ------------------------------------------------------------------ стабильность аннотатора

STABILITY_FACTS = [
    # (ключ, текст, новый?, тип, топик, допустимые действия, пояснение)
    ("moscow", "Пользователь живёт в Москве", False, "global", "places", {"DELETE"}, "противоречит новому факту про Казань"),
    ("kazan", "Пользователь переехал в Казань", True, None, None, {"KEEP"}, "новый актуальный факт"),
    ("dost", "Пользователь любит читать Достоевского", False, "global", "literature", {"UPDATE"}, "уточняется новым (Толстой)"),
    ("tolst", "Пользователь любит читать Толстого", True, None, None, {"DELETE"}, "сливается со старым"),
    ("emp_old", "У пользователя 12 сотрудников", False, "global", "profession", {"KEEP", "DELETE"}, "один из пары дублей"),
    ("emp_new", "В компании пользователя работает 12 человек", True, None, None, {"KEEP", "DELETE"}, "один из пары дублей"),
    ("usn", "Пользователь работает на УСН", False, "global", "tax_system", {"DELETE"}, "цель команды «забудь»"),
    ("forget", "Забудь, какая у пользователя система налогообложения", True, None, None, {"DELETE"}, "команда удаления"),
    ("cargo", "Пользователь занимается грузоперевозками", False, "global", "profession", {"KEEP"}, "не затронут"),
    ("pizza", "Сегодня пользователь хочет заказать пиццу", True, None, None, {"KEEP", "DELETE"}, "сиюминутный, новый"),
]


class Recorder:
    """Пропускает вызовы в живую модель и запоминает ответы, чтобы потом проиграть их на другой логике."""

    def __init__(self, llm):
        self.llm, self.records = llm, []

    def json_task(self, kind, prompt):
        out = self.llm.json_task(kind, prompt)
        self.records.append((kind, out))
        return out

    def chat(self, messages, functions=None):
        return self.llm.chat(messages, functions)


def _seed(s, ids):
    for key, txt, new, typ, topic, _, _ in STABILITY_FACTS:
        s.seed_fact(USER, txt, dialog="d1", type_=typ, topic=topic, status="PENDING" if new else "PROCESSED", guid=ids[key])
    return ids


class Replayer:
    """Отдаёт записанные ответы модели; если для логики «после» нужен вызов, которого не было «до»
    (например, «до» упало раньше классификатора), — честно спрашивает живую модель."""

    def __init__(self, records, llm):
        self.queues, self.llm = _queues(records), llm

    def json_task(self, kind, prompt):
        q = self.queues.get(kind)
        return q.pop(0) if q else self.llm.json_task(kind, prompt)

    def chat(self, messages, functions=None):
        return self.llm.chat(messages, functions)


def run_stability(llm, n=10, progress=print):
    per_fact = {k: Counter() for k, *_ in STABILITY_FACTS}
    outcome = {"before": Counter(), "after": Counter()}
    for i in range(n):
        rec = Recorder(llm)
        results = {}
        fixed_ids = {k: str(uuid.uuid4()) for k, *_ in STABILITY_FACTS}
        for label, fx in (("before", Fixes.before()), ("after", Fixes.after())):
            if label == "before":
                s = MemorySystem(rec, fx)
            else:  # те же ответы модели, другая логика
                s = MemorySystem(Replayer(rec.records, llm), fx)
            ids = _seed(s, fixed_ids)
            try:
                res = HandlerRun(s, USER).run()
            except Exception as e:  # noqa: BLE001
                res = {"status": "crashed", "reason": str(e)}
            facts = {f["guid"]: f for f in s.db.user_facts(USER)}
            results[label] = (res, ids, facts)
            o = outcome[label]
            o["runs"] += 1
            o["crashed"] += res["status"] == "crashed"
            o["forget_ok"] += res["status"] == "done" and ids["usn"] not in facts
            o["moscow_gone"] += res["status"] == "done" and ids["moscow"] not in facts
            o["hidden"] += sum(1 for f in facts.values() if f["type"] is None or f["status"] != "PROCESSED")
        # распределение решений модели по каждому факту (из записанного ответа)
        ann = next((out for kind, out in rec.records if kind == "annotate"), None) or {}
        _, ids, _ = results["before"]
        rev = {v: k for k, v in ids.items()}
        seen = set()
        for d in ann.get("decisions") or []:
            k = rev.get(str((d or {}).get("index")))
            if k and k not in seen:
                seen.add(k)
                per_fact[k][str(d.get("action"))] += 1
        for k in per_fact:
            if k not in seen:
                per_fact[k]["нет решения"] += 1
        progress(f"[stability {i + 1}/{n}] до: {results['before'][0]['status']}, после: {results['after'][0]['status']}")
    table = []
    for key, txt, new, _, _, gold, why in STABILITY_FACTS:
        c = per_fact[key]
        total = sum(c.values()) or 1
        modal = c.most_common(1)[0][1] if c else 0
        table.append({"fact": txt, "new": new, "gold": sorted(gold), "why": why, "dist": dict(c),
                      "agreement": modal / total, "correct": sum(v for a, v in c.items() if a in gold) / total})
    return {"n": n, "facts": table, "outcome": {k: dict(v) for k, v in outcome.items()}}


def _queues(records):
    q = {}
    for kind, out in records:
        q.setdefault(kind, []).append(out)
    return q


def load_scenarios(path):
    """Читает сценарии; seed_pack раскрывается из общего раздела packs (большие наборы фактов для BUDGET)."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    packs = data.get("packs", {})
    out = []
    for sc in data["scenarios"]:
        if sc.get("seed_pack"):
            sc = dict(sc, seed=list(packs[sc["seed_pack"]]) + list(sc.get("seed", [])))
        _validate(sc)
        out.append(sc)
    return out


_TURN_KEYS = {"user", "assistant", "dialog", "at", "pause_min", "org", "process", "agent", "memory_switch", "fail"}
_EXPECT_KEYS = {"all", "none", "none_strict", "abstain", "claims", "recall", "min_recall", "report",
                "profile_has", "profile_lacks", "prev_value", "claim_saved", "min_words", "max_words"}


def _validate_env(x, agents, need):
    """agent, memory_switch, fail у реплики, сессии или вопроса."""
    need(not x.get("agent") or x["agent"] in agents, f"агента {x.get('agent')} нет в agents")
    need(x.get("memory_switch") in (None, "on", "off"), f"memory_switch только on / off: {x.get('memory_switch')}")
    need(set(x.get("fail") or ()) <= {"read", "write"}, f"fail только read / write: {x.get('fail')}")
_VOLUME_EXPECT_KEYS = {"min_pass", "min_retention"}
_MARK_KEYS = {"checkpoint", "probe", "at", "pause_min"}


def _validate_volume(sc, need, inns):
    """VOLUME: шкала времени с контрольными точками, ledger всех фактов и список вопросов вместо одного."""
    need("timeline" in sc and sc.get("start"), "сценарию VOLUME нужны timeline и start")
    need(set(sc["expect"]) <= _VOLUME_EXPECT_KEYS, f"в expect сценария VOLUME только {_VOLUME_EXPECT_KEYS}")
    labels = []
    for t in sc["timeline"]:
        if "checkpoint" in t or "probe" in t:
            need(set(t) <= _MARK_KEYS, f"странная отметка {t}")
            if "checkpoint" in t:
                need(t["checkpoint"] not in labels, f"контрольная точка «{t['checkpoint']}» повторяется")
                labels.append(t["checkpoint"])
            else:
                need(set(t["probe"]["expect"]) <= _EXPECT_KEYS, f"неизвестные поля expect в вопросе {t['probe']}")
            continue
        need(set(t) <= _TURN_KEYS and "user" in t and t.get("dialog"), f"странная реплика {t}")
        need(not t.get("org") or str(t["org"]) in inns, f"организации {t.get('org')} нет в orgs")
    need("checkpoint" in sc["timeline"][-1], "timeline должен заканчиваться контрольной точкой")
    ids = set()
    for f in sc.get("ledger", []):
        need(f["id"] not in ids, f"факт {f['id']} в ledger повторяется")
        ids.add(f["id"])
        need(f["said"] in labels and (not f.get("gone") or f["gone"] in labels), f"у факта {f['id']} неизвестная точка")
        need(not f.get("gone") or labels.index(f["gone"]) > labels.index(f["said"]), f"факт {f['id']} уходит раньше, чем сказан")
    for q in sc["questions"]:
        need(set(q["expect"]) <= _EXPECT_KEYS, f"неизвестные поля expect в вопросе «{q['text']}»")
        need(not q.get("org") or str(q["org"]) in inns, f"организации {q.get('org')} нет в orgs")


def _validate(sc):
    """Ловит опечатки в сценариях до прогона, а не на середине живого бенча."""
    sid = sc.get("id", "?")

    def need(cond, msg):
        if not cond:
            raise ValueError(f"сценарий {sid}: {msg}")
    need(sc.get("category") in CATEGORIES, f"неизвестная категория {sc.get('category')}")
    need(not ("timeline" in sc and sc.get("sessions")), "нужно что-то одно: sessions или timeline")
    inns = {str(o["inn"]) for o in sc.get("orgs", [])}
    if sc.get("questions"):
        _validate_volume(sc, need, inns)
        return
    need(set(sc["expect"]) <= _EXPECT_KEYS, f"неизвестные поля expect: {set(sc['expect']) - _EXPECT_KEYS}")
    turns = [t for t in sc.get("timeline", [])] + [t for s_ in sc.get("sessions", []) for t in s_["turns"]]
    agents = sc.get("agents", {})
    for t in turns:
        if isinstance(t, dict):
            need(set(t) <= _TURN_KEYS and "user" in t, f"странная реплика {t}")
            need(not t.get("org") or str(t["org"]) in inns, f"организации {t.get('org')} нет в orgs")
            _validate_env(t, agents, need)
    for t in sc.get("timeline", []):
        need(isinstance(t, dict) and t.get("dialog"), "у реплики timeline должен быть dialog")
    for s_ in sc.get("sessions", []):
        need(not s_.get("org") or str(s_["org"]) in inns, f"организации {s_.get('org')} нет в orgs")
        _validate_env(s_, agents, need)
    q = sc["question"]
    need(not q.get("org") or str(q["org"]) in inns, f"организации {q.get('org')} нет в orgs")
    _validate_env(q, agents, need)
    uses_time = sc.get("start") or any(isinstance(t, dict) and t.get("at") for t in turns) or q.get("at")
    need(not uses_time or sc.get("start"), "при at в репликах нужно поле start (начало шкалы времени)")
