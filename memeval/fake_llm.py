"""Офлайн-«модель» для холостого прогона: ничего не вызывает по сети, отвечает по простым правилам.

Нужна, чтобы проверить набор и всю обвязку (загрузка истории, чтение памяти, судья, отчёт)
до того, как тратить токены: `python run.py public --data ... --provider fake`.
Угадывание темы при чтении специально сделано «как у модели» — по хэшу, то есть иногда мимо.
"""
import hashlib
import re

from . import prompts

_WORD = re.compile(r"[\w']+", re.UNICODE)
_STOP = {"the", "a", "an", "and", "or", "is", "are", "was", "were", "i", "you", "he", "she", "it", "we", "they",
         "my", "your", "to", "of", "in", "on", "at", "for", "with", "do", "did", "does", "what", "who", "when",
         "where", "how", "why", "that", "this", "have", "has", "had", "not", "be", "been", "about"}


def _hash_pick(text, options):
    h = int(hashlib.md5(text.encode("utf-8")).hexdigest(), 16)
    return options[h % len(options)]


class FakeLLM:
    """Интерфейс как у OpenRouterLLM, но без сети."""

    def __init__(self, model=None, **_):
        self.model = model or "fake-offline"
        self.served_model = self.model
        self.usage = {"calls": 0, "by_kind": {}}

    def _count(self, kind):
        self.usage["calls"] += 1
        self.usage["by_kind"][kind] = self.usage["by_kind"].get(kind, 0) + 1

    def json_task(self, kind, prompt):
        self._count(kind)
        if kind in ("session", "command", "sleep"):
            return {"ops": _profile_ops(kind, prompt)}
        if kind == "collect":
            facts = []
            for m in re.finditer(r'"messageText"\s*:\s*"(.*?)"\s*,\s*"role"\s*:\s*"user"', prompt, re.S):
                t = m.group(1).replace("\\n", " ").strip()
                if len(t) > 15:
                    facts.append(t[:200])
            return {"facts": facts}
        ids = re.findall(r'"index"\s*:\s*"([0-9a-f-]{36})"', prompt)
        if kind == "annotate":
            return {"decisions": [{"index": i, "action": "KEEP", "reason": "none", "scope": "global"} for i in ids]}
        codes = re.findall(r"^- ([a-z_]+) \(", prompt, re.M)
        return {"topics": [{"index": i, "topic": _hash_pick(i, codes)} for i in ids]}

    def chat(self, messages, functions=None):
        self._count("chat")
        question = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        facts = [m["content"] for m in messages if m.get("role") == "function"]
        names = [f.get("name") for f in functions or []]
        if "remember" in names and not facts:  # шаг 5: явная просьба — вызываем remember / forget
            ql = question.lower()
            name = "forget" if "забуд" in ql else "remember" if ("запомни" in ql or "сохрани" in ql) else None
            if name:
                return {"content": "", "finish_reason": "function_call",
                        "function_call": {"name": name, "arguments": {"text": question}}}
        if messages and messages[0].get("role") == "system":  # память в системном промпте (шаги 2–5)
            facts += [ln[2:] for ln in messages[0]["content"].splitlines() if ln.startswith("- ")]
        if functions and not facts and prompts.FUNCTION_NAME in names:  # первый ход: «угадываем» тему по хэшу вопроса
            codes = functions[0]["parameters"]["properties"]["topics"]["items"]["enum"]
            picked = [_hash_pick(question, codes)]
            return {"content": "", "finish_reason": "function_call",
                    "function_call": {"name": prompts.FUNCTION_NAME, "arguments": {"topics": picked}}}
        words = [w.lower() for w in _WORD.findall(question) if w.lower() not in _STOP and len(w) > 2]
        hits = [f for f in facts if any(w in f.lower() for w in words)]
        if not hits and "search_memory" in names and not any(m.get("role") == "function" for m in messages):
            return {"content": "", "finish_reason": "function_call",
                    "function_call": {"name": "search_memory", "arguments": {"query": question}}}
        if not hits:
            return {"content": "I don't have that information in my memory.", "finish_reason": "stop",
                    "function_call": None}
        return {"content": " ".join(hits)[:600], "finish_reason": "stop", "function_call": None}

    def plain(self, prompt, kind="judge", max_tokens=16):
        """Судья без модели: YES, если значимые слова эталона встречаются в ответе."""
        self._count(kind)
        if kind.startswith("judge_"):  # судья живого бенча (русские промпты)
            from .live import ABSTAIN_MARKERS
            m = re.search(r"ОТВЕТ АССИСТЕНТА:\s*«(.*?)»\s*\n", prompt, re.S)
            ans = (m.group(1) if m else "").lower()
            dunno = any(x in ans for x in ABSTAIN_MARKERS)
            if kind == "judge_none":
                return "NO" if dunno else "YES"
            if kind == "judge_abstain":
                return "YES" if dunno else "NO"
            return "NO" if dunno or not ans else "YES"
        gold = re.search(r"Reference answer: (.*?)\nAssistant answer:", prompt, re.S)
        answer = re.search(r"Assistant answer: (.*)$", prompt, re.S)
        ans = (answer.group(1) if answer else "").lower()
        if not gold:
            from .public_bench import ABSTAIN_MARKERS
            return "YES" if any(m in ans for m in ABSTAIN_MARKERS) else "NO"
        need = [w.lower() for w in _WORD.findall(gold.group(1)) if w.lower() not in _STOP and len(w) > 2]
        if not need:
            return "NO"
        return "YES" if sum(w in ans for w in need) / len(need) >= 0.6 else "NO"

    def ping(self):
        return ["офлайн-модель: OK (ничего не вызывается по сети)"]


