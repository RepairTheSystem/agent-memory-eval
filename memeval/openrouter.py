"""Клиент OpenRouter (OpenAI-совместимый) с тем же интерфейсом, что GigaChatLLM.

Нужен для прогонов на публичных наборах: один токен — любая модель с поддержкой вызова функций.

Переменные окружения (можно вписать в openrouter.env рядом с run.py):
  OPENROUTER_API_KEY     — токен (обязательно)
  OPENROUTER_MODEL       — модель агента, например openai/gpt-4o-mini (по умолчанию)
  OPENROUTER_JUDGE_MODEL — модель-судья для сверки ответа с эталоном (по умолчанию = OPENROUTER_MODEL)
  OPENROUTER_BASE_URL    — по умолчанию https://openrouter.ai/api/v1
  OPENROUTER_TEMPERATURE — по умолчанию 0
  OPENROUTER_MAX_TOKENS  — лимит длины ответа агента (по умолчанию 800)
  OPENROUTER_MAX_TOKENS_JSON — лимит для служебных вызовов сбора/разбора (по умолчанию 8000)
  OPENROUTER_RETRIES     — число повторов при 429/5xx (по умолчанию 5)
  OPENROUTER_TIMEOUT     — таймаут одного запроса, секунды (по умолчанию 60)
  OPENROUTER_QUIET       — 1, чтобы не печатать строки о повторах и первом ответе
  OPENROUTER_REASONING   — off (по умолчанию) | low | medium | high | default. У моделей с рассуждениями
                           лимит токенов уходит на размышления, и content приходит пустым, поэтому off
  OPENROUTER_JSON_MODE   — 0, чтобы не просить response_format=json_object

Интерфейс (как у интерфейса модели):
  chat(messages, functions) -> {"content", "finish_reason", "function_call"}
  json_task(kind, prompt)   -> dict
"""
import http.client
import json
import os
import threading
import time
import urllib.error
import urllib.request

from .llm import normalize, parse_json, schema_hint


