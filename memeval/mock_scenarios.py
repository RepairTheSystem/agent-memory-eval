"""Детерминированные сценарии: по одному на каждую правку.

Модель здесь заскриптована (ScriptedLLM): мы заранее задаём, что она ответит, в том числе
«неудобные» ответы, которые встречаются в проде (не вернула решение, пустой топик, повторный вызов функции).
Агент-мок честно пересказывает то, что ему вернула функция памяти, поэтому по его ответу видно,
что именно получил пользователь. Каждый сценарий запускается дважды: на логике «до» и «после».
"""
import json
import re

from .llm import ScriptedLLM, fcall, text
from .pipeline import Clock, HandlerRun, MemorySystem

USER = "user-1"


# ------------------------------------------------------------------ helpers для мок-модели

def agent_reads_memory(payload):
    """Мок-агент отвечает по фактам из последнего ответа функции памяти (или говорит, что не знает)."""
    fn = [m for m in payload["messages"] if m["role"] == "function"]
    if not fn:
        return text("Не нашёл этого в памяти, уточните, пожалуйста.")
    facts = json.loads(fn[-1]["content"]).get("facts") or []
    if not facts:
        return text("В памяти об этом ничего нет.")
    return text("Из памяти: " + "; ".join(f["text"] for f in facts))


def facts_from_prompt(prompt):
    """Достаёт JSON фактов из промпта аннотатора/классификатора (как модель их «видит»)."""
    if prompt.strip().endswith("[]"):
        return []
    m = re.search(r'(\{"facts": \[.*\]\}|\[\{"index".*\])\s*$', prompt.strip(), re.S)
    data = json.loads(m.group(1))
    return data["facts"] if isinstance(data, dict) else data


def by_text(facts, needle):
    return next(f for f in facts if needle in f["text"])


def classify_all(code_by_keyword, default=None):
    def _fn(prompt):
        out = []
        for f in facts_from_prompt(prompt):
            code = next((c for kw, c in code_by_keyword.items() if kw in f["text"]), default)
            out.append({"index": f["index"], "topic": code})
        return {"topics": out}
    return _fn


def keep_all(prompt):
    return {"decisions": [{"index": f["index"], "action": "KEEP", "reason": "none", "scope": "global"}
                          for f in facts_from_prompt(prompt)]}


def snapshot(system):
    return [{"Факт": f["fact"], "Статус": f["status"], "Тип": f["type"] or "—",
             "Топик": f["topic_code"] or "—", "Диалог": (f["dialog_guid"] or "—")}
            for f in system.db.user_facts(USER)]


def result(system, llm, passed, verdict, transcript, **extra):
    calls = {}
    for c in llm.calls:
        calls[c["kind"]] = calls.get(c["kind"], 0) + 1
    return {"passed": passed, "verdict": verdict, "transcript": transcript, "events": list(system.events),
            "memory": snapshot(system), "llm_calls": calls, **extra}


def ask(system, dialog, question, transcript):
    turn = system.user_turn(USER, dialog, question, collect=False)
    transcript.append(("user", f"[{dialog}] {question}"))
    for c in turn["memory_calls"]:
        transcript.append(("memory", f"функция памяти: топики {c['topics']} → {len(c['facts'])} фактов"
                           + (": " + "; ".join(f"{x['text']} ({x['topic']})" for x in c["facts"]) if c["facts"] else "")))
    transcript.append(("assistant", turn["text"] or "⟨пустой ответ⟩"))
    return turn


def say(system, dialog, phrase, transcript, process=True):
    turn = system.user_turn(USER, dialog, phrase, process=process)
    transcript.append(("user", f"[{dialog}] {phrase}"))
    transcript.append(("assistant", turn["text"]))
    return turn


# ------------------------------------------------------------------ сценарии

