from __future__ import annotations

import json
from pathlib import Path

try:
    from .case_model import case_identity
except ImportError:
    from case_model import case_identity

REGISTRY_DIR = Path(__file__).resolve().parents[2] / "findings" / "divergences"


UNDOC_DIR = Path(__file__).resolve().parents[2] / "findings" / "undocumented"


def _key(case: dict) -> str:
    return json.dumps(case_identity(case), sort_keys=True)


def merge_run(instance_id: str, divergences: list[dict], run_id: str = "") -> dict:
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    path = REGISTRY_DIR / f"{instance_id}.json"
    reg = json.loads(path.read_text()) if path.exists() else {"instance_id": instance_id, "cases": {}}
    cases = reg["cases"]
    for c in divergences:
        k = _key(c)
        if k in cases:
            cases[k]["times_seen"] += 1
            if run_id and run_id not in cases[k]["runs"]:
                cases[k]["runs"].append(run_id)
        else:
            cases[k] = {"args": c.get("args") or [], "stdin": (c.get("stdin") or "")[:2000],
                        "oracle_stdout": (c.get("oracle_stdout") or "")[:1000],
                        "oracle_stderr": (c.get("oracle_stderr") or "")[:1000],
                        "oracle_exit": c.get("oracle_exit"),
                        "crash_matchable": bool(c.get("crash_matchable")),
                        "found_by": c.get("found_by", ""),
                        "first_seen_run": run_id, "times_seen": 1,
                        "runs": [run_id] if run_id else []}
    reg["n_cases"] = len(cases)
    path.write_text(json.dumps(reg, indent=2))
    _write_undocumented(instance_id, cases)
    _write_index()
    return reg


def _write_index() -> None:
    rows = []
    for p in sorted(REGISTRY_DIR.glob("*.json")):
        reg = json.loads(p.read_text())
        cases = reg.get("cases", {})
        crash = sum(1 for c in cases.values() if c.get("crash_matchable"))

        undoc = sum(1 for c in cases.values() if str(c.get("found_by", "")).startswith("critic:"))
        rows.append((reg["instance_id"], len(cases), crash, undoc))
    lines = ["# Divergence registry (auto-generated)", "",
             "Every distinct test case where a hunter/critic/judge/fuzz subagent found the "
             "candidate differing from the oracle, accumulated across runs per task. Durable archive "
             "of discovered behaviour; complements the flaky-oracle registry. The 'undocumented' "
             "column counts divergences surfaced by the completeness critic — behaviour the "
             "binary's --help/README never document (hidden aliases, unlisted flags, env/locale, "
             "protocol conventions); see findings/undocumented/<id>.md for the actual inputs + "
             "findings/undocumented/INDEX.md for aggregate stats. "
             "See [[deterministic-fuzz-hunter]], [[fromscratch-fuzzer-generative-blindspot]].", "",
             "| task | distinct divergences | of which crash_matchable | of which undocumented |",
             "|---|---|---|---|"]
    lines += [f"| {tid} | {n} | {c} | {u} |" for tid, n, c, u in rows]
    lines.append("")
    (REGISTRY_DIR / "INDEX.md").write_text("\n".join(lines))
