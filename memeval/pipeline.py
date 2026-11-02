"""In-memory reference baseline and ablation flags for agent-memory experiments.

The baseline models a collect/annotate/classify/read workflow. This package
contains a Python simulator, not the original service implementation.
"""
import json
import uuid
from collections import Counter
from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta, timezone

from . import prompts, prompts_profile as pp
from .db import MemoryDB

PENDING, PROCESSING, PROCESSED = "PENDING", "PROCESSING", "PROCESSED"
UNPROCESSED_LIMIT = 30
READ_ALL_LIMIT = 100           # сколько фактов максимум отдаём при чтении без фильтра по темам
UNPROCESSED_WINDOW_SEC = 3600
MAX_FUNCTION_CALLS = 2
SESSION_GAP_MIN = 30           # пауза в диалоге, после которой сессия закончена (целевая схема: разбор через 20–30 мин)
TZ = timezone(timedelta(hours=3))  # время сценариев — московское (в агенте CURRENT_TIME_ZONE по умолчанию 3)
WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
CHARS_PER_TOKEN = 3.5          # грубая оценка токенов промпта по числу символов (русский текст + JSON)
WRITE_KINDS = ("collect", "annotate", "classify", "session", "sleep", "command")  # вызовы модели на пути записи
RECENT_WINDOW_H = 3            # недавние реплики других диалогов: за сколько часов
RECENT_MAX = 10                # … не больше стольких реплик
RECENT_BUDGET_TOKENS = 500     # … и не больше стольких токенов


@dataclass
class Fixes:
    lock: bool = False             # оркестратор: замок на разбор фактов пользователя
    crash_guards: bool = False     # оркестратор: пустые/повторные ответы моделей не роняют разбор
    affects_delete: bool = False   # оркестратор: «забудь» удаляет факты из affects
    default_type: bool = False     # оркестратор: тип по умолчанию global
    read_null_topic: bool = False  # агент: факты без топика (system / uncategorized) отдаются всегда
    read_unverified: bool = False  # агент: свежие необработанные факты (+ окно 1 час для чужих диалогов)
    or_description: bool = False   # агент: описание параметра topics «ИЛИ» вместо «И»
    multi_call: bool = False       # агент: до 2 вызовов функции памяти, потом ответ текстом
    fallback: bool = False         # агент: при ошибке памяти — обычный ответ, а не пустой
    # экспериментальная правка (не входит в after(), включается отдельным вариантом прогона):
    no_topic_filter: bool = False  # агент: при чтении не фильтровать по темам — отдаём все факты пользователя
    # шаги плана перехода к целевой схеме (docs/ARCHITECTURE.md, раздел 9):
    prompt_memory: bool = False    # шаг 2: факты в системном промпте вместо функции + недавние реплики других диалогов
    collect_context: bool = False  # шаг 3: сбор видит известные факты, правило про дельты, имена можно
    profile: bool = False          # шаг 4: профиль-документ, разбор сессии после паузы, проверки кодом, журнал
    deep_sleep: bool = False       # шаг 5: сжатие профиля при переполнении
    commands: bool = False         # шаг 5: функции remember / forget у агента
    search_memory: bool = False    # шаг 6: функция search_memory по архиву и строкам, не влезшим в бюджет

    EXPERIMENTAL = ("no_topic_filter", "prompt_memory", "collect_context", "profile", "deep_sleep", "commands",
                    "search_memory")

    @classmethod
    def before(cls):
        return cls()

    @classmethod
    def after(cls):
        return cls(**{f.name: True for f in fields(cls) if f.name not in cls.EXPERIMENTAL})

    @classmethod
    def nofilter(cls):
        return cls(**{f.name: True for f in fields(cls) if f.name not in cls.EXPERIMENTAL}, no_topic_filter=True)

    @classmethod
    def lite(cls):
        """Шаг 2: профиль-лайт — все разобранные факты в промпте, функции памяти нет."""
        return replace(cls.nofilter(), prompt_memory=True)

    @classmethod
    def ctx(cls):
        """Шаг 3: шаг 2 + сбор с контекстом."""
        return replace(cls.lite(), collect_context=True)

    @classmethod
    def profile_v(cls):
        """Шаг 4: профиль и разбор сессии (старые аннотатор и классификатор не работают)."""
        return replace(cls.nofilter(), prompt_memory=True, profile=True)

    @classmethod
    def full(cls):
        """Шаг 5: шаг 4 + глубокий сон и remember / forget."""
        return replace(cls.profile_v(), deep_sleep=True, commands=True)

    @classmethod
    def search(cls):
        """Шаг 6: шаг 5 + search_memory."""
        return replace(cls.full(), search_memory=True)