def sc_bps_company(fixes):
    llm = ScriptedLLM(chat=[fcall(["legal_entity", "business_license"]), agent_reads_memory])
    s = MemorySystem(llm, fixes)
    s.save_bps(USER, {"fullOrgName": "ООО «Ромашка»", "inn": "7701234567", "okopf": "Общество с ограниченной ответственностью"})
    t = []
    turn = ask(s, "d1", "Как называется моя компания и какой у неё ИНН?", t)
    ok = "Ромашка" in turn["text"] and "7701234567" in turn["text"]
    return result(s, llm, ok, "Ассистент назвал компанию и ИНН из профиля БПС" if ok else
                  "Данные из профиля БПС есть в базе, но до ассистента не дошли", t)


def sc_fresh_new_dialog(fixes):
    llm = ScriptedLLM(chat=[text("Хорошо, учту."), fcall(["tax_system"]), agent_reads_memory],
                      collect=[{"facts": ["Пользователь работает на УСН «доходы минус расходы»"]}])
    s = MemorySystem(llm, fixes)
    t = []
    say(s, "d1", "Запомни: мы работаем на УСН «доходы минус расходы».", t, process=False)
    t.append(("note", "Фоновый разбор ещё не закончился (или упал) — факт лежит в статусе PENDING"))
    turn = ask(s, "d2", "Какая у нас система налогообложения?", t)
    ok = "УСН" in turn["text"]
    return result(s, llm, ok, "Только что сказанное доступно в новом диалоге" if ok else
                  "Факт сохранён, но пока не разобран — в новом диалоге ассистент его не видит", t)


def sc_stale_other_dialog(fixes):
    llm = ScriptedLLM(chat=[fcall(["food"]), agent_reads_memory])
    s = MemorySystem(llm, fixes)
    s.seed_fact(USER, "Сегодня пользователь хочет заказать пиццу", dialog="d-old", status="PENDING", age_sec=3 * 3600)
    t = [("note", "В старом диалоге (3 часа назад) остался неразобранный сиюминутный факт про пиццу")]
    turn = ask(s, "d2", "Что я люблю из еды?", t)
    ok = "пицц" not in turn["text"].lower()
    return result(s, llm, ok, "Сиюминутный факт из старого чужого диалога не всплыл" if ok else
                  "В ответ попал сырой сиюминутный факт из другого диалога", t)


def sc_forget(fixes):
    def annotate(prompt):
        fs = facts_from_prompt(prompt)
        city, cmd = by_text(fs, "Москве"), by_text(fs, "Забудь")
        # Типичная «половинчатая» работа модели: указала цель в affects, но самой цели поставила KEEP
        return {"decisions": [
            {"index": city["index"], "action": "KEEP", "reason": "none", "scope": "global"},
            {"index": cmd["index"], "action": "DELETE", "reason": "explicit_request", "scope": "local",
             "affects": [city["index"]]},
        ]}
    llm = ScriptedLLM(chat=[text("Хорошо, забыл."), fcall(["places"]), agent_reads_memory],
                      collect=[{"facts": ["Забудь информацию о месте жительства пользователя"]}],
                      annotate=[annotate], classify=[{"topics": []}])
    s = MemorySystem(llm, fixes)
    s.seed_fact(USER, "Пользователь живёт в Москве", dialog="d0", type_="global", topic="places")
    t = []
    say(s, "d1", "Забудь, где я живу.", t)
    turn = ask(s, "d2", "Где я живу?", t)
    ok = "Москв" not in turn["text"]
    return result(s, llm, ok, "После «забудь» город больше не всплывает" if ok else
                  "Пользователь попросил забыть, но ассистент всё ещё называет город", t)


