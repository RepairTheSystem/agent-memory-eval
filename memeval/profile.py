"""Structured profile memory with validated edits, bounded views and archival search.

Experimental state is held in process memory. This module does not implement
a production database or durable distributed transactions.
"""
import difflib
import json
import re
from datetime import datetime, timedelta

from . import prompts_profile as pp
from .pipeline import SESSION_GAP_MIN, TZ, MemorySystem, _is_fc, approx_tokens

READ_BUDGET_TOKENS = 1500   # бюджет вида для чтения
MAX_OPS = 15                # не больше стольких правок за разбор
MAX_SLEEP_OPS = 80          # во сне правок больше: сжимается весь профиль
SLEEP_ROUNDS = 3            # сон повторяется, пока профиль не влезет в бюджет
_STOP = {"забудь", "забыть", "удали", "удалить", "сотри", "пожалуйста", "больше", "помни", "информацию", "это", "что",
         "где", "мой", "моя", "мое", "мои", "мне", "меня", "про", "нас", "наш", "наша", "наше", "наши", "уберите", "убери"}
CURRENT_MAX_DAYS = 90       # until у «Текущих дел» не дальше стольких дней
WAS_SHOW_DAYS = 90          # «раньше — …» показывается агенту столько дней после замены
MAX_COMMAND_CALLS = 3
SEARCH_TOP = 5              # search_memory: сколько строк вернуть
EXPLICIT = {"remember": re.compile(r"запомни|сохрани|запиши|учти|не забудь|зафиксируй|занеси|remember|save|note that|"
                                   r"keep in mind|don t forget"),
            "forget": re.compile(r"забуд|удали|сотри|не помни|больше не храни|forget|delete|remove|erase")}

USER_SECTIONS = (("personal", "Личность"), ("prefs", "Предпочтения"), ("current", "Текущие дела"))
ORG_SECTIONS = (("bank", "Реквизиты (профиль банка)"), ("people", "Люди"), ("assets", "Деятельность и активы"),
                ("finance", "Финансы и налоги"), ("partners", "Контрагенты и договоры"))
ID_PREFIX = {"personal": "p", "prefs": "f", "current": "c", "bank": "r"}

# темы старой схемы → разделы профиля (для фактов, положенных в память сценарием напрямую)
TOPIC_SECTION = {"legal_entity": "assets", "business_license": "bank", "profession": "assets", "tax_system": "finance",
                 "tax_debt": "finance", "accounting_period": "finance", "bank_products": "finance",
                 "business_risk": "finance", "contract": "partners", "temporary": "current"}

PDN = [(re.compile(r"(?<!\d)(?:\d{4}[ -]?){3}\d{4}(?!\d)"), "номер карты"),
       (re.compile(r"(?<!\d)\d{2}\s?\d{2}\s\d{6}(?!\d)"), "паспорт"),
       (re.compile(r"(?:\+7|(?<!\d)8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)"), "телефон"),
       (re.compile(r"(?<!\d)\d{3}-\d{3}-\d{3}[ -]\d{2}(?!\d)"), "СНИЛС")]
FORGET_MARKERS = ("забуд", "удали", "сотри", "не помни", "больше не", "убери", "forget", "delete", "remove", "erase")
CONFIRM = re.compile(r"^\s*(да|верно|точно|так и есть|именно|ага|угу|правильно|yes|yeah|yep|right|correct|exactly)\b")
_TAG = re.compile(r"\s⟦([^⟧]*)⟧\s*$")
_ANY_TAG = re.compile(r"\s*⟦[^⟧]*⟧?")


def _clean_text(text):
    """Метки пишет только код: если модель скопировала метку в текст, она вырезается. Строка профиля — одна строка."""
    t = _ANY_TAG.sub("", str(text or ""))
    return re.sub(r"\s*\n+\s*", "; ", t.strip()).strip()


def _clean_id(raw):
    """Модель может прислать id вместе с хвостом метки («o1|2026-10-05», «⟦o1⟧») — берём сам id."""
    return re.split(r"[|\s]", str(raw or "").strip().strip("⟦⟧").strip())[0]


