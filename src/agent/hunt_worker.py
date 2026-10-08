from __future__ import annotations

import base64
import json
import os
import re
import sys
from pathlib import Path
from work_orders import format_work_orders

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from container import Cleanroom, DOCKER
from llm import LLMClient
from subagents import (HUNTER_LENSES, HUNTER_SYSTEM, JUDGE_SYSTEM,
                       CRITIC_SYSTEM, CRITIC_ANGLES,
                       run_subagent, clone_candidate)
from tools import (
    _b,
    _case_stdin,
    _is_crash,
    _NONDET_SETTLE_S,
    _normalize_volatile,
    _struct_fs_path,
    _struct_target_path,
    attach_exact_streams,
    exact_streams_match,
)
try:
    from .case_model import case_identity
except ImportError:
    from case_model import case_identity
import fuzz
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor
import subprocess


def _os_env_float(name: str, default: float) -> float:
    try: return float(os.environ.get(name) or default)
    except (TypeError, ValueError): return default


def _extract_probe_findings(summary: str, transcript: list) -> dict:
    texts = [summary or ""]
    for entry in reversed(transcript or []):
        content = entry.get("content")
        if isinstance(content, list):
            texts.append(" ".join(b.get("text", "") for b in content
                                  if isinstance(b, dict) and b.get("type") == "text"))
        elif isinstance(content, str):
            texts.append(content)
    for text in texts:
        for m in reversed(list(re.finditer(r"\{[^{}]*\"(?:flags|enum_values|subcommands)\"", text))):
            depth, start = 0, m.start()
            for i in range(start, len(text)):
                if text[i] == "{": depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            d = json.loads(text[start:i + 1])
                            if isinstance(d, dict):
                                return d
                        except Exception:
                            pass
                        break
    return {}


def _dump_capped(path, blob: dict, cap: int = 4_000_000) -> None:
    import json as _j
    text = _j.dumps(blob)
    if len(text) <= cap:
        path.write_text(text)
        return
    trimmed = dict(blob)
    dropped = []
    for key in ("transcript", "divergences", "work_orders"):
        if key not in trimmed:
            continue
        n = len(trimmed[key] or [])
        trimmed[key] = []
        dropped.append(f"{key}({n})")
        trimmed["truncated"] = {"cap_bytes": cap, "dropped": dropped}
        text = _j.dumps(trimmed)
        if len(text) <= cap:
            break
    path.write_text(text[:cap] if len(text) > cap else text)


def _write_atif_worker(out_dir, stem: str, blob: dict, instance_id: str) -> None:
    try:
        import atif
        (out_dir / f"{stem}.atif.json").write_text(json.dumps(
            atif.worker_trajectory(blob, trajectory_id=stem, session_id=instance_id),
            ensure_ascii=False)[:8_000_000])
    except Exception as e:
        sys.stderr.write(f"[atif] worker {stem}: {type(e).__name__}: {e}\n")


def _seed_room_from_tar(instance_id: str, cand_tar: bytes, inject_cargo: bool = False) -> Cleanroom:
    from room_factory import make_room, seed_candidate_tar
    import os as _os

    scpu = int(_os.environ.get("PB_SUB_CPUS", "2"))
    smem = _os.environ.get("PB_SUB_MEM_GB", "2") + "g"
    return _build_room(make_room(instance_id, cpus=scpu, memory=smem,
                                 inject_cargo=inject_cargo, role="sub"),
                       cand_tar, seed_candidate_tar)


def _build_room(room, cand_tar: bytes, seed_candidate_tar) -> Cleanroom:
    try:
        room.start()
        seed_candidate_tar(room, cand_tar, timeout=120)
        strict = os.environ.get("PB_TEST_DISCOVERY_V2", "1") != "0"
        build = room.sh("cd /workspace && sh compile.sh 2>&1" + ("" if strict else " || true"), timeout=300)
        if strict and build.code != 0:
            text = build.stdout.decode("utf-8", "replace") if isinstance(build.stdout, bytes) else str(build.stdout or "")
            raise RuntimeError(f"candidate clone build failed rc={build.code}: {text[-1200:]}")
        room.apply_env_setup()

    except BaseException:
        room.stop()
        raise
    return room


