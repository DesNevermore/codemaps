#!/usr/bin/env python3
"""CodeMaps entry point: synthesize one ProgramBench task.

    uv run python src/agent/run_task.py <instance_id>
    uv run python src/agent/run_task.py <instance_id> --dry-run   # no API calls

The implementer works with a team of divergence hunters, critics and judges from the first turn.
Model and credentials come from the environment (PB_LLM_PROVIDER / PB_LLM_API_KEY); everything
else is fixed below.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

WALL_CLOCK_S = 21600
MAX_TURNS = 1000
MAX_SUBMISSIONS = 1
DOCKER_CPUS = 4

HUNTERS = 4
CRITICS = 2
JUDGES = 1

FEATURES = {
    "PB_SUCCESS_SIG_CAP": "12",
    "PB_CORPUS_TARGET": "350",
}
for _k, _v in FEATURES.items():
    os.environ.setdefault(_k, _v)

from llm import LLMClient, StubLLM          # noqa: E402  (after FEATURES: modules read env at import)
from orchestrator import Orchestrator       # noqa: E402


def _dry_run_script():
    return [
        [{"type": "text", "text": "[stub] recon"},
         {"type": "tool_use", "id": "a1", "name": "run_oracle", "input": {"args": ["--help"]}}],
        [{"type": "text", "text": "[stub] probe"},
         {"type": "tool_use", "id": "a2", "name": "diff", "input": {"args": []}}],
        [{"type": "text", "text": "[stub] write candidate"},
         {"type": "tool_use", "id": "a3", "name": "bash",
          "input": {"command": "cat > main.py <<'EOF'\nimport sys\nsys.stdout.write('')\nEOF"}}],
        [{"type": "text", "text": "[stub] regression"},
         {"type": "tool_use", "id": "a4", "name": "run_regression", "input": {}}],
        [{"type": "text", "text": "[stub] done (dry-run, no submit)"}],
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description="Run CodeMaps on one ProgramBench task.")
    ap.add_argument("instance_id")
    ap.add_argument("--artifact-dir", type=Path, default=None,
                    help="where to write the trajectory and score (default: src/agent/runs/<id>)")
    ap.add_argument("--dry-run", action="store_true",
                    help="use a scripted stub model: validates wiring, costs nothing")
    a = ap.parse_args()

    llm = StubLLM(_dry_run_script()) if a.dry_run else LLMClient()

    t0 = time.time()
    orch = Orchestrator(
        a.instance_id, llm,
        max_turns=MAX_TURNS,
        max_submissions=MAX_SUBMISSIONS,
        wall_clock_s=WALL_CLOCK_S,
        docker_cpus=DOCKER_CPUS,
        hunters=HUNTERS,
        artifact_dir=a.artifact_dir,
    )
    score = orch.run()
    score["wall_clock_s"] = round(time.time() - t0, 1)
    score["model"] = getattr(llm, "cfg", None) and llm.cfg.model
    score["dry_run"] = a.dry_run or llm.is_stub

    (orch.artifact / "score.json").write_text(json.dumps(score, indent=2))
    print(json.dumps(score, indent=2))
    return 0 if score.get("solved") else 1


if __name__ == "__main__":
    raise SystemExit(main())