FIX_TITLES = {
    "lock": "Замок от параллельного разбора",
    "crash_guards": "Защита разбора от падений",
    "affects_delete": "«Забудь» удаляет целевые факты",
    "default_type": "Тип факта по умолчанию global",
    "read_null_topic": "Факты без топика (БПС и др.) видны при чтении",
    "read_unverified": "Свежие необработанные факты видны при чтении",
    "or_description": "Описание топиков: «ИЛИ» вместо «И»",
    "multi_call": "Повторный вызов функции памяти",
    "fallback": "Ответ без памяти при ошибке",
    "no_topic_filter": "Чтение без фильтра по темам",
    "prompt_memory": "Шаг 2: факты в промпте + недавние реплики",
    "collect_context": "Шаг 3: сбор с контекстом",
    "profile": "Шаг 4: профиль и разбор сессии",
    "deep_sleep": "Шаг 5: глубокий сон",
    "commands": "Шаг 5: remember / forget",
    "search_memory": "Шаг 6: search_memory",
}


class Crash(Exception):
    """Исключение внутри разбора: в Java транзакция откатывается целиком."""


class MemoryReadError(Exception):
    pass


class Clock:
    def __init__(self, start=1_760_000_000.0):
        self.t = start

    def now(self):
        self.t += 1
        return self.t

    def advance(self, seconds):
        self.t += seconds

    def jump(self, ts):
        """Перевести часы на момент ts (реплика с полем at). Назад время не идёт."""
        if ts < self.t - 120:
            raise ValueError(f"время в сценарии идёт назад: {fmt_time(ts)} раньше {fmt_time(self.t)}")
        self.t = max(self.t, ts)

    def dt(self):
        return datetime.fromtimestamp(self.t, TZ)


def parse_time(value, clock):
    """«2026-10-12 10:00» / «2026-10-12T10:00» — абсолютное время; «10:25» — в тот же день, что и часы сейчас."""
    v = str(value).strip().replace("T", " ")
    if len(v) <= 5:
        h, m = (int(x) for x in v.split(":"))
        return clock.dt().replace(hour=h, minute=m, second=0, microsecond=0).timestamp()
    return datetime.strptime(v, "%Y-%m-%d %H:%M").replace(tzinfo=TZ).timestamp()


def fmt_time(ts):
    d = datetime.fromtimestamp(ts, TZ)
    return f"{('пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс')[d.weekday()]} {d:%d.%m.%Y %H:%M}"


def approx_tokens(chars):
    return int(round(chars / CHARS_PER_TOKEN))


class CostMeter:
    """Обёртка над моделью одного прогона: считает вызовы и размер промпта по видам вызова.

    Судья сюда не попадает — он вызывается отдельно и к стоимости памяти отношения не имеет.
    """

    def __init__(self, llm):
        self.llm = llm
        self.calls, self.chars = Counter(), Counter()

    def __getattr__(self, name):  # served_model, usage, plain … — как у обёрнутой модели
        return getattr(self.llm, name)

    def _add(self, kind, chars):
        self.calls[kind] += 1
        self.chars[kind] += chars

    def json_task(self, kind, prompt):
        self._add(kind, len(prompt))
        return self.llm.json_task(kind, prompt)

    def chat(self, messages, functions=None):
        chars = sum(len(m.get("content") or "") + (len(json.dumps(m["function_call"], ensure_ascii=False))
                                                    if m.get("function_call") else 0) for m in messages)
        if functions:
            chars += len(json.dumps(functions, ensure_ascii=False))
        self._add("chat", chars)
        return self.llm.chat(messages, functions)


BPS_TEMPLATES = {
    "fullOrgName": "Организация пользователя называется %s",
    "sflFullName": "Пользователя зовут %s",
    "inn": "Инн пользователя - %s",
    "login_id": "Логин пользователя - %s",
    "okopf": "Организационно правовая форма пользователя - %s",
}


def _is_uuid(v):
    try:
        uuid.UUID(str(v))
        return True
    except ValueError:
        return False


def _norm_decision(d):
    """Модель может ответить в snake_case (как в промпте) — приводим к полям Java-DTO."""
    if not isinstance(d, dict):
        return d
    m = dict(d)
    for a, b in (("new_text", "newText"), ("duplicate_of", "duplicateOf")):
        if a in m and b not in m:
            m[b] = m.pop(a)
    return m