def _norm(text):
    t = (text or "").lower().replace("ё", "е")
    t = re.sub(r"[«»\"'“”„()\[\].,;:!?—–-]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _found(evidence, texts):
    """Цитата есть в одной из реплик: как подстрока или ≥ 75% её слов в одной реплике (нечёткое сравнение)."""
    ev = _norm(evidence)
    if len(ev) < 3:
        return False
    words = [w for w in ev.split() if len(w) > 2] or ev.split()
    for t in texts:
        nt = _norm(t)
        if ev in nt:
            return True
        tw = set(nt.split())
        if words and sum(w in tw for w in words) / len(words) >= 0.75:
            return True
    return False


def _same(a, b):
    na, nb = _norm(a), _norm(b)
    return na == nb or bool(na and na in nb) or difflib.SequenceMatcher(None, na, nb).ratio() >= 0.85


def _pdn(text):
    return next((what for rx, what in PDN if rx.search(text or "")), None)


def _numbers(text):
    return {x.replace(",", ".") for x in re.findall(r"\d+(?:[.,]\d+)?", text or "")}


def _points_to(evidence, line_text):
    """«Забудь …» указывает на строку: хотя бы один корень значимого слова цитаты есть в строке.
    Если значимых слов нет («забудь это»), судить не по чему — пропускаем."""
    words = [w for w in _norm(evidence).split() if len(w) >= 4 and w not in _STOP and not w.startswith("забуд")]
    if not words:
        return True
    text = _norm(line_text)
    return any(w[:max(3, len(w) - 3)] in text for w in words)


def _date(ts):
    return datetime.fromtimestamp(ts, TZ)


def _wd(d):
    return pp.L["wd_short"][d.weekday()]


def calendar(ts_list):
    """Подсказка для перевода относительных дат: для каждой даты реплик — вчера, завтра, дни этой и прошлой недели,
    конец месяца. Модель плохо считает дни недели сама; код считает точно."""
    out = []
    for d in sorted({_date(t).date() for t in ts_list}):
        def f(x):
            return f"{_wd(x)} {x:%d.%m.%Y}"
        mon = d - timedelta(days=d.weekday())
        week = ", ".join(f(mon + timedelta(days=i)) for i in range(7))
        prev = ", ".join(f(mon - timedelta(days=7 - i)) for i in range(7))
        nxt = (d.replace(day=28) + timedelta(days=4))
        month_end = nxt - timedelta(days=nxt.day)
        out.append(pp.L["calendar"].format(d=f(d), d2=f(d - timedelta(days=2)), d1=f(d - timedelta(days=1)),
                                           n1=f(d + timedelta(days=1)), n2=f(d + timedelta(days=2)), week=week,
                                           prev=prev, me=f(month_end)))
    return "\n".join(out)


def _bm25_tokens(text):
    return [w[:5] for w in _norm(text).split() if len(w) >= 3]


def _bm25(query, docs, k1=1.5, b=0.75):
    """Поиск по строкам архива без сторонних библиотек: BM25 по грубым основам слов (первые 5 букв)."""
    import math
    toks = [_bm25_tokens(d) for d in docs]
    n = len(docs) or 1
    avg = sum(len(t) for t in toks) / n or 1
    df = {}
    for t in toks:
        for w in set(t):
            df[w] = df.get(w, 0) + 1
    q = set(_bm25_tokens(query))
    scores = []
    for i, t in enumerate(toks):
        s = 0.0
        for w in q:
            tf = t.count(w)
            if tf:
                idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
                s += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(t) / avg))
        scores.append(s)
    return sorted((i for i in range(len(docs)) if scores[i] > 0), key=lambda i: -scores[i])


_SUB_WORDS = (("people", ("директор", "бухгалтер", "сотрудник", "штат", "человек", "кадр", "управляющ", "менеджер",
                            "курьер", "водител", "бариста")),
              ("finance", ("налог", "усн", "осно", "патент", "выручк", "кредит", "лизинг", "счет", "овердрафт", "депозит",
                           "ставк", "оборот")),
              ("partners", ("поставщ", "клиент", "договор", "контрагент", "аренд")))


def _guess_sub(text):
    t = _norm(text)
    return next((sub for sub, keys in _SUB_WORDS if any(k in t for k in keys)), "assets")


def _label(text):
    m = re.match(r"\s*([^:«»\d]{2,40}):", text or "")
    return _norm(m.group(1)) if m else None


def _org_mentioned(org, texts):
    """В репликах названа эта организация: значимое слово названия или её форма (ООО, ИП, АО) отдельным словом."""
    words = _norm(org.get("name", "")).split()
    keys = [w for w in words if len(w) >= 4] + [w for w in words[:1] if len(w) <= 3]
    joined = " " + " ".join(_norm(t) for t in texts) + " "
    return any((" " + k + " ") in joined if len(k) <= 3 else k[:max(4, len(k) - 2)] in joined for k in keys)


def _parse_until(v):
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(str(v).strip(), fmt).date()
        except (ValueError, TypeError):
            continue
    return None