class OpenRouterLLM:
    def __init__(self, model=None, api_key=None, label="agent"):
        env = os.environ
        self.api_key = api_key or env.get("OPENROUTER_API_KEY")
        if not self.api_key:
            raise RuntimeError("Не задан OPENROUTER_API_KEY: впишите его в openrouter.env рядом с run.py")
        self.model = model or env.get("OPENROUTER_MODEL", "openai/gpt-4o-mini")
        self.base = env.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
        self.chat_url = self.base + "/chat/completions"
        self.temperature = float(env.get("OPENROUTER_TEMPERATURE", "0"))
        self.max_tokens = int(env.get("OPENROUTER_MAX_TOKENS", "800"))
        self.max_tokens_json = int(env.get("OPENROUTER_MAX_TOKENS_JSON", "8000"))
        self.retries = int(env.get("OPENROUTER_RETRIES", "5"))
        self.timeout = float(env.get("OPENROUTER_TIMEOUT", "60"))
        self.verbose = env.get("OPENROUTER_QUIET", "0").lower() in ("0", "false", "no")
        self._first_done = False
        self.json_mode = env.get("OPENROUTER_JSON_MODE", "1").lower() not in ("0", "false", "no")
        self.reasoning = env.get("OPENROUTER_REASONING", "off").lower()
        self.label = label
        self.served_model = None
        self.calls = []                      # для отладки: пишется только если keep_calls=True
        self.keep_calls = False
        self._lock = threading.Lock()
        self.usage = {"calls": 0, "failed": 0, "prompt_tokens": 0, "completion_tokens": 0, "retries": 0,
                      "by_kind": {}}

    # ---------------------------------------------------------------- транспорт
    def _say(self, msg):
        if self.verbose:
            with self._lock:
                print(f"    [модель] {msg}", flush=True)

    def _post(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
                   "X-Title": "memory-eval"}
        req = urllib.request.Request(self.chat_url, data=body, headers=headers, method="POST")
        last = None
        for attempt in range(self.retries):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                if not self._first_done:
                    self._first_done = True
                    self._say(f"первый ответ получен, модель {data.get('model') or self.model} — прогон пошёл")
                return data
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:400]
                last = RuntimeError(f"OpenRouter HTTP {e.code}: {detail}")
                dropped = None
                if e.code == 400 and "response_format" in detail and "response_format" in payload:
                    payload.pop("response_format")  # модель не умеет json-режим — повторяем без него
                    self.json_mode = False
                    dropped = "response_format"
                elif e.code == 400 and "reasoning" in detail and "reasoning" in payload:
                    payload.pop("reasoning")  # модель не даёт выключить рассуждения
                    self.reasoning = "default"
                    dropped = "reasoning"
                if dropped:
                    self._say(f"модель не приняла параметр {dropped} — повтор без него")
                    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    req = urllib.request.Request(self.chat_url, data=body, headers=headers, method="POST")
                    continue
                if e.code not in (408, 409, 429, 500, 502, 503, 504, 520, 524):
                    break
                wait = float(e.headers.get("Retry-After") or 0) or min(2 ** attempt, 30)
            except urllib.error.URLError as e:
                last = RuntimeError(f"OpenRouter недоступен: {e.reason}")
                wait = min(2 ** attempt, 30)
            except (ConnectionError, TimeoutError, http.client.HTTPException) as e:
                last = RuntimeError(f"OpenRouter оборвал соединение: {type(e).__name__}: {e}")
                wait = min(2 ** attempt, 30)
            with self._lock:
                self.usage["retries"] += 1
            self._say(f"повтор {attempt + 1}/{self.retries} через {wait:.0f} с: {last}")
            time.sleep(wait)
        with self._lock:
            self.usage["failed"] += 1
        raise last

    def _account(self, kind, data):
        u = data.get("usage") or {}
        with self._lock:
            self.usage["calls"] += 1
            self.usage["prompt_tokens"] += u.get("prompt_tokens") or 0
            self.usage["completion_tokens"] += u.get("completion_tokens") or 0
            self.usage["by_kind"][kind] = self.usage["by_kind"].get(kind, 0) + 1
            if data.get("model"):
                self.served_model = data["model"]

    # ---------------------------------------------------------------- перевод форматов
    @staticmethod
    def _to_openai(messages):
        """Сообщения эмулятора (формат ГигаЧата: function_call / role=function) → формат OpenAI (tools)."""
        out, call_id, n = [], None, 0
        for m in messages:
            role = m.get("role")
            if role == "assistant" and m.get("function_call"):
                n += 1
                call_id = f"call_{n}"
                args = m["function_call"].get("arguments")
                out.append({"role": "assistant", "content": m.get("content") or None, "tool_calls": [
                    {"id": call_id, "type": "function", "function": {
                        "name": m["function_call"]["name"],
                        "arguments": args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)}}]})
            elif role == "function":
                out.append({"role": "tool", "tool_call_id": call_id or "call_1",
                            "name": m.get("name"), "content": m.get("content") or ""})
            else:
                out.append({"role": role, "content": m.get("content") or ""})
        return out

    @staticmethod
    def _tools(functions):
        return [{"type": "function", "function": {"name": f["name"], "description": f["description"],
                                                  "parameters": f["parameters"]}} for f in functions]

    def _chat_raw(self, messages, functions=None, kind="chat", json_object=False, max_tokens=None):
        payload = {"model": self.model, "messages": self._to_openai(messages),
                   "temperature": self.temperature, "max_tokens": max_tokens or self.max_tokens}
        if functions:
            payload["tools"] = self._tools(functions)
            payload["tool_choice"] = "auto"
        if json_object and self.json_mode:
            payload["response_format"] = {"type": "json_object"}
        if self.reasoning == "off":
            payload["reasoning"] = {"enabled": False}
        elif self.reasoning in ("low", "medium", "high"):
            payload["reasoning"] = {"effort": self.reasoning}
        data = self._post(payload)
        self._account(kind, data)
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        tc = (msg.get("tool_calls") or [None])[0]
        fc = None
        if tc:
            args = (tc.get("function") or {}).get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            fc = {"name": (tc.get("function") or {}).get("name"), "arguments": args or {}}
        finish = "function_call" if fc else (choice.get("finish_reason") or "stop")
        out = {"content": msg.get("content") or "", "finish_reason": finish, "function_call": fc}
        if self.keep_calls:
            with self._lock:
                self.calls.append({"kind": kind, "input": messages, "output": out})
        return out

    # ---------------------------------------------------------------- интерфейс
    def chat(self, messages, functions=None):
        out = self._chat_raw(messages, functions, kind="chat")
        if not out["content"] and not out["function_call"] and out["finish_reason"] == "length":
            # лимит ушёл на рассуждения — пустой ответ агента испортил бы замер, повторяем с двойным лимитом
            self._say(f"агент вернул пустой ответ (обрезано по длине), повтор с лимитом {self.max_tokens * 2}")
            out = self._chat_raw(messages, functions, kind="chat", max_tokens=self.max_tokens * 2)
        return out

    def json_task(self, kind, prompt):
        hint = schema_hint(kind)
        strict = "\n\nReturn ONLY the JSON object, with no explanation and no markdown fences."
        limit, last = self.max_tokens_json, None
        for attempt in range(3):
            out = self._chat_raw([{"role": "user", "content": prompt + hint + (strict if attempt else "")}],
                                 kind=kind, json_object=True, max_tokens=limit)
            try:
                return normalize(kind, parse_json(out["content"]))
            except ValueError as e:
                last = e
                cut = out["finish_reason"] == "length"
                self._say(f"ответ на '{kind}' не разобрался (finish_reason={out['finish_reason']}, "
                          f"длина {len(out['content'])}), повтор {attempt + 1}/3"
                          + (f", лимит токенов {limit} → {limit * 2}" if cut else ""))
                if cut:  # обрезано по длине: тот же лимит даст тот же результат
                    limit *= 2
        raise ValueError(
            f"модель {self.model} не вернула JSON для '{kind}' за 3 попытки "
            f"(finish_reason={out['finish_reason']}, длина ответа {len(out['content'])}). "
            + ("Похоже, лимит токенов уходит на внутренние рассуждения модели: возьмите модель без "
               "рассуждений или поднимите OPENROUTER_MAX_TOKENS_JSON. " if out["finish_reason"] == "length" else "")
            + f"Последняя ошибка разбора: {last}")

    def plain(self, prompt, kind="judge", max_tokens=64):
        """Короткий текстовый запрос без функций (для судьи). Лимит — аргумент, общее состояние не меняем."""
        return self._chat_raw([{"role": "user", "content": prompt}], kind=kind, max_tokens=max_tokens)["content"]

    def ping(self):
        out = []
        try:
            r = self._chat_raw([{"role": "user", "content": "Reply with one word: working?"}])
            out.append(f"чат ({self.model}): OK — «{r['content'][:60]}»")
        except Exception as e:  # noqa: BLE001
            out.append(f"чат ({self.model}): ОШИБКА {e}")
            return out
        fn = [{"name": "get_user_memory_by_topics", "description": "Returns facts about the user by topic",
               "parameters": {"type": "object", "required": ["topics"], "properties": {"topics": {
                   "type": "array", "items": {"type": "string", "enum": ["family", "pets"]}}}}}]
        try:
            r = self._chat_raw([{"role": "user", "content": "Do I have a dog? Check your memory."}], fn)
            if r["finish_reason"] == "function_call":
                out.append(f"вызов функций: OK — модель вызвала функцию с {r['function_call'].get('arguments')}")
            else:
                out.append("вызов функций: модель ответила текстом без вызова функции — выберите модель "
                           "с поддержкой tools (см. openrouter.ai/models?supported_parameters=tools)")
        except Exception as e:  # noqa: BLE001
            out.append(f"вызов функций: ОШИБКА {e} — без функций бенч не запустить")
        try:
            r = self._chat_raw([{"role": "user", "content": 'Return JSON {"facts": ["ok"]}'}],
                               kind="collect", json_object=True)
            parse_json(r["content"])
            out.append("json-режим: OK")
        except Exception as e:  # noqa: BLE001
            out.append(f"json-режим: не работает ({e}) — будет использована текстовая подсказка схемы")
        return out