def rotation_off() -> bool:

    return os.environ.get("PB_LENS_ROTATE") == "0"

def _shq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _compact_trace(transcript) -> list:

    out = []
    for e in transcript or []:
        if e.get("role") != "assistant":
            continue
        for b in (e.get("content") or []):
            if isinstance(b, dict) and b.get("type") == "tool_use":
                out.append({"turn": e.get("turn"), "tool": b.get("name"),
                            "args": json.dumps(b.get("input", {}), ensure_ascii=False)[:160]})
    return out


def assign_work_orders(orders: list[dict], agent_count: int, round_id: int,
                       agent_index: int) -> list[dict]:
    if not orders or agent_index >= min(agent_count, len(orders)):
        return []
    start = (max(0, round_id - 1) * max(1, agent_count)) % len(orders)
    return [orders[(start + agent_index) % len(orders)]]


def attribute_work_orders(divergences: list[dict], orders: list[dict], tool_trace: list[dict],
                          mode: str, *, attempted: bool = True, error: str = "") -> list[dict]:
    tools_used = {x.get("tool") for x in (tool_trace or [])}
    results = []
    for order in orders or []:
        required = order.get("executor")
        applicable = bool(attempted and (not required or required in tools_used))

        def belongs(case):
            if not applicable:
                return False
            if required == "scenario":
                return bool(case.get("scenario") or case.get("executor") == "pty_screen")
            if required == "metamorphic":
                return bool(case.get("metamorphic"))
            if required == "propose_env":
                return bool(case.get("needs_env") or case.get("env_endpoint"))
            return True

        attributed = [c for c in (divergences or []) if belongs(c)]
        for case in attributed:
            case.setdefault("work_order_id", order.get("order_id"))
            case.setdefault("work_order_objective", order.get("objective"))
        blocked = bool(not attempted or not tools_used)
        if error:
            reason = error
        elif required and required not in tools_used:
            reason = "required executor not called"
        elif not attributed:
            reason = "no attributable divergence"
        else:
            reason = "attributable divergence found"
        results.append({"order_id": order.get("order_id"), "mode": mode,
                        "attempted": attempted, "applicable": applicable,
                        "found": len(attributed), "blocked": blocked, "reason": reason})
    return results


def _restore_struct(room, fingerprint: str):
    for part in fingerprint.split(";"):
        if "=" not in part:
            continue
        path, val = part.split("=", 1)
        if "|" not in val:
            continue
        kind, target, b64 = val.split("|", 2)
        try:
            data = base64.b64decode(b64, validate=True)
        except Exception:
            data = base64.b64decode(b64)
        if kind == "L" and target:
            link_fs, target_fs = _struct_target_path(path, target)
            room.exec(["mkdir", "-p", str(Path(target_fs).parent)])
            room.exec(["mkdir", "-p", str(Path(link_fs).parent)])
            room.write_bytes(target_fs, data)
            room.exec(["ln", "-sf", target, link_fs])
        else:
            fs_path = _struct_fs_path(path)
            room.exec(["rm", "-f", fs_path])
            room.exec(["mkdir", "-p", str(Path(fs_path).parent)])
            room.write_bytes(fs_path, data)


def _materialize_files(room, files: dict):
    for path, b64 in (files or {}).items():
        try:
            data = base64.b64decode(b64, validate=True)
        except Exception:
            data = base64.b64decode(b64)
        room.exec(["mkdir", "-p", str(Path(path).parent)])
        room.write_bytes(path, data)


def _is_exec_layer_error(code: int, stderr: str) -> bool:
    if code in (126, 127):
        return True
    s = stderr or ""
    return any(m in s for m in (
        "command not found", "Not a directory", "No such file or directory",
        "Permission denied", "cannot execute", "exec format error"))