# ------------------------------------------------------------------ целевая схема: разбор сессии, команды, сон

_SECTION_WORDS = [("current", ("сегодня", "завтра", "на этой неделе", "в пятницу", "встреча", "до конца месяца")),
                  ("org/people", ("директор", "бухгалтер", "сотрудник", "штат", "человек", "кадр", "курьер")),
                  ("org/finance", ("усн", "налог", "выручк", "кредит", "лизинг", "счет", "счёт", "осно", "патент")),
                  ("org/partners", ("поставщ", "клиент", "договор", "контрагент", "сеть", "отсрочк")),
                  ("org/assets", ("склад", "магазин", "офис", "прода", "кофейн", "погрузчик", "аренд", "кофемашин"))]


def _profile_lines(prompt):
    """Строки профиля из вида для правки: (id, текст, раздел)."""
    out, section = [], None
    for ln in prompt.splitlines():
        m = re.search(r"\((?:раздел|section) (org:[^)]+)\)", ln)
        if ln.startswith("## "):
            section = m.group(1) if m else {"Личность": "personal", "Предпочтения": "prefs", "Текущие дела": "current",
                                             "Personal": "personal", "Preferences": "prefs",
                                             "Current affairs": "current"}.get(ln[3:].strip())
        m = re.match(r"- (.*) ⟦([^|⟧]+)[^⟧]*⟧\s*$", ln)
        if m:
            out.append((m.group(2), m.group(1), section, "|банк" in ln or "|bank" in ln))
    return out


def _words(text):
    return {w.lower()[:5] for w in _WORD.findall(text) if len(w) > 3}  # грубая основа слова


def _best_line(lines, text):
    tw = _words(text) - {"забуд", "забыт", "тепер"}
    best = max(((len(_words(t) & tw), i, t) for i, t, _, bank in lines if not bank), default=(0, None, None))
    return best if best[0] else (0, None, None)


def _today(prompt):
    m = re.search(r"(?:Сегодня|Today): (\d{4}-\d{2}-\d{2})", prompt)
    return m.group(1) if m else "2026-01-01"


def _op_for(text, lines, today):
    low = text.lower()
    if "забуд" in low:
        _, lid, old = _best_line(lines, text)
        return {"op": "remove", "id": lid, "old": old, "reason": "forget", "evidence": text} if lid else None
    if any(w in low for w in ("теперь", "сменил", "переехал", "переименовал")):
        _, lid, old = _best_line(lines, text)
        if lid:
            return {"op": "replace", "id": lid, "old": old, "text": text, "evidence": text}
    section = next((sec for sec, keys in _SECTION_WORDS if any(k in low for k in keys)), "personal")
    op = {"op": "add", "section": section, "text": text, "evidence": text}
    if section == "current":
        from datetime import date, timedelta
        op["until"] = (date.fromisoformat(today) + timedelta(days=7)).isoformat()
    return op


def _profile_ops(kind, prompt):
    lines = _profile_lines(prompt)
    today = _today(prompt)
    if kind == "sleep":  # сливаем старшую половину строк в разделах, где их ≥ 4
        by_sec = {}
        for lid, text, sec, bank in lines:
            if not bank:
                by_sec.setdefault(sec, []).append((lid, text))
        ops = []
        for sec, items in by_sec.items():
            if len(items) >= 4:
                half = items[:len(items) // 2]
                ops.append({"op": "merge", "ids": [i for i, _ in half], "section": sec,
                            "text": "; ".join(t for _, t in half)})
        return ops
    if kind == "command":
        m = re.search(r"(?:РЕПЛИКА ПОЛЬЗОВАТЕЛЯ|USER MESSAGE): «(.*)»\n", prompt)
        text = m.group(1) if m else ""
        op = _op_for(text, lines, today)
        return [op] if op else []
    part = re.split(r"НОВАЯ ЧАСТЬ ДИАЛОГА:|NEW PART OF THE CONVERSATION:", prompt, maxsplit=1)[-1]
    part = re.split(r"Разделы профиля|Profile sections", part, maxsplit=1)[0]
    ops = []
    for m in re.finditer(r"^\[[^\]]+\] (?:Пользователь|User): (.*)$", part, re.M):
        text = m.group(1).strip()
        if len(text) <= 15 or text.endswith("?"):
            continue
        op = _op_for(text, lines, today)
        if op:
            ops.append(op)
    return ops
