from __future__ import annotations

import fcntl
import json
import os
import time

_STATE = os.environ.get("PB_LLM_RATE_FILE", "/tmp/pb_llm_ratelimit.json")


def _rate_per_sec() -> float:
    return float(os.environ.get("PB_LLM_RATE_PER_MIN", "55")) / 60.0


def acquire(timeout: float = 600.0) -> None:
    rps = _rate_per_sec()
    if rps <= 0:
        return
    burst = float(os.environ.get("PB_LLM_RATE_BURST", "1"))
    deadline = time.monotonic() + timeout
    while True:
        wait = _try_take(rps, burst)
        if wait <= 0:
            return
        if time.monotonic() >= deadline:
            return
        time.sleep(min(wait, 1.0))


def _try_take(rps: float, burst: float) -> float:
    fd = os.open(_STATE, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        now = time.time()
        try:
            st = json.loads(os.read(fd, 4096) or b"{}")
        except Exception:
            st = {}
        tokens = float(st.get("tokens", burst))
        last = float(st.get("ts", now))
        tokens = min(burst, tokens + (now - last) * rps)
        if tokens >= 1.0:
            tokens -= 1.0
            wait = 0.0
        else:
            wait = (1.0 - tokens) / rps
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps({"tokens": tokens, "ts": now}).encode())
        return wait
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