class HandlerRun:
    """Один запуск MemoryHandlerServiceImpl.memoryHandler (одна транзакция)."""

    def __init__(self, system, user):
        self.s = system
        self.user = user
        self.facts = None
        self.to_delete = []
        self.result = None

    # --- 1. проверки и чтение снимка (до коммита изменения другим запускам не видны)
    def begin(self):
        s, user = self.s, self.user
        if s.fixes.lock:
            if user in s.locks:
                return self._finish("skipped", "замок занят параллельным разбором")
            s.locks.add(user)
        if s.db.exists_status(user, PROCESSING):
            return self._finish("skipped", "есть факты в статусе PROCESSING")
        if not s.db.exists_status(user, PENDING):
            return self._finish("skipped", "нет фактов в статусе PENDING")
        self.facts = s.db.user_facts(user)
        self.original = {f["guid"]: dict(f) for f in self.facts}
        for f in self.facts:
            if f["status"] == PENDING:
                f["status"] = PROCESSING
        s.log(f"[HANDLE] разбор: всего фактов {len(self.facts)}, новых "
              f"{sum(f['status'] == PROCESSING for f in self.facts)}")
        return True

    # --- 2. вызовы моделей и применение решений (в памяти)
    def compute(self):
        if self.result:
            return
        try:
            self._annotate()
            self._classify()
        except Crash as e:
            self.s.log(f"[HANDLE] ПАДЕНИЕ разбора: {e}. Транзакция откатывается, факты остаются PENDING")
            self._finish("crashed", str(e))
        except Exception as e:  # noqa: BLE001 — сбой модели или неразбираемый ответ: в Java транзакция
            # откатывается точно так же, факты остаются PENDING до следующего запуска
            self.s.log(f"[HANDLE] ПАДЕНИЕ разбора ({type(e).__name__}): {e}. Транзакция откатывается")
            self._finish("crashed", f"{type(e).__name__}: {e}")

    # --- 3. коммит
    def commit(self):
        if self.result:
            return self.result
        # Как Hibernate: пишем только изменённые строки; если строку уже удалил другой запуск,
        # UPDATE/DELETE затрагивает 0 строк -> StaleStateException -> откат всей транзакции.
        now = self.s.clock.now()
        deleted = {f["guid"] for f in self.to_delete}
        dirty = [f for f in self.facts if f["guid"] not in deleted and f != self.original.get(f["guid"])]
        gone = [g for g in list(deleted) + [f["guid"] for f in dirty] if not self.s.db.row_exists(g)]
        if gone:
            self.s.log(f"[HANDLE] ПАДЕНИЕ при коммите: {len(gone)} строк уже изменены/удалены параллельным разбором "
                       f"(StaleStateException). Транзакция откатывается")
            return self._finish("crashed", "StaleStateException: строку уже удалил параллельный разбор")
        for g in deleted:
            self.s.db.delete_fact(g)
        for f in dirty:
            f["update_date"] = now
            self.s.db.write_fact(f)
        return self._finish("done", "")

    def run(self):
        if self.begin() is True:
            self.compute()
        return self.commit()

    def _finish(self, status, reason):
        self.s.locks.discard(self.user)
        self.result = {"status": status, "reason": reason}
        if status == "skipped":
            self.s.log(f"[HANDLE] разбор пропущен: {reason}")
        return self.result

    # ---------------- аннотатор (KEEP / DELETE / UPDATE)
    def _annotate(self):
        s = self.s
        req = []
        for f in self.facts:  # Gson не пишет null-поля
            item = {"index": f["guid"], "text": f["fact"]}
            if f["topic_code"]:
                item["topic"] = f["topic_code"]
            if f["type"]:
                item["currentScope"] = f["type"]
            item["updateTime"] = str(int(f["update_date"]))
            item["isNew"] = f["status"] == PROCESSING
            req.append(item)
        prompt = prompts.fill(prompts.MEMORY_PROMPT, json.dumps({"facts": req}, ensure_ascii=False))
        resp = s.llm.json_task("annotate", prompt)
        decisions = (resp or {}).get("decisions")
        if decisions is None:
            raise Crash("аннотатор вернул пустой ответ (NullPointerException)")
        decisions = [_norm_decision(d) for d in decisions]
        for d in decisions:  # LangChain4j не сможет разобрать не-UUID в поле UUID — в обеих версиях
            if isinstance(d, dict) and d.get("index") is not None and not _is_uuid(d["index"]):
                raise Crash(f"модель вернула '{d['index']}' вместо UUID в index — ответ не разбирается")
        self._apply(decisions)

    def _apply(self, decisions):
        s, fx = self.s, self.s.fixes
        by_id = {f["guid"]: f for f in self.facts}
        default_scope = "global" if fx.default_type else "local"
        decided, commands = {}, []
        for d in decisions:
            if d is None:
                continue
            e = by_id.get(str(d.get("index")))
            if e is None:
                s.log(f"[HANDLE] решение для неизвестного факта {d.get('index')} пропущено")
                continue
            action = d.get("action")
            if action is None and not fx.crash_guards:
                raise Crash("аннотатор вернул пустой action (NullPointerException в switch)")
            decided[e["guid"]] = action
            if fx.affects_delete and d.get("reason") == "explicit_request" and d.get("affects"):
                commands.append((e, d["affects"]))
            if action == "DELETE":
                self.to_delete.append(e)
                s.log(f"[HANDLE] DELETE «{e['fact']}» ({d.get('reason')})")
            elif action == "UPDATE":
                e["type"] = d.get("scope") if d.get("scope") is not None else default_scope
                if d.get("newText") is not None:
                    s.log(f"[HANDLE] UPDATE «{e['fact']}» → «{d['newText']}»")
                    e["fact"] = d["newText"]
            elif action == "KEEP":
                if e["type"] is None:
                    e["type"] = d.get("scope") if d.get("scope") is not None else default_scope
            else:
                s.log(f"[HANDLE] неизвестное действие '{action}' — факт оставлен как есть")
        for cmd, affects in commands:
            for raw in affects:
                t = by_id.get(str(raw))
                if t is None or t in self.to_delete:
                    continue
                self.to_delete.append(t)
                s.log(f"[HANDLE] DELETE «{t['fact']}» по команде «{cmd['fact']}» "
                      f"(модель поставила ему {decided.get(t['guid'], 'нет решения')})")
        for f in self.facts:
            if f["guid"] not in decided and f not in self.to_delete:
                s.log(f"[HANDLE] модель не вернула решение для «{f['fact']}»")
        self.facts = [f for f in self.facts if f not in self.to_delete]
        if fx.default_type:
            for f in self.facts:
                if f["type"] is None:
                    f["type"] = "global"
                    s.log(f"[HANDLE] «{f['fact']}»: тип не определён → global по умолчанию")
        for f in self.facts:
            if f["type"] is None and f["status"] == PROCESSING:
                s.log(f"[HANDLE] «{f['fact']}» остался без типа — при чтении не найдётся")

    # ---------------- классификатор топиков
    def _classify(self):
        s, fx = self.s, self.s.fixes
        processing = [f for f in self.facts if f["status"] == PROCESSING]
        if fx.crash_guards and not processing:
            return
        topics = s.db.topics()
        topic_list = "\n".join(f"- {t['code']} ({t['description']})" for t in topics)
        facts_json = json.dumps([{"index": f["guid"], "text": f["fact"]} for f in processing], ensure_ascii=False)
        resp = s.llm.json_task("classify", prompts.fill(prompts.TOPIC_PROMPT, topic_list, facts_json))
        items = (resp or {}).get("topics")
        if items is None:
            raise Crash("классификатор вернул пустой ответ (NullPointerException)")
        mapping = {}
        for it in items:
            idx, code = str(it.get("index")), it.get("topic")
            if not fx.crash_guards:  # Collectors.toMap: падает на повторе ключа и на null-значении
                if idx in mapping:
                    raise Crash(f"классификатор вернул факт {idx} дважды (IllegalStateException: Duplicate key)")
                if code is None:
                    raise Crash("классификатор вернул пустой топик (NullPointerException в Collectors.toMap)")
            if idx not in mapping:
                mapping[idx] = code
        temp_guid = s.db.topic_guid("temporary")
        for f in processing:
            code = mapping.get(f["guid"])
            if code is None:
                s.log(f"[HANDLE] «{f['fact']}»: топик не назначен, остаётся пустым")
            else:
                g = s.db.topic_guid(code)
                f["topic_guid"] = g or temp_guid
                if not g:
                    s.log(f"[HANDLE] «{f['fact']}»: неизвестный топик '{code}' → temporary")
            f["status"] = PROCESSED