def sc_no_decision(fixes):
    def annotate(prompt):  # модель «забыла» вернуть решение по новому факту
        fs = facts_from_prompt(prompt)
        return {"decisions": [{"index": f["index"], "action": "KEEP", "reason": "none", "scope": "global"}
                              for f in fs if "детей" not in f["text"]]}
    llm = ScriptedLLM(chat=[text("Поздравляю!"), fcall(["family"]), agent_reads_memory],
                      collect=[{"facts": ["У пользователя двое детей"]}],
                      annotate=[annotate], classify=[classify_all({"детей": "family"})])
    s = MemorySystem(llm, fixes)
    s.seed_fact(USER, "Пользователь любит рыбалку", dialog="d0", type_="global", topic="hobbies")
    t = []
    say(s, "d1", "У меня двое детей.", t)
    turn = ask(s, "d2", "Сколько у меня детей?", t)
    ok = "двое" in turn["text"]
    return result(s, llm, ok, "Факт без решения модели получил тип global и виден" if ok else
                  "Модель не вернула решение → у факта пустой тип → чтение его не находит", t)


def sc_keep_no_scope(fixes):
    def annotate(prompt):  # KEEP, но scope не указан
        return {"decisions": [{"index": f["index"], "action": "KEEP", "reason": "none"} for f in facts_from_prompt(prompt)]}
    llm = ScriptedLLM(chat=[text("Принято."), fcall(["profession"]), agent_reads_memory],
                      collect=[{"facts": ["Пользователь занимается грузоперевозками"]}],
                      annotate=[annotate], classify=[classify_all({"грузоперевоз": "profession"})])
    s = MemorySystem(llm, fixes)
    t = []
    say(s, "d1", "Мы занимаемся грузоперевозками.", t)
    turn = ask(s, "d2", "Чем занимается моя компания?", t)
    ok = "грузоперевоз" in turn["text"]
    return result(s, llm, ok, "Долгосрочный факт виден в новом диалоге" if ok else
                  "Модель не указала scope → факт стал local → в новом диалоге не виден", t)


def sc_no_topic(fixes):
    llm = ScriptedLLM(chat=[text("Понял."), fcall(["contract", "business_risk"]), agent_reads_memory],
                      collect=[{"facts": ["У пользователя есть договор аренды склада до декабря"]}],
                      annotate=[keep_all], classify=[{"topics": []}])
    s = MemorySystem(llm, fixes)
    t = []
    say(s, "d1", "У нас договор аренды склада до декабря.", t)
    turn = ask(s, "d2", "Какие у меня договоры?", t)
    ok = "аренды склада" in turn["text"]
    return result(s, llm, ok, "Факт без топика всё равно доходит до ассистента" if ok else
                  "Классификатор не дал топик → факт навсегда невидим для чтения по топикам", t)


def sc_null_topic_crash(fixes):
    def classify(prompt):
        return {"topics": [{"index": f["index"], "topic": None} for f in facts_from_prompt(prompt)]}
    llm = ScriptedLLM(chat=[text("Ок."), fcall(["accounting_period"]), agent_reads_memory],
                      collect=[{"facts": ["Пользователь сдаёт отчётность ежеквартально"]}],
                      annotate=[keep_all], classify=[classify])
    s = MemorySystem(llm, fixes)
    t = []
    turn1 = say(s, "d1", "Мы сдаём отчётность раз в квартал.", t)
    t.append(("note", f"Фоновый разбор: {turn1['handler']['status']} {turn1['handler']['reason']}".strip()))
    turn = ask(s, "d2", "Как часто мы сдаём отчётность?", t)
    ok = turn1["handler"]["status"] == "done" and "ежеквартально" in turn["text"]
    return result(s, llm, ok, "Разбор пережил пустой топик от модели" if ok else
                  "Пустой топик от модели уронил весь разбор: факты застряли в PENDING", t)