class ProfileSystem(MemorySystem):
    """Шаг 4 (и шаг 5 с флагами deep_sleep / commands) на тех же часах, диалогах и организациях, что и MemorySystem."""

    def __init__(self, llm, fixes, **kw):
        super().__init__(llm, fixes, **kw)
        self.profiles = {}
        self.journal = []          # memory_op: каждая предложенная правка и вердикт
        self.processed_until = {}  # memory_job.processed_until_message: диалог -> время последней разобранной реплики
        self.commanded = {}        # (пользователь, диалог, время реплики) -> что уже записала команда remember / forget
        self.hidden_ids = {}       # пользователь -> id строк, не влезших в вид для чтения (их находит search_memory)

    # ======================= документ
    def _prof(self, user):
        p = self.profiles.get(user)
        if p is None:
            p = self.profiles[user] = {"orgs": [], "lines": [], "archive": [], "seq": 0, "version": 0}
        return p

    def _orgs(self, user):
        p = self._prof(user)
        if not p["orgs"]:  # организаций из банка нет — один узел «бизнес пользователя»
            p["orgs"].append({"key": "main", "name": pp.L["default_org"], "current": True})
        return p["orgs"]

    def _org(self, user, key):
        return next((o for o in self._orgs(user) if o["key"] == key), None)

    def _current_org(self, user, dialog=None):
        inn = self.dialog_org.get(dialog) if dialog else None
        orgs = self._orgs(user)
        return self._org(user, inn) if inn and self._org(user, inn) else \
            next((o for o in orgs if o.get("current")), orgs[0])

    def _sections(self, user):
        out = [s for s, _ in USER_SECTIONS]
        for o in self._orgs(user):
            out += [f"org:{o['key']}/{s}" for s, _ in ORG_SECTIONS]
        return out

    def _new_line(self, user, section, text, ts, **extra):
        p = self._prof(user)
        p["seq"] += 1
        sub = section.split("/")[-1]
        line = {"id": f"{ID_PREFIX.get(sub, 'o')}{p['seq']}", "section": section, "text": text.strip(), "date": ts,
                "until": None, "was": None, "was_date": None, "bank": False, "overrides": None}
        line.update(extra)
        p["lines"].append(line)
        return line

    def _line(self, user, lid):
        return next((x for x in self._prof(user)["lines"] if x["id"] == str(lid)), None)

    def edit_view(self, user):
        """Вид для правки: документ с метками ⟦id|дата|до=|было=|банк|перекрывает=⟧. Метки пишет только код."""
        L = pp.L
        p, out = self._prof(user), [L["user_doc"]]

        def tag(x):
            parts = [x["id"], _date(x["date"]).date().isoformat()]
            if x["until"]:
                parts.append(f"{L['tag_until']}={x['until']}")
            if x["was"]:
                parts.append(f"{L['tag_was']}={x['was']}")
            if x["bank"]:
                parts.append(L["tag_bank"])
            if x["overrides"]:
                parts.append(f"{L['tag_overrides']}={x['overrides']}")
            return "|".join(p_.replace("⟧", "").replace("|", "/") if i > 1 else p_ for i, p_ in enumerate(parts))

        def lines(section):
            return [f"- {x['text']} ⟦{tag(x)}⟧" for x in p["lines"] if x["section"] == section]
        for s, _ in USER_SECTIONS:
            out += [f"## {L['titles'][s]}"] + lines(s)
        for o in self._orgs(user):
            out.append(f"\n{L['org_doc']} {o['name']} ⟦org={o['key']}⟧")
            for s, _ in ORG_SECTIONS:
                out += [f"## {L['titles'][s]}  ({L['section_mark']} org:{o['key']}/{s})"] + lines(f"org:{o['key']}/{s}")
        return "\n".join(out)

    def _parses(self, user):
        """После правки документ разбирается заново: каждая строка на месте, метки читаются."""
        doc = self.edit_view(user)
        seen, org, sec = [], None, None
        L = pp.L
        titles_u = {L["titles"][s]: s for s, _ in USER_SECTIONS}
        for raw in doc.splitlines():
            if raw.startswith(L["org_doc"]):
                m = re.search(r"⟦org=([^⟧]+)⟧", raw)
                if not m:
                    return False
                org = m.group(1)
            elif raw.startswith("## "):
                m = re.search(r"\(" + re.escape(L["section_mark"]) + r" (org:[^)]+)\)", raw)
                sec = m.group(1) if m else titles_u.get(raw[3:].strip())
                if sec is None:
                    return False
            elif raw.startswith("- "):
                m = _TAG.search(raw)
                if not m or not sec:
                    return False
                seen.append((sec, m.group(1).split("|")[0]))
        want = [(x["section"], x["id"]) for x in self._prof(user)["lines"]]
        return sorted(seen) == sorted(want) and org is not None

    # ======================= вид для чтения (агент)
    def read_view(self, user, dialog, budget=READ_BUDGET_TOKENS):
        p = self._prof(user)
        now = _date(self.clock.t)
        today = now.date()
        cur = self._current_org(user, dialog)
        overridden = {x["overrides"] for x in p["lines"] if x["overrides"]}
        visible = [x for x in p["lines"] if not (x["until"] and _parse_until(x["until"]) and _parse_until(x["until"]) < today)]

        L = pp.L

        def fmt(x):
            if x["overrides"]:
                said = f"{_date(x['date']):%d.%m.%Y}"
                return f"- {x['text']} ({L['override_note'].format(date=said)})"
            if x["bank"]:
                note = L["bank_note"] + (L["bank_overridden"] if x["id"] in overridden else "")
            elif x["was"] and x["was_date"] and (self.clock.t - x["was_date"]) <= WAS_SHOW_DAYS * 86400:
                note = L["was_note"].format(date=f"{_date(x['date']):%d.%m.%Y}", was=x["was"])
            elif x["until"]:
                pu = _parse_until(x["until"])
                note = L["until_note"].format(date=f"{pu:%d.%m.%Y}") if pu else x["until"]
            else:
                note = f"{_date(x['date']):%d.%m.%Y}"
            return f"- {x['text']} ({note})"

        def render(hidden):
            out = [pp.PROFILE_HEADER.format(date=f"{now:%d.%m.%Y}")]
            for s, _ in USER_SECTIONS:
                ls = [fmt(x) for x in visible if x["section"] == s and x["id"] not in hidden]
                if ls:
                    out += [f"### {L['titles'][s]}"] + ls
            for o in sorted(self._orgs(user), key=lambda o: o["key"] != cur["key"]):
                head = f"### {o['name']}" + (f", {L['inn']} {o['inn']}" if o.get("inn") else "") + \
                       (L["current_org"] if o["key"] == cur["key"] else "")
                block = []
                for s, _ in ORG_SECTIONS:
                    ls = [fmt(x) for x in visible if x["section"] == f"org:{o['key']}/{s}" and x["id"] not in hidden]
                    if ls:
                        block += [f"#### {L['titles'][s]}"] + ls
                if block or o.get("inn"):  # пустой узел «бизнес пользователя» без данных банка не показываем
                    out += [head] + block
            return "\n".join(out)
        hidden = set()
        if not visible:
            return pp.PROFILE_HEADER.format(date=f"{now:%d.%m.%Y}") + "\n" + pp.PROFILE_EMPTY, \
                {"строк профиля": 0, "скрыто из-за бюджета": 0, "истекло": len(p["lines"])}
        text = render(hidden)
        if budget == READ_BUDGET_TOKENS:  # то, что агент не увидел, найдёт search_memory
            self.hidden_ids[user] = hidden
        if approx_tokens(len(text)) > budget:
            # не влезло: сначала прячем старые строки других организаций, потом самые старые остальные; банк — никогда
            def rank(x):
                if not x["section"].startswith("org:"):
                    return 2  # личность, предпочтения, текущие дела — прячутся последними
                return 1 if x["section"].startswith(f"org:{cur['key']}/") else 0
            order = sorted((x for x in visible if not x["bank"]), key=lambda x: (rank(x), x["date"]))
            for x in order:
                hidden.add(x["id"])
                text = render(hidden)
                if approx_tokens(len(text)) <= budget:
                    break
        return text, {"строк профиля": len(visible) - len(hidden), "скрыто из-за бюджета": len(hidden),
                      "истекло": len(p["lines"]) - len(visible)}

    def memory_prompt(self, user, dialog):
        text, info = self.read_view(user, dialog)
        if info["скрыто из-за бюджета"] and info["скрыто из-за бюджета"] != getattr(self, "_hidden_logged", None):
            self._hidden_logged = info["скрыто из-за бюджета"]
            self.log(f"[PROFILE] вид для чтения не влез в {READ_BUDGET_TOKENS} токенов: скрыто строк "
                     f"{info['скрыто из-за бюджета']}")
        recent, k = self.recent_block(user, dialog, after=lambda d: self.processed_until.get(d, float("-inf")))
        info["недавних реплик"] = k
        if self.fixes.commands:
            text += "\n\n" + pp.CLAIM_RULE
        if self.fixes.search_memory:
            n_more = len(self._prof(user)["archive"]) + len(self.hidden_ids.get(user, ()))
            if n_more:
                text += "\n\n" + pp.SEARCH_HINT.format(n=n_more)
                info["в архиве и за бюджетом"] = n_more
        return text + ("\n\n" + recent if recent else ""), info

    # ======================= данные банка и сценария
    def set_orgs(self, user, orgs):
        p = self._prof(user)
        for o in orgs:
            self.orgs[str(o["inn"])] = o
            key = str(o["inn"])
            node = {"key": key, "name": o["name"], "inn": key, "current": bool(o.get("current"))}
            p["orgs"] = [x for x in p["orgs"] if x["key"] != key] + [node]
            self._bank_lines(user, key, {"fullOrgName": o["name"], "inn": key, "okopf": o.get("okopf")})
            if o.get("person"):
                self._new_line(user, "personal", pp.L["bank_person"].format(v=o["person"]), self.clock.now(), bank=True)
        if p["orgs"] and not any(o.get("current") for o in p["orgs"]):
            p["orgs"][0]["current"] = True
        self.log(f"[BANK] организаций из профиля банка: {len(orgs)}")

    def _bank_lines(self, user, key, params):
        for k, label in pp.L["bank_labels"].items():
            if params.get(k):
                self._new_line(user, f"org:{key}/bank", f"{label}: {params[k]}", self.clock.now(), bank=True)

    def save_bps(self, user, params):
        """Профиль БПС → раздел «Реквизиты» кодом, без модели. Имя пользователя — в «Личность» с пометкой банка."""
        p = self._prof(user)
        org_params = {k: v for k, v in params.items() if k in ("fullOrgName", "inn", "okopf", "login_id")}
        if org_params:
            key = str(params.get("inn") or "main")
            node = next((o for o in p["orgs"] if o["key"] == key), None)
            if node is None:
                node = {"key": key, "name": params.get("fullOrgName") or pp.L["default_org"],
                        "current": not p["orgs"]}
                if params.get("inn"):
                    node["inn"] = key
                p["orgs"].append(node)
            self._bank_lines(user, key, org_params)
        if params.get("sflFullName"):
            self._new_line(user, "personal", pp.L["bank_person"].format(v=params["sflFullName"]), self.clock.now(),
                           bank=True)
        self.log(f"[BANK] профиль банка: {len(params)} полей → «Реквизиты»")

    def seed_fact(self, user, fact, *, dialog=None, type_=None, topic=None, status="PROCESSED", age_sec=0, guid=None):
        """Факт, положенный сценарием напрямую. local-факт диалога — это просто реплика в истории диалога
        (понятия local в целевой схеме нет); неразобранный — реплика, которая ещё ждёт разбора."""
        ts = self.clock.now() - age_sec
        if type_ == "local" and dialog:
            self.dialogs.setdefault(dialog, []).append({"role": "user", "content": fact})
            return None
        if status != "PROCESSED":
            self.msg_log.append({"user": user, "dialog": dialog or "seed", "t": ts, "role": "user", "text": fact})
            return None
        sub = TOPIC_SECTION.get(topic or "", "personal")
        if sub == "current":
            section = "current"
        elif sub == "personal":
            section = "personal"
        else:
            org = self._current_org(user)
            section = f"org:{org['key']}/{'assets' if sub == 'bank' else sub}"
        line = self._new_line(user, section, fact, ts)
        if section == "current":
            line["until"] = (_date(ts) + timedelta(days=1)).date().isoformat()
        return line["id"]

    def profile_lines(self, user):
        """Строки профиля. Архив считается сохранённым, только если агент может его найти (search_memory, шаг 6)."""
        p = self._prof(user)
        out = [{"text": x["text"], "section": x["section"], "was": x["was"], "status": "банк" if x["bank"] else "профиль"}
               for x in p["lines"]]
        if self.fixes.search_memory:
            out += [{"text": x["text"], "section": x["section"], "was": x["was"], "status": "архив"} for x in p["archive"]]
        return out

    def memory_table(self, user):
        p = self._prof(user)
        rows = [{"Раздел": x["section"], "id": x["id"], "Строка": x["text"], "Дата": f"{_date(x['date']):%d.%m.%Y}",
                 "До": x["until"] or "", "Было": x["was"] or "", "Банк": "да" if x["bank"] else ""} for x in p["lines"]]
        rows += [{"Раздел": "архив", "id": x["id"], "Строка": x["text"], "Дата": f"{_date(x['date']):%d.%m.%Y}",
                  "До": x["until"] or "", "Было": x["was"] or "", "Банк": ""} for x in p["archive"]]
        return rows

    # ======================= запись: на каждой реплике ничего не происходит
    def collect(self, user, dialog, history):
        return []

    def handler(self, user):
        return {"status": "deferred", "reason": "разбор — после паузы в диалоге"}

    def end_session(self, user, dialog):
        st = self.sessions.get(dialog)
        if st and st["open"]:
            st["open"] = False
            self.session_end(user, dialog)

    def session_end(self, user, dialog):
        """Пауза в диалоге: один разбор новой части диалога (memory_job), потом — сон, если профиль переполнен."""
        since = self.processed_until.get(dialog, float("-inf"))
        msgs = [m for m in self.msg_log if m["user"] == user and m["dialog"] == dialog and m["t"] > since]
        if not any(m["role"] == "user" for m in msgs):
            return
        org = self._current_org(user, dialog)
        job_t = min(self.clock.t, msgs[-1]["t"] + SESSION_GAP_MIN * 60)  # задача срабатывает через паузу после реплики
        def line(m):
            who = pp.L["role_user"] if m["role"] == "user" else pp.L["role_assistant"]
            done = self.commanded.get((user, m["dialog"], m["t"])) if m["role"] == "user" else None
            tail = f"  ⟦{pp.L['done_mark'].format(done=done)}⟧" if done else ""
            return f"[{_wd(_date(m['t']))} {_date(m['t']):%d.%m.%Y %H:%M}] {who}: {m['text']}{tail}"
        lines = "\n".join(line(m) for m in msgs)
        prompt = pp.SESSION_PROMPT.format(today=self._today(job_t), org=self._org_label(org), profile=self.edit_view(user),
                                          dialog=lines, sections=pp.SECTIONS_HELP, ops=pp.OPS_HELP,
                                          calendar=calendar([m["t"] for m in msgs]))
        try:
            if self.fail_memory_write:
                raise RuntimeError("сбой записи в память (эмуляция)")
            ops = (self.llm.json_task("session", prompt) or {}).get("ops") or []
        except Exception as e:  # noqa: BLE001 — задача остаётся в очереди, разбор повторится на следующей паузе
            self.log(f"[SESSION] диалог {dialog}: разбор упал ({type(e).__name__}: {e}), задача остаётся в очереди")
            return
        self.log(f"[SESSION] диалог {dialog}: разбор {len(msgs)} реплик → правок предложено {len(ops)}")
        self.apply_ops(user, ops, kind="session", msgs=msgs, org=org, at=job_t)
        self.processed_until[dialog] = msgs[-1]["t"]
        self._after_write(user, job_t)

    def _today(self, t=None):
        d = _date(self.clock.t if t is None else t)
        return f"{d:%Y-%m-%d} ({pp.L['weekdays'][d.weekday()]})"

    @staticmethod
    def _org_label(org):
        return pp.L["org_label"].format(name=org["name"], key=org["key"])

    # ======================= проверки кодом и применение правок
    def _section(self, user, raw, org):
        s = str(raw or "").strip()
        if s in ("personal", "prefs", "current"):
            return s
        if s.startswith("org:") and "/" in s:
            key, sub = s[4:].split("/", 1)
            if not self._org(user, key):
                return None
            return f"org:{key}/{sub}" if sub in dict(ORG_SECTIONS) else None
        sub = s.split("/")[-1]
        return f"org:{org['key']}/{sub}" if sub in dict(ORG_SECTIONS) else None

    def apply_ops(self, user, ops, *, kind, msgs=None, org=None, at=None):
        """Каждая правка проверяется кодом; принятые и отклонённые пишутся в журнал. Возвращает принятые."""
        p = self._prof(user)
        msgs = msgs or []
        user_texts = [m["text"] for m in msgs if m["role"] == "user"]
        accepted = []
        if not isinstance(ops, list):
            ops = []
        for i, op in enumerate(ops):
            if not isinstance(op, dict):
                continue
            op = dict(op)
            for k in ("text", "old"):
                if op.get(k) is not None:
                    op[k] = _clean_text(op[k])
            for k in ("id", "overrides"):
                if op.get(k):
                    op[k] = _clean_id(op[k])
            if isinstance(op.get("ids"), list):
                op["ids"] = [_clean_id(x) for x in op["ids"]]
            entry = {"kind": kind, "op": op.get("op"), "id": op.get("id"), "text": op.get("text"),
                     "evidence": op.get("evidence"), "verdict": "принята", "reason": ""}
            snapshot = json.dumps(p, ensure_ascii=False)
            try:
                limit = MAX_SLEEP_OPS if kind == "sleep" else MAX_OPS
                if i >= limit:
                    raise _Reject(f"больше {limit} правок за раз")
                note = self._apply_one(user, op, kind, msgs, user_texts, org, self.clock.t if at is None else at)
                if not self._parses(user):
                    raise _Reject("документ после правки не разбирается")
                entry["reason"] = note or ""
                accepted.append(op)
                self.log(f"[PROFILE] ✓ {op.get('op')} {note}")
            except _Reject as e:
                self.profiles[user] = p = json.loads(snapshot)
                entry["verdict"], entry["reason"] = "отклонена", str(e)
                self.log(f"[PROFILE] ✗ {op.get('op')} «{(op.get('text') or op.get('old') or '')[:60]}» — {e}")
            self.journal.append(entry)
        if accepted:
            p["version"] += 1
        return accepted

    def _check_evidence(self, op, kind, msgs, user_texts):
        if kind == "sleep":
            return
        ev = op.get("evidence") or ""
        if op.get("source") == "assistant_confirmed" and not _found(ev, user_texts):
            for i, m in enumerate(msgs):
                if m["role"] == "assistant" and _found(ev, [m["text"]]):
                    nxt = next((x for x in msgs[i + 1:] if x["role"] == "user"), None)
                    if nxt and CONFIRM.search(_norm(nxt["text"])):
                        return
            raise _Reject("цитата ассистента без подтверждения пользователя")
        if not _found(ev, user_texts):
            raise _Reject("цитаты нет в репликах пользователя")

    def _org_guard(self, user, section, org, kind, user_texts):
        """Факт ложится в организацию диалога; в другую — только если она названа в репликах (решение раздела 4.2)."""
        if kind == "sleep" or not org or not str(section).startswith("org:"):
            return
        key = section[4:].split("/", 1)[0]
        if key != org["key"]:
            other = self._org(user, key)
            if other and not _org_mentioned(other, user_texts):
                raise _Reject(f"правка организации «{other['name']}» из диалога «{org['name']}», а она не названа")

    def _apply_one(self, user, op, kind, msgs, user_texts, org, now):
        name = op.get("op")
        if name in ("replace", "remove") and self._line(user, op.get("id")):
            line0 = self._line(user, op.get("id"))
            try:
                self._org_guard(user, line0["section"], org, kind, user_texts)
            except _Reject:
                if name != "replace" or not line0["section"].startswith("org:"):
                    raise
                sub = line0["section"].split("/", 1)[1]
                op = {"op": "add", "section": f"org:{org['key']}/{'assets' if sub == 'bank' else sub}",
                      "text": op.get("text"), "evidence": op.get("evidence"), "source": op.get("source")}
                return ("(правка строки другой организации → add в организацию диалога) "
                        + self._apply_one(user, op, kind, msgs, user_texts, org, now))
        if kind == "sleep" and name not in ("merge", "archive", "replace"):
            raise _Reject(f"во сне правка {name} не разрешена")
        if kind != "sleep" and name in ("merge", "archive"):
            raise _Reject(f"правка {name} разрешена только во сне")
        if name in ("add", "replace", "merge"):
            text = (op.get("text") or "").strip()
            if not text:
                raise _Reject("пустой текст")
            what = _pdn(text)
            if what:
                raise _Reject(f"персональные данные ({what})")
        if name == "add":
            section = self._section(user, op.get("section"), org)
            if not section:
                raise _Reject(f"нет такого раздела: {op.get('section')}")
            if section.endswith("/bank"):
                raise _Reject("реквизиты банка правит только код")
            self._org_guard(user, section, org, kind, user_texts)
            self._check_evidence(op, kind, msgs, user_texts)
            if any(_norm(x["text"]) == _norm(text) for x in self._prof(user)["lines"]):
                raise _Reject("дубль существующей строки")
            extra = {}
            if section == "current":
                extra["until"] = self._until(op.get("until"), now)
            if op.get("overrides"):
                tgt = self._line(user, op["overrides"])
                if tgt and not tgt["bank"]:  # модель имела в виду замену этой строки
                    op = {"op": "replace", "id": tgt["id"], "old": tgt["text"], "text": text,
                          "evidence": op.get("evidence"), "source": op.get("source")}
                    return "(overrides на обычную строку → replace) " + \
                        self._apply_one(user, op, kind, msgs, user_texts, org, now)
                if not tgt:
                    raise _Reject("overrides указывает на несуществующую строку")
                extra["overrides"] = tgt["id"]
            line = self._new_line(user, section, text, now, **extra)
            return f"{line['id']} → {section}: «{text}»"
        tgt = self._line(user, op.get("id")) if name in ("replace", "remove") and kind != "sleep" else None
        if tgt is not None and tgt["bank"]:
            # решение 5: слова пользователя важнее банка. Строку банка код не меняет, а рядом кладёт строку
            # «со слов пользователя», которая её перекрывает; значение банка остаётся видно с пометкой.
            self._check_evidence(op, kind, msgs, user_texts)
            text = (op.get("text") or "").strip() if name == "replace" else f"{tgt['text']}{pp.L['stale']}"
            if not text:
                raise _Reject("пустой текст")
            if _label(text) and _label(text) != _label(tgt["text"]):
                # «Директор: …» поверх «Название: …» — это не другой реквизит, а новый факт об организации
                key = tgt["section"][4:].split("/", 1)[0] if tgt["section"].startswith("org:") else org["key"]
                op = {"op": "add", "section": f"org:{key}/{_guess_sub(text)}", "text": text,
                      "evidence": op.get("evidence"), "source": op.get("source")}
                return "(правка строки банка про другое → add) " + self._apply_one(user, op, kind, msgs, user_texts, org, now)
            what = _pdn(text)
            if what:
                raise _Reject(f"персональные данные ({what})")
            if any(x["overrides"] == tgt["id"] and _norm(x["text"]) == _norm(text) for x in self._prof(user)["lines"]):
                raise _Reject("дубль существующей строки")
            line = self._new_line(user, tgt["section"], text, now, overrides=tgt["id"])
            return f"({name} строки банка {tgt['id']} → перекрытие) {line['id']}: «{text}»"
        if name == "replace" and kind != "sleep" and self._line(user, op.get("id")) is None:
            # модель назвала изменением то, чего в профиле нет: факт не теряем, кладём как новую строку.
            # Раздел — из section, из похожего на раздел id («org:main/people:staff») или текущая организация.
            raw_id = str(op.get("id") or "")
            guess = re.match(r"(org:[^/:|]+/[a-z]+|personal|prefs|current)", raw_id)
            section = (self._section(user, op.get("section"), org) or
                       (self._section(user, guess.group(1), org) if guess else None) or
                       f"org:{org['key']}/{_guess_sub(op.get('text'))}")
            op = dict(op, op="add", section=section)
            note = self._apply_one(user, op, kind, msgs, user_texts, org, now)
            return f"(replace по несуществующему id {op.get('id')} → add) {note}"
        if name == "replace":
            line = self._target(user, op, need_old=kind != "sleep")
            if kind != "sleep":
                self._check_evidence(op, kind, msgs, user_texts)
            if kind == "sleep" and _numbers(line["text"]) - _numbers(text):
                raise _Reject("при сокращении потерялись числа")
            if kind != "sleep":
                line["was"], line["was_date"] = line["text"], now
                line["date"] = now
            old = line["text"]
            line["text"] = text
            if line["section"] == "current" and op.get("until"):
                line["until"] = self._until(op.get("until"), now)
            return f"{line['id']}: «{old}» → «{text}»"
        if name == "remove":
            line = self._target(user, op)
            ev = _norm(op.get("evidence"))
            if op.get("reason") != "forget" or not any(mk in ev for mk in FORGET_MARKERS):
                raise _Reject("удалять можно только по просьбе забыть, с цитатой")
            if not _points_to(op.get("evidence"), line["text"]):
                raise _Reject("цитата «забудь» не указывает на эту строку")
            self._check_evidence(op, kind, msgs, user_texts)
            self._prof(user)["lines"].remove(line)
            return f"{line['id']}: удалена «{line['text']}»"
        if name == "add_org":
            title = (op.get("name") or "").strip()
            if not title:
                raise _Reject("нет названия организации")
            self._check_evidence(op, kind, msgs, user_texts)
            p = self._prof(user)
            if any(_norm(o["name"]) == _norm(title) for o in p["orgs"]):
                raise _Reject("такая организация уже есть")
            key = f"extra{sum(o['key'].startswith('extra') for o in p['orgs']) + 1}"
            p["orgs"].append({"key": key, "name": title, "current": False})
            return f"новая организация {key}: «{title}»"
        if name == "merge":
            ids = [str(x) for x in (op.get("ids") or [])]
            src = [self._line(user, i) for i in ids]
            if len(ids) < 2 or any(x is None for x in src):
                raise _Reject("merge: нужны ≥ 2 существующие строки")
            if any(x["bank"] for x in src):
                raise _Reject("строки банка не сливаются")
            if len({x["section"] for x in src}) > 1:
                raise _Reject("merge: строки из разных разделов")
            lost = set().union(*(_numbers(x["text"]) for x in src)) - _numbers(text)
            if lost:
                raise _Reject(f"merge теряет числа {sorted(lost)[:5]}")
            section = self._section(user, op.get("section"), org) or src[0]["section"]
            for x in src:
                self._prof(user)["lines"].remove(x)
            until = max((x["until"] for x in src if x["until"]), default=None)
            line = self._new_line(user, section, text, max(x["date"] for x in src), until=until)
            return f"{', '.join(ids)} → {line['id']}: «{text}»"
        if name == "archive":
            line = self._target(user, op, need_old=False)
            self._prof(user)["lines"].remove(line)
            self._prof(user)["archive"].append(line)
            return f"{line['id']} в архив: «{line['text']}» ({op.get('reason') or 'без причины'})"
        raise _Reject(f"неизвестная правка {name}")

    def _target(self, user, op, need_old=True):
        line = self._line(user, op.get("id"))
        if line is None:
            raise _Reject(f"нет строки с id {op.get('id')}")
        if line["bank"]:
            raise _Reject("строки банка правит только код")
        if need_old and not op.get("old"):
            raise _Reject("нет old — нечем проверить, та ли строка")
        if need_old and not _same(op["old"], line["text"]):
            raise _Reject(f"old не совпадает со строкой {line['id']}")
        return line

    def _until(self, raw, now):
        d = _parse_until(raw)
        if d is None:
            raise _Reject("у строки «Текущих дел» нет until")
        today = _date(now).date()
        if d < today - timedelta(days=1) or d > today + timedelta(days=CURRENT_MAX_DAYS):
            raise _Reject(f"until {d} вне [сегодня; +{CURRENT_MAX_DAYS} дней]")
        return d.isoformat()

    # ======================= шаг 5: глубокий сон
    def _after_write(self, user, at=None):
        if not self.fixes.deep_sleep:
            return
        at = self.clock.t if at is None else at
        p = self._prof(user)
        today = _date(at).date()
        expired = [x for x in p["lines"] if x["until"] and _parse_until(x["until"]) and _parse_until(x["until"]) < today]
        for x in expired:  # аналог max_unused_days: просроченные «Текущие дела» уходят в архив кодом
            p["lines"].remove(x)
            p["archive"].append(x)
        if expired:
            self.log(f"[SLEEP] в архив по сроку: {len(expired)}")
        for _ in range(SLEEP_ROUNDS):
            full, _ = self.read_view(user, None, budget=10 ** 9)
            tokens = approx_tokens(len(full))
            if tokens <= READ_BUDGET_TOKENS:
                return
            prompt = pp.SLEEP_PROMPT.format(tokens=tokens, budget=READ_BUDGET_TOKENS, today=self._today(at),
                                            profile=self.edit_view(user))
            try:
                ops = (self.llm.json_task("sleep", prompt) or {}).get("ops") or []
            except Exception as e:  # noqa: BLE001
                self.log(f"[SLEEP] сон упал ({type(e).__name__}: {e})")
                return
            before = len(self._prof(user)["lines"])
            accepted = self.apply_ops(user, ops, kind="sleep", org=self._current_org(user), at=at)
            after, _ = self.read_view(user, None, budget=10 ** 9)
            self.log(f"[SLEEP] профиль ≈{tokens} → ≈{approx_tokens(len(after))} токенов, строк {before} → "
                     f"{len(self._prof(user)['lines'])}, в архиве {len(self._prof(user)['archive'])}")
            if not accepted:
                return

    # ======================= шаг 5: remember / forget
    def agent_functions(self):
        out = [pp.REMEMBER_FUNCTION, pp.FORGET_FUNCTION] if self.fixes.commands else []
        if self.fixes.search_memory:
            out.append(pp.SEARCH_FUNCTION)
        return out or None

    def search(self, user, query):
        """search_memory: BM25 по архиву и строкам, не влезшим в вид для чтения."""
        if self.fail_memory_read:
            self.log(f"[SEARCH] «{query}» — сбой чтения памяти (эмуляция)")
            return {pp.L["error"]: pp.L["mem_read_down"]}
        p = self._prof(user)
        hidden = self.hidden_ids.get(user, set())
        pool =[(x, pp.L["archive"]) for x in p["archive"]] + [(x, pp.L["profile"]) for x in p["lines"] if x["id"] in hidden]
        hits = _bm25(query, [x["text"] for x, _ in pool])[:SEARCH_TOP]
        self.log(f"[SEARCH] «{query}» → {len(hits)} из {len(pool)}")
        return {pp.L["found"]: [f"{pool[i][0]['text']} ({_date(pool[i][0]['date']):%d.%m.%Y}; {pool[i][1]})" for i in hits]}

    def _answer_no_function(self, user, dialog, messages, turn):
        functions = self.agent_functions()
        resp = self.llm.chat(messages, functions)
        n = 0
        message = messages[-1]["content"]
        while functions and _is_fc(resp) and n < MAX_COMMAND_CALLS:
            n += 1
            fc = resp["function_call"]
            args = fc.get("arguments") or {}
            if fc.get("name") == "search_memory":
                result = self.search(user, args.get("query") or args.get("text") or message)
            else:
                result = self._command(user, dialog, fc.get("name"), args.get("text") or "", message)
            turn.setdefault("commands", []).append({"name": fc.get("name"), "text": args.get("text") or args.get("query"),
                                                    "result": result})
            fc_msg = {"role": "assistant", "content": "", "function_call": fc}
            if resp.get("functions_state_id"):
                fc_msg["functions_state_id"] = resp["functions_state_id"]
            messages.append(fc_msg)
            messages.append({"role": "function", "name": fc.get("name"), "content": json.dumps(result, ensure_ascii=False)})
            resp = self.llm.chat(messages, functions if n < MAX_COMMAND_CALLS else None)
        turn["text"] = (resp.get("content") or "").strip()
        turn["empty"] = not turn["text"]
        turn["unfinished_function_call"] = _is_fc(resp)
        if turn["empty"]:
            self.log("[AGENT] пользователь получил ПУСТОЙ ответ")
        return turn

    def _command(self, user, dialog, name, text, message):
        """Срочная правка по remember / forget: тот же формат правок и те же проверки, цитата — текущая реплика."""
        if name not in ("remember", "forget"):
            return {pp.L["error"]: pp.L["unknown_fn"].format(name=name)}
        if not EXPLICIT[name].search(_norm(message)):
            self.log(f"[COMMAND] {name} без явной просьбы — ничего не делаем, сохранит разбор сессии")
            return {pp.L["saved"]: [], pp.L["removed"]: [], pp.L["note"]: pp.L["no_explicit"]}
        org = self._current_org(user, dialog)
        what = pp.L["what_remember"] if name == "remember" else pp.L["what_forget"]
        prompt = pp.COMMAND_PROMPT.format(what=what, today=self._today(), org=self._org_label(org),
                                          profile=self.edit_view(user), message=message, text=text,
                                          sections=pp.SECTIONS_HELP, ops=pp.OPS_HELP, calendar=calendar([self.clock.t]))
        try:
            if self.fail_memory_write:
                raise RuntimeError("сбой записи в память (эмуляция)")
            ops = (self.llm.json_task("command", prompt) or {}).get("ops") or []
        except Exception as e:  # noqa: BLE001
            self.log(f"[COMMAND] {name} упал ({type(e).__name__}: {e})")
            return {pp.L["saved"]: [], pp.L["removed"]: [], pp.L["error"]: pp.L["mem_down"]}
        if name == "remember":
            ops = [o for o in ops if isinstance(o, dict) and o.get("op") in ("add", "replace", "add_org")]
        else:
            ops = [o for o in ops if isinstance(o, dict) and o.get("op") == "remove"]
        n_before = len(self.journal)
        msgs = [{"role": "user", "text": message, "t": self.clock.t}]
        accepted = self.apply_ops(user, ops, kind="command", msgs=msgs, org=org)
        rejected = [f"{e['text'] or e['id']}: {e['reason']}" for e in self.journal[n_before:] if e["verdict"] != "принята"]
        self.log(f"[COMMAND] {name}({text!r}) → принято {len(accepted)}, отклонено {len(rejected)}")
        if accepted:
            done = "; ".join(f"{o.get('op')} «{o.get('text') or o.get('old') or o.get('id')}»" for o in accepted)
            key = (user, dialog, self.clock.t)
            self.commanded[key] = (self.commanded.get(key, "") + "; " + done).strip("; ")
        self._after_write(user)
        return {pp.L["saved"]: [o.get("text") for o in accepted if o.get("op") != "remove"],
                pp.L["removed"]: [o.get("old") or o.get("id") for o in accepted if o.get("op") == "remove"],
                pp.L["rejected"]: rejected}


class _Reject(Exception):
    pass
