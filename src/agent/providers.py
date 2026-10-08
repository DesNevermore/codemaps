from __future__ import annotations

import json
import os
from pathlib import Path


MODELS = {
    "claude-opus":   ("anthropic", "claude-opus-4-8"),
}


THINKING_BUDGET = {"none": 0, "minimal": 0, "low": 2048, "medium": 8192,
                   "high": 16384, "xhigh": 24576, "max": 32768}

REASONING_EFFORT = {"minimal": "minimal", "low": "low", "medium": "medium",
                    "high": "high", "xhigh": "high", "max": "high"}


def load_api_key(kind: str) -> str:
    names = ("PB_LLM_API_KEY",
             "ANTHROPIC_API_KEY" if kind == "anthropic" else "OPENAI_API_KEY")
    for n in names:
        if os.environ.get(n):
            return os.environ[n]
    raise RuntimeError(f"missing API key: set {' or '.join(names)}")


def _orphan(m: dict) -> bool:
    return isinstance(m.get("content"), list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"])


def trim_oldest(msgs: list[dict]) -> bool:
    if not msgs:
        return False
    i = 1
    while i < len(msgs) and _orphan(msgs[i]):
        i += 1
    if i >= len(msgs):
        return False
    del msgs[0:i]
    return True


def _is_context_overflow(err: Exception) -> bool:
    s = str(err).lower()
    return any(k in s for k in ("context_length_exceeded", "reduce the length", "input length",
                                "too long", "context window", "prompt is too long"))


class Provider:
    name = "base"

    def complete(self, system: str, msgs: list[dict], tools: list[dict] | None,
                 *, max_tokens: int, effort: str, timeout: int = 600) -> dict:
        raise NotImplementedError

    def _complete_with_trim(self, send, system: str, msgs: list[dict], tools, **kw) -> dict:
        import ratelimit
        history = list(msgs)
        while True:
            ratelimit.acquire()
            try:
                return send(system, history, tools, **kw)
            except Exception as e:
                if not _is_context_overflow(e) or not trim_oldest(history):
                    raise


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, model: str):
        import anthropic
        self.model = model
        self.client = anthropic.Anthropic(
            api_key=load_api_key("anthropic"),
            base_url=os.environ.get("PB_LLM_BASE_URL") or None,
            max_retries=int(os.environ.get("PB_LLM_MAX_RETRIES", "8")))

    def complete(self, system, msgs, tools, *, max_tokens, effort, timeout=600):
        return self._complete_with_trim(self._send, system, msgs, tools,
                                        max_tokens=max_tokens, effort=effort, timeout=timeout)

    def _send(self, system, msgs, tools, *, max_tokens, effort, timeout):
        budget = THINKING_BUDGET.get(effort, 0)
        kw: dict = {}
        if budget:

            max_tokens = max(max_tokens, budget + 4096)
            kw["thinking"] = {"type": "enabled", "budget_tokens": budget}
        if system:

            kw["system"] = [{"type": "text", "text": system,
                             "cache_control": {"type": "ephemeral"}}]
        if tools:

            tools = [dict(t) for t in tools]
            tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
            kw["tools"] = tools
        resp = self.client.messages.create(
            model=self.model, max_tokens=max_tokens,
            messages=with_prefix_cache(msgs), timeout=float(timeout), **kw)
        u = resp.usage
        return {"content": strip_trailing_thinking([b.model_dump() for b in resp.content]),
                "stop_reason": resp.stop_reason or "end_turn",
                "usage": {"input_tokens": u.input_tokens,
                          "output_tokens": u.output_tokens,
                          "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
                          "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0}}


def strip_trailing_thinking(content: list) -> list:
    if not content:
        return content
    end = len(content)
    while end > 0 and content[end - 1].get("type") in ("thinking", "redacted_thinking"):
        end -= 1
    if end == len(content):
        return content
    return content[:end] if end else [{"type": "text", "text": "(thinking)"}]


def with_prefix_cache(msgs: list[dict]) -> list[dict]:
    if not msgs:
        return msgs
    out = list(msgs)
    last = dict(out[-1])
    c = last.get("content")
    if isinstance(c, str):
        last["content"] = [{"type": "text", "text": c, "cache_control": {"type": "ephemeral"}}]
    elif isinstance(c, list) and c:
        blocks = [dict(b) if isinstance(b, dict) else b for b in c]

        for i in range(len(blocks) - 1, -1, -1):
            if isinstance(blocks[i], dict) and                    blocks[i].get("type") not in ("thinking", "redacted_thinking"):
                blocks[i] = {**blocks[i], "cache_control": {"type": "ephemeral"}}
                break
        last["content"] = blocks
    out[-1] = last
    return out


class OpenAIProvider(Provider):
    name = "openai"

    def __init__(self, model: str):
        import openai
        self.model = model
        self.client = openai.OpenAI(
            api_key=load_api_key("openai"),
            base_url=os.environ.get("PB_LLM_BASE_URL") or None,
            max_retries=int(os.environ.get("PB_LLM_MAX_RETRIES", "8")))

    def complete(self, system, msgs, tools, *, max_tokens, effort, timeout=600):
        return self._complete_with_trim(self._send, system, msgs, tools,
                                        max_tokens=max_tokens, effort=effort, timeout=timeout)

    def _send(self, system, msgs, tools, *, max_tokens, effort, timeout):
        body: dict = {"model": self.model, "messages": self._to_chat(system, msgs),
                      "max_completion_tokens": max_tokens, "timeout": float(timeout)}
        if eff := REASONING_EFFORT.get(effort):
            body["reasoning_effort"] = eff
        if temp := os.environ.get("PB_LLM_TEMPERATURE"):

            body.pop("reasoning_effort", None)
            body["temperature"] = float(temp)
        if tools:
            body["tools"] = [{"type": "function",
                              "function": {"name": t["name"],
                                           "description": t.get("description", ""),
                                           "parameters": t["input_schema"]}} for t in tools]
        return self._from_chat(self.client.chat.completions.create(**body))

    def _to_chat(self, system: str, msgs: list[dict]) -> list[dict]:
        out: list[dict] = []
        if system:
            out.append({"role": "system", "content": system})
        for m in msgs:
            role, content = m["role"], m["content"]
            if isinstance(content, str):
                out.append({"role": role, "content": content})
                continue
            if role == "assistant":
                a: dict = {"role": "assistant",
                           "content": "".join(b["text"] for b in content
                                              if b.get("type") == "text")}
                if calls := [{"id": b["id"], "type": "function",
                              "function": {"name": b["name"],
                                           "arguments": json.dumps(b.get("input", {}))}}
                             for b in content if b.get("type") == "tool_use"]:
                    a["tool_calls"] = calls
                out.append(a)
            else:

                emitted = False
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_result":
                        c = b.get("content", "")
                        if isinstance(c, list):
                            c = "".join(x.get("text", "") if isinstance(x, dict) else str(x)
                                        for x in c)
                        out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": c})
                        emitted = True
                    elif b.get("type") == "text":
                        out.append({"role": "user", "content": b["text"]})
                        emitted = True
                if not emitted:
                    out.append({"role": role, "content": ""})
        return out

    def _from_chat(self, resp) -> dict:
        msg = resp.choices[0].message
        blocks: list[dict] = []
        if msg.content:
            blocks.append({"type": "text", "text": msg.content})
        calls = msg.tool_calls or []
        for tc in calls:
            try:
                inp = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                inp = {}
            blocks.append({"type": "tool_use", "id": tc.id,
                           "name": tc.function.name, "input": inp})
        if not blocks:
            blocks.append({"type": "text", "text": ""})
        u = resp.usage

        cache_read = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        return {"content": blocks,
                "stop_reason": "tool_use" if calls else "end_turn",
                "usage": {"input_tokens": max(0, u.prompt_tokens - cache_read),
                          "output_tokens": u.completion_tokens,
                          "cache_read": cache_read, "cache_write": 0}}


def make_provider(name: str, cache_task_id: str | None = None) -> Provider:
    kind, _, explicit = name.partition(":")
    if explicit:
        model = explicit
    elif name in MODELS:
        kind, model = MODELS[name]
    else:
        raise ValueError(f"unknown provider {name!r}; use an alias from MODELS "
                         f"({', '.join(sorted(MODELS))}) or 'anthropic:<model>'/'openai:<model>'")
    if kind == "anthropic":
        return AnthropicProvider(model)
    if kind == "openai":
        return OpenAIProvider(model)
    raise ValueError(f"unknown provider kind {kind!r} (expected 'anthropic' or 'openai')")