def sc_null_action_crash(fixes):
    def annotate(prompt):
        return {"decisions": [{"index": f["index"], "action": None, "reason": "none", "scope": "global"}
                              for f in facts_from_prompt(prompt)]}
    llm = ScriptedLLM(chat=[text("Ок.")], collect=[{"facts": ["У пользователя 12 сотрудников"]}],
                      annotate=[annotate], classify=[classify_all({"сотрудник": "profession"})])
    s = MemorySystem(llm, fixes)
    t = []
    turn1 = say(s, "d1", "У нас 12 сотрудников.", t)
    t.append(("note", f"Фоновый разбор: {turn1['handler']['status']} {turn1['handler']['reason']}".strip()))
    ok = turn1["handler"]["status"] == "done"
    return result(s, llm, ok, "Пустое действие от модели не роняет разбор" if ok else
                  "Пустое действие от модели уронило весь разбор (NullPointerException)", t)


def sc_race(fixes):
    """Два сообщения подряд → два фоновых разбора одного пользователя одновременно."""
    def annotate(prompt):
        fs = facts_from_prompt(prompt)
        dec = []
        for f in fs:
            new_tolstoy = any(x["isNew"] and "Толстого" in x["text"] for x in fs)
            if f["isNew"] and "Толстого" in f["text"]:
                dec.append({"index": f["index"], "action": "DELETE", "reason": "refinement"})
            elif "Толстого" not in f["text"] and "Достоевского" in f["text"] and new_tolstoy:
                dec.append({"index": f["index"], "action": "UPDATE", "reason": "refinement", "scope": "global",
                            "newText": "Пользователь любит читать Достоевского и Толстого"})
            else:
                dec.append({"index": f["index"], "action": "KEEP", "reason": "none", "scope": "global"})
        return {"decisions": dec}
    cls = classify_all({"Толстого": "literature", "сотрудник": "profession"})
    llm = ScriptedLLM(chat=[text("Отлично!"), text("Понял.")],
                      collect=[{"facts": ["Пользователь любит читать Толстого"]}, {"facts": ["У пользователя 12 сотрудников"]}],
                      annotate=[annotate, annotate, annotate], classify=[cls, cls, cls])
    s = MemorySystem(llm, fixes)
    s.seed_fact(USER, "Пользователь любит читать Достоевского", dialog="d0", type_="global", topic="literature")
    t = []
    # сообщение 1: сбор + запуск разбора (ещё не закоммичен)
    s.user_turn(USER, "d1", "А ещё я люблю Толстого.", process=False)
    t.append(("user", "[d1] А ещё я люблю Толстого."))
    run1 = HandlerRun(s, USER)
    run1.begin()
    # сообщение 2 приходит, пока разбор 1 работает
    s.user_turn(USER, "d1", "Кстати, у нас 12 сотрудников.", process=False)
    t.append(("user", "[d1] Кстати, у нас 12 сотрудников."))
    run2 = HandlerRun(s, USER)
    run2.begin()
    run1.compute()
    run2.compute()
    r1, r2 = run1.commit(), run2.commit()
    t.append(("note", f"Разбор №1: {r1['status']}; разбор №2: {r2['status']} {r2['reason']}".strip()))
    if r2["status"] != "done":  # следующий триггер (новое сообщение / проверка раз в 20 минут)
        r3 = s.handler(USER)
        t.append(("note", f"Следующий запуск разбора: {r3['status']} {r3['reason']}".strip()))
    annot_calls = sum(c["kind"] == "annotate" for c in llm.calls)
    facts = {f["fact"]: f["status"] for f in s.db.user_facts(USER)}
    ok_state = facts.get("Пользователь любит читать Достоевского и Толстого") == "PROCESSED" and \
        facts.get("У пользователя 12 сотрудников") == "PROCESSED"
    crashed = r2["status"] == "crashed"
    ok = ok_state and not crashed
    verdict = ("Один разбор за раз: второй вежливо пропущен, его факт разобран следующим запуском" if ok else
               "Два разбора параллельно взяли одни и те же факты: второй упал с StaleStateException и откатился"
               if crashed else "Итоговое состояние памяти неверное")
    return result(s, llm, ok, verdict, t, metric=f"вызовов аннотатора: {annot_calls}")


