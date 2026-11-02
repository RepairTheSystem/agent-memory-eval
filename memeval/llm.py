"""Provider-independent model helpers and a deterministic scripted model."""
import json
import re
from . import prompts


def text(content):
    """Ответ модели обычным текстом."""
    return {"content": content, "finish_reason": "stop", "function_call": None}


def fcall(topics):
    """Ответ модели: «вызови функцию памяти с такими топиками»."""
    return {"content": "", "finish_reason": "function_call",
            "function_call": {"name": prompts.FUNCTION_NAME, "arguments": {"topics": list(topics)}}}


def parse_json(raw):
    """Достаёт JSON из ответа модели: снимает ```json-обёртку, берёт первый объект/массив."""
    if raw is None:
        raise ValueError("пустой ответ модели")
    s = raw.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (s.find("{"), s.find("[")) if i >= 0]
    if not starts:
        raise ValueError(f"в ответе модели нет JSON: {raw[:200]}")
    start = min(starts)
    closer = "}" if s[start] == "{" else "]"
    end = s.rfind(closer)
    return json.loads(s[start:end + 1])


def schema_hint(kind):
    """Схема ответа текстом (в проде её навязывает LangChain4j)."""
    from . import prompts_profile as pp
    return {"collect": prompts.COLLECT_SCHEMA_HINT, "annotate": prompts.MEMORY_SCHEMA_HINT,
            "classify": prompts.TOPIC_SCHEMA_HINT, "session": pp.SESSION_SCHEMA_HINT, "sleep": pp.SESSION_SCHEMA_HINT,
            "command": pp.SESSION_SCHEMA_HINT}[kind]


def normalize(kind, data):
    """Приводит ответ к форме Java-DTO: CollectFact{facts}, Facts{decisions}, TopicResponse{topics}."""
    key = {"collect": "facts", "annotate": "decisions", "classify": "topics",
           "session": "ops", "sleep": "ops", "command": "ops"}[kind]
    if isinstance(data, list):
        return {key: data}
    return data


class ScriptedLLM:
    """Отдаёт заранее заготовленные ответы по очереди для каждого типа вызова.

    Элемент очереди — готовый ответ или функция (вход -> ответ): так мок может «честно» отвечать
    по тому, что ему передали (например, по фактам, которые вернула функция памяти).
    """

    def __init__(self, **queues):
        self.queues = {k: list(v) for k, v in queues.items()}
        self.calls = []

    def _next(self, kind, payload):
        q = self.queues.get(kind)
        if not q:
            raise AssertionError(f"мок-модель: нет заготовленного ответа для вызова '{kind}'")
        item = q.pop(0)
        out = item(payload) if callable(item) else item
        self.calls.append({"kind": kind, "input": payload, "output": out})
        return out

    def chat(self, messages, functions=None):
        return self._next("chat", {"messages": messages, "functions": functions})

    def json_task(self, kind, prompt):
        return normalize(kind, self._next(kind, prompt))