def main():
    instance_id = sys.argv[1]
    cand_tar_path = sys.argv[2]
    mode = sys.argv[3]
    n = int(sys.argv[4]); parallel = int(sys.argv[5]); turns = int(sys.argv[6])
    inject_cargo = len(sys.argv) > 7 and sys.argv[7] == "1"
    out_dir = Path(sys.argv[8]) if len(sys.argv) > 8 and sys.argv[8] else None
    round_id = sys.argv[9] if len(sys.argv) > 9 else "0"
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    cand_tar = Path(cand_tar_path).read_bytes()
    seeds_path = sys.argv[10] if len(sys.argv) > 10 and sys.argv[10] else None

    focus = [r for r in (sys.argv[11].split(",") if len(sys.argv) > 11 and sys.argv[11] else []) if r]

    uncovered_flags = [f for f in (sys.argv[12].split(",") if len(sys.argv) > 12 and sys.argv[12] else []) if f]
    work_orders = []
    if len(sys.argv) > 13 and sys.argv[13]:
        try: work_orders = json.loads(Path(sys.argv[13]).read_text())
        except Exception: work_orders = []

    try:
        k = int(round_id)
    except (TypeError, ValueError):
        k = 0

    if mode == "buildcheck":
        try:
            room = _seed_room_from_tar(instance_id, cand_tar, inject_cargo)
        except BaseException as e:
            sys.stdout.write(json.dumps({"status":"failed", "clone_build_error":str(e)[:1600],
                                         "planned_lenses":1,"executed_lenses":0,"failed_lenses":1,
                                         "divergences":[],"per_lens":[],"usage":{},"llm_calls":0}))
            sys.stdout.flush(); return
        try:
            ok = room.exec(["test","-x","/workspace/executable"]).code == 0
            err = None if ok else "fresh clone produced no executable"

            if ok:
                p = room.exec(["sh","-c","cd /workspace && timeout 15 ./executable --help "
                                         "</dev/null >/dev/null 2>/tmp/_pb_smoke_err; "
                                         "rc=$?; head -c 600 /tmp/_pb_smoke_err; exit $rc"])
                tail = (p.stdout or b"").decode("utf-8", "replace").strip()

                fatal = ("Traceback (most recent call last)", "ModuleNotFoundError", "ImportError",
                         "FileNotFoundError", "No such file or directory",
                         "error while loading shared libraries", "cannot open shared object file")
                hit = next((s for s in fatal if s in tail), None)
                if p.code == 124:
                    ok, err = False, "fresh clone: ./executable --help HUNG (timeout 15s)"
                elif hit:
                    ok, err = False, (f"fresh clone: ./executable --help crashed at STARTUP ({hit}) — "
                                      f"the candidate depends on something that exists only in the "
                                      f"agent's own sandbox (e.g. a /tmp file it wrote). Embed the data "
                                      f"in the source instead of reading it at import. stderr: "
                                      f"{tail[:400]}")
            res={"status":"clean" if ok else "failed", "planned_lenses":1,
                 "executed_lenses":1 if ok else 0,"failed_lenses":0 if ok else 1,
                 "clone_build_error":err,
                 "divergences":[],"per_lens":[],"usage":{},"llm_calls":0}
            sys.stdout.write(json.dumps(res));sys.stdout.flush();return
        finally:
            room.stop()

    if mode == "fuzz":
        seeds = []
        if seeds_path and Path(seeds_path).exists():
            try:
                seeds = json.loads(Path(seeds_path).read_text())
            except Exception:
                seeds = []

        seeds = fuzz.select_seeds(seeds or [], n, round_id=k) if n else (seeds or [])
        import os as _os2
        bank_cap = int(_os2.environ.get("PB_FUZZ_BANK_CAP", "15"))
        res = _run_fuzz(instance_id, cand_tar, inject_cargo, seeds,
                        per_seed=max(20, turns * 3), parallel=parallel,
                        out_dir=out_dir, round_id=round_id, bank_cap=bank_cap,
                        extra_flags=uncovered_flags)
        sys.stdout.write(json.dumps(res)); sys.stdout.flush()
        return

    if mode == "critic":
        role = CRITIC_SYSTEM
        lenses = rotate_lenses(CRITIC_ANGLES, k, n)
    elif mode == "judge":
        role = JUDGE_SYSTEM

        blended = []
        for i in range(n):
            j = i if rotation_off() else k * n + i
            blended.append(CRITIC_ANGLES[j % len(CRITIC_ANGLES)] if i % 2
                           else HUNTER_LENSES[j % len(HUNTER_LENSES)])
        lenses = blended
    else:
        role = HUNTER_SYSTEM
        lenses = rotate_lenses(HUNTER_LENSES, k, n)

    import os as _os
    from llm import LLMConfig
    provider = _os.environ.get("PB_SUBAGENT_PROVIDER")
    if mode == "judge" and _os.environ.get("PB_JUDGE_PROVIDER"):
        provider = _os.environ["PB_JUDGE_PROVIDER"]
    llm = LLMClient(LLMConfig(provider=provider)) if provider else LLMClient()

    sem = threading.Semaphore(parallel)
    results: list[dict] = []
    lock = threading.Lock()

    def one(idx_lens):
        idx, lens = idx_lens
        assigned_orders = assign_work_orders(work_orders, n, k, idx)
        with sem:
            try:
                room = _seed_room_from_tar(instance_id, cand_tar, inject_cargo)
            except BaseException as e:
                return {"lens": lens, "status": "infra_failed",
                        "error": f"sandbox/build failed: {str(e)[:500]}", "divergences": [],
                        "work_orders": assigned_orders}
            if room.exec(["test", "-x", "/workspace/executable"]).code != 0:
                room.stop()
                return {"lens": lens, "status": "candidate_unbuildable",
                        "error": "clone/build produced no executable", "divergences": [],
                        "work_orders": assigned_orders}

            fellback = bool(getattr(room, "pb_local_fallback", False))
            try:
                focus_note = (
                    "FOCUS — the orchestrator has flagged these subcommands/modes as the candidate's "
                    "WEAKEST or UNCOVERED so far (most observed divergence, or never probed): "
                    f"{', '.join(focus)}. Spend the bulk of THIS round on them — exercise each one's "
                    "core/typical usage and diff oracle vs candidate — before touching regions you "
                    "have already probed. A neglected subcommand recovers far more points than another "
                    "edge case on an already-correct one.\n\n"
                ) if focus else ""
                uncov_note = (
                    "UNCOVERED FEATURES — these DOCUMENTED flags (from the oracle's own --help) have "
                    "NOT been exercised by any recorded case yet, so the candidate's behaviour on them "
                    f"is UNVERIFIED: {', '.join(uncovered_flags)}. Prioritise running each on the oracle "
                    "vs candidate (alone AND combined with the flags you already use) and bank every "
                    "divergence — a whole unimplemented/wrong feature recovers far more than another "
                    "edge of an already-correct one.\n\n"
                ) if uncovered_flags else ""
                order_objs = []
                for o in assigned_orders:
                    try:
                        from work_orders import CoverageWorkOrder
                        order_objs.append(CoverageWorkOrder(**o))
                    except Exception:
                        pass
                work_order_note = format_work_orders(order_objs)
                if work_order_note:
                    work_order_note += "\nReport for each assigned order whether it was attempted, applicable, and whether it found a real divergence.\n\n"
                seed = focus_note + uncov_note + work_order_note + (
                    "Before probing, THINK for one step (do not skip this):\n"
                    "1. WHAT KIND OF SOFTWARE is this? Run `--help`/`--version` and a couple of "
                    "happy-path inputs on /workspace/.oracle_ref to characterize it: what is its "
                    "input space (stdin? files? a query/selector/regex/URL language? binary?), its "
                    "output (text/structured/in-place edit?), its flags, and its state (filesystem? "
                    "network? env/locale?).\n"
                    "2. PROJECT YOUR LENS onto THIS software: given what it actually is, what are the "
                    "3-5 highest-value, NOT-yet-obvious things your lens implies for IT specifically? "
                    "Brainstorm beyond the literal lens text — what would a careful re-implementer of "
                    "THIS tool most plausibly get subtly wrong?\n"
                    "3. THINK IN COMBINATIONS: which CROSS-PRODUCTS of (flag × input-shape × "
                    "edge-value) for this tool have you NOT covered? The trickiest bugs live where two "
                    "dimensions meet (e.g. a format flag × an unusual node kind; a value flag × a "
                    "boundary input). List a few untested combinations.\n"
                    "4. THEN PROBE: turn each hypothesis into a concrete `run_oracle`/`diff` (or rely "
                    "on the mechanical fuzzer for byte-level breadth). Bank every divergence. Prefer "
                    "the inputs your reasoning flagged as most likely to break THIS specific tool.\n"
                    "Reference: /workspace/.oracle_ref  Candidate: /workspace/executable")
                r = run_subagent(role.format(lens=lens), seed, room, llm, max_turns=turns)
                r["lens"] = lens
                r["status"] = "ok"
                r["work_orders"] = assigned_orders
                r["tool_trace"] = _compact_trace(r.get("transcript"))
                work_order_results = attribute_work_orders(
                    r.get("divergences") or [], assigned_orders, r.get("tool_trace") or [], mode)
                r["work_order_results"] = work_order_results

                if out_dir:
                    import json as _j
                    blob = {"mode": mode, "round": round_id, "lens": lens,
                            "work_orders": assigned_orders,
                            "work_order_results": work_order_results,
                            "n_diff": r.get("n_diff"), "summary": r.get("summary"),
                            "transcript": r.get("transcript"),
                            "divergences": r.get("divergences")}
                    stem = f"{mode}_round{round_id}_lens{idx}"
                    _dump_capped(out_dir / f"{stem}.json", blob)
                    _write_atif_worker(out_dir, stem, blob, instance_id)

                    r["ref"] = f"subagents/{stem}.json"
                r.pop("transcript", None)
                r["backend"] = "local_fallback" if fellback else "cloud"
                return r
            finally:
                room.stop()

    with ThreadPoolExecutor(max_workers=parallel) as ex:
        for r in ex.map(one, list(enumerate(lenses))):
            with lock:
                results.append(r)

    seen = set(); merged = []
    for r in results:
        prov = f"{mode}:{(r.get('lens') or '')[:40]}"
        for c in r.get("divergences", []):
            key = case_identity(c)
            if key in seen:
                continue
            seen.add(key); c.setdefault("found_by", prov); merged.append(c)

    fseen = set(); flaky = []
    for r in results:
        for c in r.get("flaky", []):
            key = case_identity(c)
            if key in fseen:
                continue
            fseen.add(key); flaky.append(c)

    eseen = set(); env_proposals = []
    for r in results:
        for p in r.get("env_proposals", []):
            ep = p.get("endpoint", "")
            if ep and ep not in eseen:
                eseen.add(ep); env_proposals.append(p)
    ok_results = [r for r in results if r.get("status") == "ok"]
    failed_results = [r for r in results if r.get("status") != "ok"]
    n_real = sum(r.get("n_diff", 0) for r in ok_results)
    status = ("failed" if not ok_results else "partial" if failed_results else
              "found" if n_real else "clean")
    order_results = []
    for r in results:
        if r.get("work_order_results") is not None:
            order_results.extend(r.get("work_order_results") or [])
            continue
        tools = {x.get("tool") for x in (r.get("tool_trace") or [])}
        for order in r.get("work_orders") or []:
            attempted = r.get("status") == "ok"
            required = order.get("executor")
            applicable = bool(attempted and (not required or required in tools))
            found = len(r.get("divergences") or []) if applicable else 0
            blocked = not attempted or not tools
            order_results.append({"order_id": order.get("order_id"), "mode": mode,
                                  "attempted": attempted, "applicable": applicable,
                                  "found": found, "blocked": blocked,
                                  "reason": (r.get("error") or
                                             ("required executor not called" if required and required not in tools else
                                              "no divergence" if not found else "divergence found"))})
    out = {"status": status, "planned_lenses": len(lenses), "executed_lenses": len(ok_results),
           "failed_lenses": len(failed_results), "divergences": merged,
           "n_real_divergences": n_real,
           "flaky": flaky,
           "env_proposals": env_proposals,
           "order_results": order_results,

           "per_lens": [{"lens": (r.get("lens") or "")[:50], "n_diff": r.get("n_diff", 0),
                         "summary": (r.get("summary") or "")[:300],
                         "status": r.get("status", "ok" if not r.get("error") else "infra_failed"),
                         "error": r.get("error"),
                         "backend": r.get("backend"),
                         "ref": r.get("ref"),
                         "n_tool_calls": len(r.get("tool_trace") or [])} for r in results],
           "usage": llm.usage.as_dict(), "llm_calls": llm.calls}
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