def sc_double_call(fixes):
    llm = ScriptedLLM(chat=[fcall(["tax_system"]), fcall(["contract"]), agent_reads_memory])
    s = MemorySystem(llm, fixes)
    s.seed_fact(USER, "Пользователь работает на УСН", dialog="d0", type_="global", topic="tax_system")
    s.seed_fact(USER, "У пользователя договор поставки с ООО «Вектор»", dialog="d0", type_="global", topic="contract")
    t = []
    turn = ask(s, "d1", "Расскажи про мои налоги и договоры.", t)
    ok = not turn["empty"]
    return result(s, llm, ok, "Модель дважды обратилась к памяти и ответила текстом" if ok else
                  "Модель вызвала функцию второй раз — вызов не обработан, пользователю ушёл пустой ответ", t)


def sc_memory_error(fixes):
    llm = ScriptedLLM(chat=[fcall(["legal_entity"]), text("Сейчас не могу заглянуть в память, но давайте разберёмся: что именно нужно?")])
    s = MemorySystem(llm, fixes)
    s.fail_memory_read = True
    t = [("note", "Эмулируем ошибку при чтении памяти (БД недоступна)")]
    turn = ask(s, "d1", "Как называется моя компания?", t)
    ok = not turn["empty"]
    return result(s, llm, ok, "Ошибка памяти → обычный ответ без памяти" if ok else
                  "Ошибка памяти → пользователю ушёл пустой ответ", t)


SCENARIOS = [
    # id, заголовок, жалоба пользователя/суть, правки, функция
    ("bps", "Данные из профиля БПС", "«Как называется моя компания?» — ассистент не знает", ["read_null_topic"], sc_bps_company),
    ("fresh", "Только что сказанное — в новом диалоге", "«Запомнил», но в новом диалоге не помнит", ["read_unverified"], sc_fresh_new_dialog),
    ("stale", "Защита: старые сырые факты чужих диалогов", "Не тащить сиюминутное из старых диалогов", ["read_unverified"], sc_stale_other_dialog),
    ("forget", "«Забудь» действительно удаляет", "Удаление работает «рандомно»", ["affects_delete"], sc_forget),
    ("no_decision", "Модель пропустила факт при разборе", "«Запомнил», но не помнит", ["default_type"], sc_no_decision),
    ("no_scope", "Модель не указала global/local", "В новом диалоге не помнит", ["default_type"], sc_keep_no_scope),
    ("no_topic", "Классификатор не дал топик", "«Запомнил», но не помнит", ["read_null_topic"], sc_no_topic),
    ("null_topic", "Пустой топик роняет разбор", "Факты зависают необработанными", ["crash_guards", "read_null_topic"], sc_null_topic_crash),
    ("null_action", "Пустое действие роняет разбор", "Факты зависают необработанными", ["crash_guards"], sc_null_action_crash),
    ("race", "Два сообщения подряд", "Параллельные разборы мешают друг другу", ["lock"], sc_race),
    ("double_call", "Модель дважды зовёт память", "Пустой ответ ассистента", ["multi_call"], sc_double_call),
    ("mem_error", "Ошибка при чтении памяти", "Пустой ответ ассистента", ["fallback"], sc_memory_error),
]


def run_all():
    from .pipeline import Fixes
    out = []
    for sid, title, problem, fix_keys, fn in SCENARIOS:
        row = {"id": sid, "title": title, "problem": problem, "fixes": fix_keys}
        only = Fixes(**{k: True for k in fix_keys})  # «до» + только правки этого сценария
        for label, fx in (("before", Fixes.before()), ("only", only), ("after", Fixes.after())):
            try:
                row[label] = fn(fx)
            except Exception as e:  # noqa: BLE001
                row[label] = {"passed": False, "verdict": f"Сценарий упал: {type(e).__name__}: {e}", "transcript": [],
                              "events": [], "memory": [], "llm_calls": {}}
        out.append(row)
    return out
