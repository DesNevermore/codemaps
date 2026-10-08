from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

from providers import make_provider, Provider


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def add(self, u: dict):
        self.input_tokens += u.get("input_tokens", 0)
        self.output_tokens += u.get("output_tokens", 0)
        self.cache_read += u.get("cache_read", 0)
        self.cache_write += u.get("cache_write", 0)

    def as_dict(self):
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "cache_read": self.cache_read, "cache_write": self.cache_write}


class UsageLedger:

    def __init__(self):
        self.by_provider: dict[str, Usage] = {}
        self.calls: dict[str, int] = {}

    def add(self, provider: str, u: dict, calls: int = 1):
        prov = provider or "unknown"
        self.by_provider.setdefault(prov, Usage()).add(u)
        self.calls[prov] = self.calls.get(prov, 0) + calls

    def as_dict(self) -> dict:
        per = {}
        total = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0,
                 "calls": 0}
        for prov, u in self.by_provider.items():
            per[prov] = {**u.as_dict(), "calls": self.calls.get(prov, 0)}
            for k in ("input_tokens", "output_tokens", "cache_read", "cache_write"):
                total[k] += getattr(u, k)
            total["calls"] += self.calls.get(prov, 0)
        return {"by_provider": per, "total": total}


@dataclass
class LLMConfig:
    provider: str = field(default_factory=lambda: os.environ.get("PB_LLM_PROVIDER", "claude"))
    effort: str = field(default_factory=lambda: os.environ.get("PB_LLM_EFFORT", "high"))
    max_tokens: int = field(default_factory=lambda: int(os.environ.get("PB_LLM_MAX_TOKENS", "16384")))
    timeout: int = field(default_factory=lambda: int(os.environ.get("PB_LLM_TIMEOUT", "900")))

    @property
    def model(self) -> str:
        return self.provider


class LLMClient:
    def __init__(self, config: LLMConfig | None = None, provider: Provider | None = None):
        self.cfg = config or LLMConfig()
        self._provider = provider
        self.usage = Usage()
        self.calls = 0
        self.timings: list[dict] = []

    @property
    def provider(self) -> Provider:
        if self._provider is None:
            self._provider = make_provider(self.cfg.provider)
        return self._provider

    @property
    def is_stub(self) -> bool:
        return False

    def messages(self, system: str, msgs: list[dict], tools: list[dict] | None = None) -> dict:
        self.calls += 1
        t0 = time.time()
        err = None
        try:
            resp = self.provider.complete(system, msgs, tools,
                                          max_tokens=self.cfg.max_tokens,
                                          effort=self.cfg.effort, timeout=self.cfg.timeout)
        except BaseException as e:

            err = f"{type(e).__name__}: {str(e)[:200]}"
            self._record_timing(t0, time.time(), None, err)
            raise
        self.usage.add(resp.get("usage", {}))
        self._record_timing(t0, time.time(), resp.get("usage") or {}, None)
        return resp

    def _record_timing(self, t0: float, t1: float, usage: dict | None, error: str | None) -> None:
        rec = {"at": round(t0, 3), "wall_s": round(t1 - t0, 3), "provider": self.cfg.provider,
               "effort": self.cfg.effort, "call": self.calls,
               "role": os.environ.get("PB_LLM_TIMING_ROLE", "main"),
               "in": (usage or {}).get("input_tokens"), "out": (usage or {}).get("output_tokens"),
               "cache_read": (usage or {}).get("cache_read"), "error": error}
        self.timings.append(rec)
        path = os.environ.get("PB_LLM_TIMING_LOG")
        if not path:
            return
        try:
            with open(path, "a") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass


class StubLLM(LLMClient):

    def __init__(self, script: list[list[dict]] | None = None):
        super().__init__(LLMConfig(provider="stub"), provider=object())
        self.script = list(script or [])
        self._i = 0

    @property
    def is_stub(self) -> bool:
        return True

    def messages(self, system, msgs, tools=None) -> dict:
        self.calls += 1
        if self._i < len(self.script):
            content = self.script[self._i]
            self._i += 1
            stop = "tool_use" if any(b.get("type") == "tool_use" for b in content) else "end_turn"
            return {"content": content, "stop_reason": stop, "usage": {}}
        return {"content": [{"type": "text", "text": "[stub] script exhausted."}],
                "stop_reason": "end_turn", "usage": {}}


def make_client() -> LLMClient:
    return LLMClient()