class MemorySystem:
    def __init__(self, llm, fixes, db=None, clock=None, agent_prompt=None):
        self.llm, self.fixes = llm, fixes
        self.db = db or MemoryDB()
        self.clock = clock or Clock()
        self.custom_prompt = agent_prompt is not None
        self.agent_prompt = agent_prompt or prompts.AGENT_SYSTEM_PROMPT
        if fixes.prompt_memory and not self.custom_prompt:  # без функции памяти фраза «воспользуйся функцией» лишняя
            self.agent_prompt = pp.AGENT_SYSTEM_PROMPT_PROFILE
        self.msg_log = []          # все реплики всех диалогов со временем: для блока недавних реплик
        self.locks = set()
        self.dialogs = {}
        self.events = []
        self.fail_memory_read = False
        self.fail_memory_write = False  # сбой записи (ERR): сбор, разбор и команды памяти не могут ничего записать
        self.memory_on = {}        # пользователь -> положение переключателя памяти в настройках (SWITCH); нет — включена.
        #                            Старая схема и шаги 2–6 переключатель не читают: это пробел, который меряет бенч
        self.agents = {}           # id агента -> название (AGENT); диалог без агента — основной ассистент
        self.dialog_agent = {}     # диалог -> id агента
        self.timed = False         # сценарий идёт по настоящей шкале времени: агенту подставляется текущая дата
        self.orgs = {}             # ИНН -> организация пользователя из профиля банка
        self.dialog_org = {}       # диалог -> ИНН текущей организации (под ней пользователь работает в СББОЛ)
        self.sessions = {}         # диалог -> {"last": время последней реплики, "open": bool, "count": сессий}
        self.user_messages = 0

    def log(self, msg):
        self.events.append(msg)

    # ======================= оркестратор
    def seed_fact(self, user, fact, *, dialog=None, type_=None, topic=None, status=PROCESSED, age_sec=0, guid=None):
        now = self.clock.now() - age_sec
        return self.db.insert_fact(user, fact, status=status, now=now, dialog=dialog, type_=type_, topic_code=topic,
                                   guid=guid)

    def save_bps(self, user, params):
        """saveGlobalFactsAboutUser: факты от БПС — global, PROCESSED, без топика и без диалога."""
        for key, value in params.items():
            self.db.insert_fact(user, BPS_TEMPLATES[key] % value, status=PROCESSED, now=self.clock.now(), type_="global")
        self.log(f"[BPS] сохранено {len(params)} системных фактов (без топика)")

    def set_orgs(self, user, orgs):
        """Организации пользователя из профиля банка. Старая логика знает только шаблоны БПС, поэтому каждая
        организация превращается в системные факты (название, ИНН, ОПФ) — как если бы БПС пришёл по каждой."""
        for o in orgs:
            self.orgs[str(o["inn"])] = o
            params = {"fullOrgName": o["name"], "inn": o["inn"]}
            if o.get("okopf"):
                params["okopf"] = o["okopf"]
            if o.get("person"):
                params["sflFullName"] = o["person"]
            self.save_bps(user, params)

    # ======================= сессии: пауза в диалоге закрывает сессию
    def tick(self, user):
        """Закрывает сессии диалогов, где пауза превысила SESSION_GAP_MIN.

        В старой схеме разбор идёт на каждой реплике, поэтому здесь только учёт сессий для метрик стоимости.
        Целевая схема запустит здесь «разбор сессии» (один вызов модели на сессию)."""
        for dialog, st in self.sessions.items():
            if st["open"] and self.clock.t - st["last"] >= SESSION_GAP_MIN * 60:
                st["open"] = False
                self.session_end(user, dialog)

    def end_session(self, user, dialog):
        """Сессия закончена без паузы по часам. Старые сценарии без шкалы времени считают, что к вопросу всё
        сказанное уже разобрано; старая схема и так разбирает каждую реплику, профиль запускает разбор здесь."""
        st = self.sessions.get(dialog)
        if st and st["open"]:
            st["open"] = False

    def memory_table(self, user):
        """Память в конце сценария — для отчёта."""
        return [{"Факт": f["fact"], "Статус": f["status"], "Тип": f["type"] or "—", "Топик": f["topic_code"] or "—"}
                for f in self.db.user_facts(user)]

    def session_end(self, user, dialog):
        self.log(f"[SESSION] диалог {dialog}: пауза ≥ {SESSION_GAP_MIN} мин — сессия закончена"
                 " (старая схема разбирает каждую реплику сразу, отдельного разбора нет)")

    def _touch(self, dialog):
        st = self.sessions.setdefault(dialog, {"last": self.clock.t, "open": False, "count": 0})
        if not st["open"]:
            st["open"] = True
            st["count"] += 1
        st["last"] = self.clock.t
        self.user_messages += 1

    def session_count(self):
        return sum(st["count"] for st in self.sessions.values())

    # ======================= профиль: что о пользователе записано
    def profile_lines(self, user):
        """Строки профиля для проверок profile_has / profile_lacks / prev_value.

        В старой схеме профиля нет — это строки таблицы memory (все статусы). Разделов и истории значений
        у них нет: section и was = None. Целевая схема вернёт строки документа с разделом и «было»."""
        return [{"text": f["fact"], "section": None, "was": None, "status": f["status"]}
                for f in self.db.user_facts(user)]

    def collect(self, user, dialog, history):
        if self.fail_memory_write:
            self.log("[COLLECT] сбой записи в память (эмуляция): факты реплики не сохранены")
            return []
        msgs = [{"messageText": m["content"], "role": m["role"]} for m in history[-4:]]
        if self.fixes.collect_context:
            known = self._known_facts(user)
            prompt = prompts.fill(pp.COLLECT_PROMPT_CTX, "\n".join(f"- {f}" for f in known) or "(пока ничего)",
                                  json.dumps(msgs, ensure_ascii=False))
        else:
            prompt = prompts.fill(prompts.COLLECT_PROMPT, json.dumps(msgs, ensure_ascii=False))
        facts = [f for f in (self.llm.json_task("collect", prompt).get("facts") or []) if f and f.strip()]
        now = self.clock.now()
        for f in facts:
            self.db.insert_fact(user, f, status=PENDING, now=now, dialog=dialog)
        self.log(f"[COLLECT] выделено фактов: {len(facts)}" + (f": {facts}" if facts else ""))
        return facts

    def handler(self, user):
        return HandlerRun(self, user).run()

    # ======================= агент ГигаЧата: чтение
    def read_facts(self, user, dialog, topics):
        if self.fail_memory_read:
            raise MemoryReadError("БД памяти недоступна (эмуляция)")
        if self.fixes.no_topic_filter:
            return self._read_all(user, dialog)
        if not topics:
            return []
        ph = ",".join("?" * len(topics))
        c = self.db.conn
        if self.fixes.read_null_topic:
            glob = c.execute(
                f"""select coalesce(t.code, case when m.dialog_guid is null then 'system' else 'uncategorized' end) as topic, m.fact
                    from memory m left join topic t on t.guid = m.topic_guid
                    where m.digital_user_id=? and m.status='PROCESSED' and m.type='global'
                    and (t.code in ({ph}) or m.topic_guid is null)""", (user, *topics)).fetchall()
        else:
            glob = c.execute(
                f"""select t.code as topic, m.fact from memory m join topic t on t.guid = m.topic_guid
                    where m.digital_user_id=? and m.status='PROCESSED' and m.type='global' and t.code in ({ph})""",
                (user, *topics)).fetchall()
        loc = c.execute(
            f"""select t.code as topic, m.fact from memory m join topic t on t.guid = m.topic_guid
                where m.digital_user_id=? and m.dialog_guid=? and m.status='PROCESSED' and m.type='local'
                and t.code in ({ph})""", (user, dialog, *topics)).fetchall()
        rows = list(glob) + list(loc)
        if self.fixes.read_unverified:
            since = self.clock.t - UNPROCESSED_WINDOW_SEC
            rows += c.execute(
                """select 'unverified' as topic, m.fact from memory m
                   where m.digital_user_id=? and m.status in ('PENDING','PROCESSING')
                   and (m.dialog_guid=? or m.insert_date > ?) order by m.insert_date desc limit ?""",
                (user, dialog, since, UNPROCESSED_LIMIT)).fetchall()
        return self._dedup(rows)

    def _read_all(self, user, dialog):
        """Чтение без фильтра по темам: все факты пользователя, самые свежие первыми.

        Темы модель всё равно угадывает (их выбор мы игнорируем), поэтому факт не теряется из-за
        несовпадения темы при записи и при чтении. Отдаём: global-факты, local-факты текущего диалога
        и ещё не разобранные (их статус виден по метке 'unverified').
        """
        rows = self.db.conn.execute(
            """select case
                        when m.status <> 'PROCESSED' then 'unverified'
                        when t.code is not null then t.code
                        when m.dialog_guid is null then 'system'
                        else 'uncategorized' end as topic,
                      m.fact
               from memory m left join topic t on t.guid = m.topic_guid
               where m.digital_user_id = ?
                 and ( (m.status = 'PROCESSED' and m.type = 'global')
                    or (m.status = 'PROCESSED' and m.type = 'local' and m.dialog_guid = ?)
                    or (m.status in ('PENDING','PROCESSING') and (m.dialog_guid = ? or m.insert_date > ?)) )
               order by m.insert_date desc limit ?""",
            (user, dialog, dialog, self.clock.t - UNPROCESSED_WINDOW_SEC, READ_ALL_LIMIT)).fetchall()
        return self._dedup(rows)

    @staticmethod
    def _dedup(rows):
        seen, out = set(), []
        for r in rows:  # в Java это Set<MemoryFact>
            key = (r["fact"], r["topic"])
            if key not in seen:
                seen.add(key)
                out.append({"text": r["fact"], "topic": r["topic"]})
        return out

    # ======================= агент ГигаЧата: ответ с функцией памяти
    def function_spec(self):
        fx = self.fixes
        return [{
            "name": prompts.FUNCTION_NAME,
            "description": prompts.FUNCTION_DESCRIPTION_AFTER if fx.read_null_topic else prompts.FUNCTION_DESCRIPTION_BEFORE,
            "parameters": {"type": "object", "required": ["topics"], "properties": {"topics": {
                "type": "array",
                "description": prompts.TOPIC_PARAM_AFTER if fx.or_description else prompts.TOPIC_PARAM_BEFORE,
                "items": {"type": "string", "enum": self.db.topic_codes()},
            }}},
        }]

    def system_prompt(self, dialog):
        """Системный промпт агента. В проде дата подставляется в {CURRENT_DATE}/{CURRENT_WEEKDAY}, а параметры
        сообщения (в том числе организация из СББОЛ) — через плейсхолдеры экшена. Без шкалы времени и
        организаций промпт не меняется, поэтому старые сценарии идут как раньше."""
        prompt = self.agent_prompt
        if self.timed:
            d = self.clock.dt()
            date, wd = d.date().isoformat(), WEEKDAYS[d.weekday()]
            if "{CURRENT_DATE}" in prompt or "{CURRENT_WEEKDAY}" in prompt:
                prompt = prompt.replace("{CURRENT_DATE}", date).replace("{CURRENT_WEEKDAY}", wd)
            else:
                prompt += f"\nСегодня {wd}, {date}."
        org = self.orgs.get(self.dialog_org.get(dialog) or "")
        if org:
            prompt += f"\nПользователь сейчас работает в СберБизнесе от организации: {org['name']}, ИНН {org['inn']}."
        agent = self.agents.get(self.dialog_agent.get(dialog) or "")
        if agent:
            prompt += f"\nВ этом диалоге ты работаешь как {agent}."
        return prompt

    # ======================= шаги 2–3: факты в промпте, недавние реплики, сбор с контекстом
    def _known_facts(self, user, limit=READ_ALL_LIMIT):
        rows = self.db.conn.execute(
            """select fact from memory where digital_user_id = ? and status = 'PROCESSED' and type = 'global'
               order by insert_date desc limit ?""", (user, limit)).fetchall()
        return [r["fact"] for r in rows]

    def facts_block(self, user, dialog):
        """Шаг 2: все разобранные факты пользователя (≤100, свежие первыми) — в системный промпт."""
        rows = self.db.conn.execute(
            """select fact, update_date from memory
               where digital_user_id = ? and status = 'PROCESSED'
                 and (type = 'global' or (type = 'local' and dialog_guid = ?))
               order by insert_date desc limit ?""", (user, dialog, READ_ALL_LIMIT)).fetchall()
        lines = [f"- {r['fact']} ({datetime.fromtimestamp(r['update_date'], TZ):%d.%m.%Y})" for r in rows]
        return pp.FACTS_HEADER + "\n" + ("\n".join(lines) or "(пока ничего)"), len(lines)

    def recent_block(self, user, dialog, after=None):
        """Реплики пользователя из других диалогов за RECENT_WINDOW_H часов, свежие, не больше RECENT_MAX и
        RECENT_BUDGET_TOKENS. after(dialog) -> время, после которого реплики диалога ещё не разобраны."""
        since = self.clock.t - RECENT_WINDOW_H * 3600
        picked, chars = [], 0
        for m in reversed(self.msg_log):
            if m["role"] != "user" or m["user"] != user or m["dialog"] == dialog or m["t"] < since:
                continue
            if after and m["t"] <= after(m["dialog"]):
                continue
            line = f"- [{datetime.fromtimestamp(m['t'], TZ):%d.%m %H:%M}] {m['text']}"
            if len(picked) >= RECENT_MAX or chars + len(line) > RECENT_BUDGET_TOKENS * CHARS_PER_TOKEN:
                break
            picked.append(line)
            chars += len(line)
        if not picked:
            return "", 0
        return pp.RECENT_HEADER + "\n" + "\n".join(reversed(picked)), len(picked)

    def memory_prompt(self, user, dialog):
        """Что о пользователе кладётся в системный промпт (шаг 2). Профиль (шаг 4) переопределяет."""
        facts, n = self.facts_block(user, dialog)
        recent, k = self.recent_block(user, dialog)
        return facts + ("\n\n" + recent if recent else ""), {"строк памяти": n, "недавних реплик": k}

    def answer(self, user, dialog, history, user_text):
        messages = [{"role": "system", "content": self.system_prompt(dialog)}] + \
                   [{"role": m["role"], "content": m["content"]} for m in history] + \
                   [{"role": "user", "content": user_text}]
        functions = self.function_spec()
        turn = {"memory_calls": [], "error": None}
        if self.fixes.prompt_memory:
            if self.fail_memory_read:  # память не прочиталась: как при ошибке функции памяти в старой схеме
                turn["error"] = "БД памяти недоступна (эмуляция)"
                self.log(f"[AGENT] ошибка при чтении памяти: {turn['error']}")
                if not self.fixes.fallback:
                    turn.update(text="", empty=True, unfinished_function_call=False)
                    self.log("[AGENT] пользователь получил ПУСТОЙ ответ")
                    return turn
                self.log("[AGENT] ответ без памяти в промпте")
                return self._answer_no_function(user, dialog, messages, turn)
            block, info = self.memory_prompt(user, dialog)
            messages[0]["content"] += "\n\n" + block
            info["≈ токенов памяти в промпте"] = approx_tokens(len(block))
            turn["prompt_memory"] = info
            return self._answer_no_function(user, dialog, messages, turn)
        resp = self.llm.chat(messages, functions)
        max_calls = MAX_FUNCTION_CALLS if self.fixes.multi_call else 1
        n = 0
        while _is_fc(resp) and n < max_calls:
            n += 1
            resp = self._handle_fc(user, dialog, messages, functions, resp, last=(n == max_calls), turn=turn)
        turn["text"] = (resp.get("content") or "").strip()
        turn["empty"] = not turn["text"]
        turn["unfinished_function_call"] = _is_fc(resp)
        if turn["empty"]:
            self.log("[AGENT] пользователь получил ПУСТОЙ ответ" +
                     (" (модель всё ещё просит функцию)" if _is_fc(resp) else ""))
        return turn

    def agent_functions(self):
        """Функции агента, когда память лежит в промпте: функции чтения нет; remember / forget — в шаге 5."""
        return None

    def _answer_no_function(self, user, dialog, messages, turn):
        resp = self.llm.chat(messages, self.agent_functions())
        turn["text"] = (resp.get("content") or "").strip()
        turn["empty"] = not turn["text"]
        turn["unfinished_function_call"] = _is_fc(resp)
        if turn["empty"]:
            self.log("[AGENT] пользователь получил ПУСТОЙ ответ")
        return turn

    def _handle_fc(self, user, dialog, messages, functions, resp, last, turn):
        before_call = list(messages)
        try:
            topics = list((resp["function_call"].get("arguments") or {}).get("topics") or [])
            facts = self.read_facts(user, dialog, topics)
            turn["memory_calls"].append({"topics": topics, "facts": facts})
            self.log(f"[AGENT] функция памяти: топики {topics} → фактов {len(facts)}")
            fc_msg = {"role": "assistant", "content": "", "function_call": resp["function_call"]}
            if resp.get("functions_state_id"):
                fc_msg["functions_state_id"] = resp["functions_state_id"]
            messages.append(fc_msg)
            messages.append({"role": "function", "name": prompts.FUNCTION_NAME,
                             "content": json.dumps({"facts": facts}, ensure_ascii=False)})
            strip = last and self.fixes.multi_call
            if strip:
                self.log("[AGENT] лимит вызовов функции — следующий ответ без функций")
            return self.llm.chat(messages, None if strip else functions)
        except Exception as e:  # noqa: BLE001 — как catch (Exception e) в Java
            turn["error"] = str(e)
            self.log(f"[AGENT] ошибка при работе с памятью: {e}")
            if self.fixes.fallback:
                messages[:] = before_call
                self.log("[AGENT] повторный запрос без функции памяти")
                return self.llm.chat(messages, None)
            return resp

    # ======================= диалог целиком
    def user_turn(self, user, dialog, user_text, *, collect=True, process=True, scripted_reply=None):
        """scripted_reply — готовый ответ ассистента вместо вызова модели. Нужен, когда сценарий проверяет
        реакцию памяти на конкретную реплику ассистента (например, ассистент предположил — пользователь подтвердил)."""
        hist = self.dialogs.setdefault(dialog, [])
        self._touch(dialog)
        if scripted_reply is not None:
            turn = {"memory_calls": [], "error": None, "text": scripted_reply, "empty": False,
                    "unfinished_function_call": False, "scripted": True}
        else:
            turn = self.answer(user, dialog, hist, user_text)
        hist.append({"role": "user", "content": user_text})
        self.msg_log.append({"user": user, "dialog": dialog, "t": self.clock.t, "role": "user", "text": user_text})
        if collect:  # в проде сбор стартует параллельно ответу и завершается позже него
            self.collect(user, dialog, hist)
            if process:
                turn["handler"] = self.handler(user)
        hist.append({"role": "assistant", "content": turn["text"]})
        self.msg_log.append({"user": user, "dialog": dialog, "t": self.clock.t, "role": "assistant", "text": turn["text"]})
        return turn


def _is_fc(resp):
    return bool(resp) and resp.get("finish_reason") == "function_call"
