from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from llm import LLMClient, UsageLedger
from container import Cleanroom, CAND, CAND_EXE, ORACLE
from room_factory import make_room
from tools import ToolBox, tool_schemas
from generator import mine_docs, mine_feature_graph
from feature_graph import coverage_debt, update_coverage, augment_behavioral_obligations
from work_orders import make_work_orders, generic_capability_orders
from capability_detector import detect_capabilities
from metamorphic_planner import plan_properties
from case_model import (merge_signature, signature_cap, case_identity,
                        is_trivial_surface_case, is_root_surface_case)
from case_ranker import rank_case, case_source, case_modality, allocate_source_quotas
from developer_probe import load_probe_journal, helper_script
from holdout_selector import evaluate_holdout
from candidate_selector import (
    bounded_candidates,
    developer_improves,
    minimum_region_floor,
    select_replayed_candidate,
    selection_vector,
)
import prompts

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))


_SNAPSHOT_SKIP_DIRS = {"target", "vendor", "node_modules", "__pycache__", ".git"}
_SNAPSHOT_MAX_FILE_BYTES = 10 * 1024 * 1024


def candidate_tar_excludes(baseline_files) -> str:

    import shlex
    excludes = ["--exclude=./executable", "--exclude=./.oracle_ref", "--exclude=./.coverage",
                "--exclude=./.git", "--exclude=./target", "--exclude=./vendor",
                "--exclude=./node_modules", "--exclude=__pycache__",
                "--exclude=*.pyc", "--exclude=*.pyo"]
    for rel in sorted(baseline_files or set()):
        excludes.append("--exclude=" + shlex.quote("./" + rel))
    return " ".join(excludes)


def _extract_env_script(text: str) -> str:
    import re
    m = re.search(r"```(?:sh|bash)?\s*\n(.*?)```", text or "", re.DOTALL)
    if not m:
        return ""
    script = m.group(1).strip()
    if not script or "NO_ENV_NEEDED" in script:
        return ""
    return script + "\n"

def _snapshot_ignore(src, names):

    import os
    skip = {n for n in names if n in _SNAPSHOT_SKIP_DIRS}
    for n in names:
        p = Path(src) / n
        try:
            if p.is_file() and p.stat().st_size > _SNAPSHOT_MAX_FILE_BYTES:
                skip.add(n)
            elif p.is_file() and not os.access(p, os.R_OK):
                skip.add(n)
        except OSError:
            skip.add(n)
    return skip


def _snapshot_copytree(src, dst):
    import shutil

    try:
        shutil.copytree(src, dst, ignore=_snapshot_ignore, symlinks=True)
    except shutil.Error:
        pass


def _trade_ledger_entry(row: dict) -> dict | None:
    pre, post = row.get("pre_validation") or {}, row.get("post_validation") or {}
    if "all_pass_keys" not in pre or "all_pass_keys" not in post:
        return None
    before, after = set(pre["all_pass_keys"]), set(post["all_pass_keys"])
    lost, gained = before - after, after - before
    anchors = set(pre.get("anchor_pass_keys") or ())
    lost_anchor = lost & anchors
    return {"pre_pass": len(before), "post_pass": len(after),
            "gained": len(gained), "lost": len(lost),
            "lost_anchor": len(lost_anchor),

            "lost_unprotected": len(lost - anchors),
            "net": len(after) - len(before),
            "lost_keys": sorted(lost)[:40]}


def _json_safe(obj):

    if isinstance(obj, str):
        return obj.encode("utf-8", "replace").decode("utf-8")
    if isinstance(obj, dict):
        return {_json_safe(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _write_json(path, obj, **kw) -> None:

    Path(path).write_text(json.dumps(_json_safe(obj), ensure_ascii=False, **kw))


_PLACEHOLDER_MAIN = ("import sys\n# RE candidate — to be filled by the agent\n"
                     "sys.stdout.write('')\n")


def _is_placeholder_candidate(d) -> bool:

    mp = d / "main.py"
    try:
        if not mp.is_file() or mp.read_bytes() != _PLACEHOLDER_MAIN.encode():
            return False
    except OSError:
        return False
    src_exts = (".py", ".c", ".cpp", ".cc", ".go", ".rs", ".js", ".ts", ".java")
    return not any(p.suffix in src_exts and p.name != "main.py"
                   for p in d.rglob("*") if p.is_file())


_WORKER_MEM_LIMIT_BYTES = 6 * 1024 * 1024 * 1024


def _limit_worker_memory() -> None:
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (_WORKER_MEM_LIMIT_BYTES, _WORKER_MEM_LIMIT_BYTES))


_PHASE1_DEFAULT_TURNS = 400






SYSTEM = prompts.load("system_main")


MULTIAGENT_NOTE = prompts.load("multiagent_note")


MULTIAGENT_NOTE_DEVELOPER = prompts.load("multiagent_note_developer")


def _multiagent_note(has_developer: bool) -> str:
    return MULTIAGENT_NOTE + (MULTIAGENT_NOTE_DEVELOPER if has_developer else "")


def _is_tool_result_msg(m: dict) -> bool:
    c = m.get("content")
    return isinstance(c, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in c)


def _last_input_tokens(llm) -> int:

    for rec in reversed(getattr(llm, "timings", []) or []):
        if rec.get("in") is not None or rec.get("cache_read"):

            return (int(rec.get("in") or 0) + int(rec.get("cache_read") or 0)
                    + int(rec.get("cache_write") or 0))
    return 0


def _truncate_msgs(msgs: list[dict], keep: int = 40) -> list[dict]:
    if len(msgs) <= keep + 1:
        return msgs
    target = len(msgs) - keep
    start = None

    for i in range(target, 0, -1):
        if not _is_tool_result_msg(msgs[i]):
            start = i
            break
    if start is None:
        return msgs
    return msgs[:1] + msgs[start:]

from subagents import HUNTER_LENSES


def summarize_worker_results(results: list[dict]) -> dict:
    if not results:
        return {"status": "failed", "executed_lenses": 0, "failed_lenses": 0}
    executed = sum(int(r.get("executed_lenses", 1 if r.get("status") in ("clean", "found") else 0))
                   for r in results)
    failed = sum(int(r.get("failed_lenses", 1 if r.get("status") == "failed" else 0))
                 for r in results)
    real = sum(int(r.get("n_real_divergences", 0) or 0) for r in results)
    statuses = [r.get("status", "failed") for r in results]
    if executed == 0:
        status = "failed"
    elif failed or any(st in ("partial", "failed") for st in statuses):
        status = "partial"
    elif real:
        status = "found"
    else:
        status = "clean"
    return {"status": status, "executed_lenses": executed,
            "failed_lenses": failed, "real_divergences": real}


class Orchestrator:
    def __init__(self, instance_id: str, llm: LLMClient, *, max_turns=200,
                 max_submissions=1, wall_clock_s=10800, docker_cpus=4,
                 artifact_dir: Path | None = None, eval_gate=None,
                 hunters=0, judges=0, hunt_parallel=4, hunter_turns=14,
                 max_hunt_rounds=14, hunt_min_free_gb=12.0,
                 inject_cargo=False, lang_hint="", critics=0, sweep_every_turns=25,
                 subagent_provider="", judge_provider="",
                 reg_parallel=8, reg_timeout=10, dry_sweeps_to_stop=5, fuzz_seeds=12,
                 fuzz_bank_cap=15, fuzz_burst_seeds=40, fuzz_burst_bank_cap=80,
                 min_seed_diversity_for_submit=24, gate_trickle_max=3, gate_converge_streak=2,
                 candidate_snapshot_min=5, regression_guard=True,
                 nearend_warn_seconds=600,
                 weak_rate=0.5, regress_anchor_eps=1, regress_decline_window=3):
        import os
        self.test_discovery_v2 = os.environ.get("PB_TEST_DISCOVERY_V2", "1") != "0"
        self.generic_scenarios_enabled = (self.test_discovery_v2 and os.environ.get("PB_GENERIC_SCENARIOS", "0") == "1")
        self.instance_id = instance_id
        self.llm = llm

        self.regression_guard = regression_guard

        self.fuzz_seeds = fuzz_seeds

        self.fuzz_bank_cap = fuzz_bank_cap
        self.fuzz_burst_seeds = fuzz_burst_seeds
        self.fuzz_burst_bank_cap = fuzz_burst_bank_cap
        self.fuzz_saturation_enabled = (self.test_discovery_v2
                                           and os.environ.get("PB_FUZZ_SATURATION", "1") != "0")
        self.fuzz_saturation_rounds = int(os.environ.get("PB_FUZZ_SATURATION_ROUNDS", "3"))
        self._fuzz_no_novel_streak = 0
        self._fuzz_saturated = False

        self.min_seed_diversity_for_submit = min_seed_diversity_for_submit
        self.reg_parallel = reg_parallel
        self.reg_timeout = reg_timeout
        self.subagent_provider = subagent_provider

        self.judge_provider = judge_provider

        self.two_phase = False
        self.phase1_max_turns = 0


        self.phase1_protocol = "tool_use"


        self.phase2_warmup = False
        self.phase2_max_turns = 0
        self.phase2_wall_clock = wall_clock_s

        self.plan_a = False
        self.total_wall_clock = wall_clock_s
        self.developer_wall_clock = 10800

        self.probe_wall_clock = 5400
        self._probe_wall_spent = 0.0
        self.nearend_warn_seconds = nearend_warn_seconds

        self.ctx_trim_tokens = int(os.environ.get("PB_CTX_TRIM_TOKENS", "0") or 0)
        self.ctx_trim_keep = int(os.environ.get("PB_CTX_TRIM_KEEP", "60") or 60)
        self._ctx_trims = 0
        self.developer_max_runs = 3
        self.developer_min_gap_turns = 30
        self.developer_min_useful = 1200
        self.developer_targeted_wall_clock = 2400

        self.developer_targeted_max_turns = 200
        self.developer_stall_turns = 40
        self.developer_targeted_max_failures = 12
        self.weak_rate = weak_rate
        self.regress_anchor_eps = regress_anchor_eps
        self.regress_decline_window = regress_decline_window
        self._state = "EMPTY"
        self._last_steered_state = None
        self._dev_runs_used = 0
        self._developer_request_seq = 0
        self._developer_attempts: list[dict] = []
        self._last_dev_turn = -10**9
        self._last_candidate_edit_turn = 0
        self._last_plan_a_writes = 0
        self._last_validation_progress_turn = 0
        self._best_progress_vector = None
        self.architecture_cluster_min_size = max(
            2, int(os.environ.get("PB_ARCH_CLUSTER_MIN_SIZE", "4")))
        self.architecture_cluster_min_revisions = max(
            2, int(os.environ.get("PB_ARCH_CLUSTER_MIN_REVISIONS", "2")))

        self.arch_steer_cooldown_turns = max(0, int(os.environ.get("PB_ARCH_STEER_COOLDOWN", "0")))
        self.arch_steer_max = max(0, int(os.environ.get("PB_ARCH_STEER_MAX", "0")))
        self.arch_steer_require_mature = os.environ.get("PB_ARCH_STEER_REQUIRE_MATURE", "0") == "1"

        self.arch_steer_min_revisions = max(
            2, int(os.environ.get("PB_ARCH_STEER_MIN_REVISIONS", "3")))
        self._last_architecture_steer_turn = -10 ** 9
        self._architecture_steers_sent = 0
        self._validation_revision = 0
        self._failure_cluster_history: dict[str, dict] = {}
        self._observed_validation_revisions: set[str] = set()
        self._persistent_architecture_clusters: list[dict] = []
        self._last_architecture_ceiling_steer = None
        self._clone_build_cache = {
            "writes": -1, "source_digest": "", "ok": None, "error": ""}
        self._clone_build_error_steered = None
        self._best_anchor_pass = 0
        self._best_anchor_pass_set: set[str] = set()
        self._rate_history: list[float] = []
        self._nearend_wrapup_sent = False
        self._anchors_seeded = False
        self._metamorphic_attempted: set[tuple[str, str]] = set()
        self._scenario_schedule_done = False
        self._component_telemetry: dict[str, dict] = {}
        self._work_order_lifecycle: dict[str, dict] = {}
        self._env_scout_done = False
        self._env_endpoints: set = set()
        self._record_sink = None
        self._phase = 1 if self.two_phase else 2
        self._phase1_done = not self.two_phase
        self._bootstrap_declared = False
        self._phase1_wrapup_sent = False
        self._phase2_timewarn_sent = False

        self._loop_started_at = None
        self._phase2_started_at = None
        self.max_turns = max_turns
        self.max_submissions = max_submissions

        self.deadline = time.time() + wall_clock_s
        self.docker_cpus = docker_cpus
        self.eval_gate = eval_gate

        self.hunters = hunters
        self.judges = judges
        self.critics = critics
        self.hunt_parallel = hunt_parallel
        self.hunter_turns = hunter_turns
        self.max_hunt_rounds = max_hunt_rounds
        self.hunt_min_free_gb = hunt_min_free_gb
        self.seed_solution_dir = None
        self.inject_cargo = inject_cargo
        self.lang_hint = lang_hint
        self._seeded_from_solution = False
        self.hunt_rounds_used = 0
        self.hunt_log: list[dict] = []
        self._all_divergences: list[dict] = []

        self._region_divergences: dict[str, int] = {}

        self.dry_sweeps_to_stop = dry_sweeps_to_stop
        self._consecutive_dry_sweeps = 0
        self._steered_to_submit = False
        self._steered_last_resort = False
        self._last_submit_steer_turn = -10**9
        self.submit_steer_every = 10
        self.per_sig_cap = 3
        self.success_sig_cap = int(os.environ.get("PB_SUCCESS_SIG_CAP", "12"))
        self.case_merge_v2 = (self.test_discovery_v2
                              and os.environ.get("PB_CASE_MERGE_V2", "1") != "0")
        self.holdout_selector_enabled = (self.test_discovery_v2
                                         and os.environ.get("PB_HOLDOUT_SELECTOR", "1") == "1")
        self.final_pool_replay_enabled = (self.test_discovery_v2
                                          and os.environ.get("PB_FINAL_POOL_REPLAY", "1") == "1")

        self.final_pool_max_candidates = max(1, int(os.environ.get("PB_FINAL_POOL_MAX_CANDIDATES", "12")))
        self.final_pool_snapshot_samples = max(0, int(os.environ.get("PB_FINAL_POOL_SNAPSHOT_SAMPLES", "3")))

        self.final_replay_wall_clock = max(60, int(os.environ.get("PB_FINAL_REPLAY_WALL_CLOCK", "1800")))

        self.developer_quarantine = os.environ.get("PB_DEVELOPER_QUARANTINE", "1") == "1"

        self.final_pool_wide_intake = os.environ.get("PB_FINAL_POOL_WIDE_INTAKE", "1") == "1"
        self.developer_scope_normalize = os.environ.get("PB_DEVELOPER_SCOPE_NORMALIZE", "1") == "1"

        self.intake_backlog_recovery = os.environ.get("PB_INTAKE_BACKLOG_RECOVERY") == "1"
        self.intake_backlog_max = max(0, int(os.environ.get("PB_INTAKE_BACKLOG_MAX", "500")))
        self._intake_backlog: dict[str, dict] = {}

        self.sweep_budget_floor = max(0, int(os.environ.get("PB_SWEEP_BUDGET_FLOOR", "0")))

        self._sweeps_done = 0
        self._sweeps_executed = 0
        self._sweep_attempts = 0
        self._last_sweep_at = 0.0
        self._last_sweep_attempt_at = 0.0
        self.sweep_wall_interval_seconds = max(
            0, int(os.environ.get("PB_SWEEP_WALL_INTERVAL_SECONDS", "2400")))
        self.sweep_retry_seconds = max(15, int(os.environ.get("PB_SWEEP_RETRY_SECONDS", "120")))
        self._successful_fuzz_rounds = 0
        self._successful_judge_panels = 0
        self._last_fuzz_round_result = None

        self.sealed_challenge_enabled = (self.test_discovery_v2
                                         and os.environ.get("PB_SEALED_CHALLENGE", "1") == "1")
        try:
            self.sealed_challenge_fraction = min(
                1.0, max(0.0, float(os.environ.get("PB_SEALED_CHALLENGE_FRACTION", "0.15"))))
        except ValueError:
            self.sealed_challenge_fraction = 0.15
        try:
            self.sealed_challenge_max = max(
                0, int(os.environ.get("PB_SEALED_CHALLENGE_MAX", "24")))
        except ValueError:
            self.sealed_challenge_max = 24

        try:
            self.sealed_challenge_repair_floor = max(
                0, int(os.environ.get("PB_SEALED_CHALLENGE_REPAIR_FLOOR", "12")))
        except ValueError:
            self.sealed_challenge_repair_floor = 12
        self._sealed_challenge: list[dict] = []
        self._sealed_challenge_clusters: set[str] = set()
        self._sealed_challenge_contaminated = 0
        self._best_validation_score = -1.0
        self._best_selection_key = None
        self._candidate_pool_labels: set[str] = set()
        self._final_pool_selected = False
        self._case_drop_counts: dict[str, int] = {}
        self.per_region_cap = 25

        self.top_region_cap = 12

        self.surface_case_cap = max(4, int(os.environ.get("PB_SURFACE_CASE_CAP", "16")))
        self.subcommand_help_anchor_cap = max(
            0, int(os.environ.get("PB_SUBCOMMAND_HELP_ANCHOR_CAP", "6")))

        self.region_depth_target = 8

        self.sweep_intake_cap = int(os.environ.get("PB_SWEEP_INTAKE_CAP", "30"))

        import os
        self.corpus_target = int(os.environ.get("PB_CORPUS_TARGET", "350"))

        self.submit_redirect_frac = 0.5
        self._phase2_started_turn = 0

        self._last_writes_seen = 0
        self._turns_since_edit = 0
        self.edit_drought_turns = 8
        self.sweep_every_turns = sweep_every_turns
        self._last_sweep_turn = 0

        self._sweep_first_turn = int(os.environ.get("PB_SWEEP_FIRST_TURN", "0") or 0)
        if self._sweep_first_turn > 0:
            self._last_sweep_turn = self._sweep_first_turn - sweep_every_turns

        self._consecutive_clean_turns = 0
        self.clean_streak_to_submit = 20

        self._gate_trickle_streak = 0
        self.gate_trickle_max = gate_trickle_max
        self.gate_converge_streak = gate_converge_streak
        try:
            self.submit_required_rate_floor = min(
                1.0, max(0.0, float(os.environ.get("PB_SUBMIT_REQUIRED_RATE_FLOOR", "0.85"))))
        except ValueError:
            self.submit_required_rate_floor = 0.85
        self._submit_attempt_fingerprints: set[str] = set()

        self.write_by_turn = 18
        self._wrote_real_candidate = False
        self.artifact = artifact_dir or (REPO / "src" / "agent" / "runs" / instance_id)
        self.artifact.mkdir(parents=True, exist_ok=True)
        self.sol_dir = self.artifact / "solution"
        self._best_local_rate = -1.0
        self._last_cand_archive_at = None
        self.candidate_snapshot_min = candidate_snapshot_min
        self.transcript: list[dict] = []
        self._turns = 0
        self.room: Cleanroom | None = None
        self.box: ToolBox | None = None
        import threading as _t
        self._usage_lock = _t.Lock()

        self._candidate_state_lock = _t.RLock()

        self._ledger = UsageLedger()
        self._impl_usage_synced = {"input_tokens": 0, "output_tokens": 0,
                                   "cache_read": 0, "cache_write": 0, "calls": 0}

        self.resume_from = None
        if self.resume_from:
            self._load_resume_state()

        self.bootstrap_artifact = None
        if self.bootstrap_artifact:
            self._load_bootstrap_artifact()

        self.bootstrap_only = None
        if self.bootstrap_only and self.bootstrap_artifact:
            raise RuntimeError("--bootstrap-only and --bootstrap-artifact are mutually exclusive")

    def _load_bootstrap_artifact(self) -> None:

        d = self.bootstrap_artifact
        contract = d / "BOOTSTRAP.json"
        if not contract.is_file():
            raise RuntimeError(f"--bootstrap-artifact {d}: no BOOTSTRAP.json")
        meta = json.loads(contract.read_text())
        for key in ("instance_id", "wall_seconds", "usage", "llm_calls", "prompt_sha256"):
            if key not in meta:
                raise RuntimeError(f"--bootstrap-artifact {d}: BOOTSTRAP.json missing '{key}'")
        if meta["instance_id"] != self.instance_id:
            raise RuntimeError(f"--bootstrap-artifact {d}: built for {meta['instance_id']}, "
                               f"this run is {self.instance_id}")
        if not (d / "candidate").is_dir():
            raise RuntimeError(f"--bootstrap-artifact {d}: no candidate/ directory")
        self._bootstrap_meta = meta
        self._bootstrap_wall = float(meta["wall_seconds"])
        report = d / "candidate" / "AGENT_REPORT.md"
        self._bootstrap_report = report.read_text() if report.is_file() else ""
        self._log(f"bootstrap artifact: {d.name} wall={self._bootstrap_wall:.0f}s "
                  f"usage_source={meta.get('usage_source', '?')} report={len(self._bootstrap_report)}B")

    def _load_resume_state(self) -> None:

        d = self.resume_from
        score_p, corpus_p, tr_p = d / "score.json", d / "corpus.json", d / "transcript.jsonl"
        for p in (score_p, corpus_p, tr_p):
            if not p.is_file():
                raise RuntimeError(f"--resume-from {d}: missing {p.name} (cannot resume; run "
                                   f"--phases 3 --seed-solution-dir instead to restart fresh)")
        score = json.loads(score_p.read_text())

        used = float((score.get("phase_seconds") or {}).get(
            "total" if self.plan_a else "phase2", 0.0) or 0.0)
        budget = float(self.total_wall_clock if self.plan_a else self.phase2_wall_clock)
        self._resume_used_seconds = used
        self._resume_remaining = max(300.0, budget - used)
        self._resume_hunt_rounds = score.get("hunt_rounds", 0)
        self._resume_dev_runs = int(score.get("dev_runs", 0) or 0)
        self._resume_submissions = list(score.get("submissions") or [])
        self._resume_corpus = json.loads(corpus_p.read_text())

        usage = score.get("usage") or {}
        if self.llm is not None:
            for key in ("input_tokens", "output_tokens", "cache_read", "cache_write"):
                setattr(self.llm.usage, key, int(usage.get(key, 0) or 0))
            self.llm.calls = int(score.get("llm_calls", 0) or 0)
        challenge_p = d / "sealed_challenge.json"
        if challenge_p.is_file():
            loaded = json.loads(challenge_p.read_text())
            self._sealed_challenge = loaded if isinstance(loaded, list) else []
            self._sealed_challenge_clusters = {
                self._semantic_cluster_key(c) for c in self._sealed_challenge
                if isinstance(c, dict)
            }
        self._resume_msgs, self._resume_turn = self._replay_transcript_to_msgs(tr_p)
        self._log(f"resume: {len(self._resume_corpus)} corpus cases, {len(self._resume_msgs)} replayed "
                  f"msgs, turn {self._resume_turn}, {self._resume_remaining:.0f}s remaining "
                  f"(of {budget:.0f}s, {used:.0f}s used; plan_a={self.plan_a})")

    @staticmethod
    def _replay_transcript_to_msgs(path: Path) -> tuple[list[dict], int]:
        rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        turns = [r.get("turn") for r in rows]
        start = 0
        for i in range(1, len(turns)):
            if isinstance(turns[i], int) and isinstance(turns[i - 1], int) and turns[i] < turns[i - 1]:
                start = i

        raw_msgs, last_turn = [], 0
        for r in rows[start:]:
            if "assistant" in r:
                raw_msgs.append({"role": "assistant", "content": r["assistant"]})
            elif "tool_results" in r:
                raw_msgs.append({"role": "user", "content": r["tool_results"]})
            else:
                continue
            if isinstance(r.get("turn"), int):
                last_turn = max(last_turn, r["turn"])

        msgs = []
        i = 0
        while i < len(raw_msgs):
            msg = raw_msgs[i]
            if msg["role"] == "user":

                msgs.append(msg)
                i += 1
                continue
            content = msg.get("content")
            tool_ids = {
                str(block.get("id")) for block in (content if isinstance(content, list) else [])
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id")
            }
            if not tool_ids:
                msgs.append(msg)
                i += 1
                continue
            nxt = raw_msgs[i + 1] if i + 1 < len(raw_msgs) else None
            result_ids = {
                str(block.get("tool_use_id")) for block in (
                    nxt.get("content", []) if isinstance(nxt, dict) else [])
                if isinstance(block, dict) and block.get("type") == "tool_result"
                and block.get("tool_use_id")
            }
            if nxt and nxt.get("role") == "user" and tool_ids.issubset(result_ids):
                msgs.extend((msg, nxt))
                i += 2
            else:
                i += 1
        return msgs, last_turn

    def _exam(self) -> dict:

        import os
        deferred = os.environ.get("PB_DEFER_EXAM", "0") == "1"

        if deferred and not hasattr(self, "sol_dir"):
            return {"passed": 0, "total": 0, "pass_rate": 0.0, "solved": False,
                    "deferred": True, "note": "official exam deferred to post-run scorer"}

        self._sync_solution_out()
        self._select_final_candidate_from_pool()

        self._guard_not_worse_than_p1()
        self._sync_solution_out()
        if deferred:
            return {"passed": 0, "total": 0, "pass_rate": 0.0, "solved": False,
                    "deferred": True, "note": "official exam deferred to post-run scorer"}
        gated = False
        if self.eval_gate:
            if not self.eval_gate[0]():
                return {"passed": 0, "total": 0, "pass_rate": 0.0,
                        "solved": False, "error": "eval gate timeout (RAM/slots)"}
            gated = True
        try:
            from exam import run_exam
            score = run_exam(self.instance_id, self.sol_dir,
                             docker_cpus=self.docker_cpus, keep_image=True,
                             timeout=2700)
        except BaseException as e:
            return {"passed": 0, "total": 0, "pass_rate": 0.0, "error": str(e)}
        finally:
            if gated:
                self.eval_gate[1]()
        score.setdefault("passed", score.get("resolved"))
        return score

    def _log(self, msg: str):
        try:
            line = f"{time.strftime('%H:%M:%S')} t{getattr(self, '_turns', 0)} {msg}\n"
            with (self.artifact / "run_events.log").open("a") as f:
                f.write(line)
        except Exception:
            pass

    def _sync_solution_out(self):
        with self._candidate_state_guard():
            return self._sync_solution_out_locked()

    def _sync_solution_out_locked(self):
        self.sol_dir.mkdir(parents=True, exist_ok=True)

        import subprocess, tarfile, io, shlex

        tmp = "/tmp/_pb_candidate_sync.tar"
        try:
            r = self.room.exec(["sh", "-c", self._candidate_tar_cmd() + f" > {shlex.quote(tmp)}"])
            if r.code != 0:
                self._log(f"sync: /workspace candidate tar failed (exit {r.code}) — container exec problem?")
                return
            raw = self.room.read_bytes(tmp)
            self.room.exec(["rm", "-f", tmp])
        except Exception as e:

            self._log(f"sync: candidate pull failed ({type(e).__name__}: {str(e)[:120]}) — skipping this sync")
            return
        if not raw:
            self._log("sync: candidate tar empty — not overwriting host snapshot")
            return
        try:
            members = tarfile.open(fileobj=io.BytesIO(raw)).getmembers()
        except Exception as e:
            self._log(f"sync: candidate tar UNREADABLE ({e}) — not overwriting host snapshot")
            return

        has_src = any(m.isfile() and m.size > 0 and not m.name.endswith("compile.sh") and
                      Path(m.name).suffix in (".py", ".c", ".cpp", ".cc", ".h", ".go", ".rs", ".js", ".ts", ".sh")
                      for m in members)
        if not has_src:
            self._log("sync: candidate has NO source file — not overwriting host snapshot")
            return

        names = {m.name.lstrip("./") for m in members if m.isfile()}
        cs_member = next((m for m in members if m.isfile() and m.name.lstrip("./") == "compile.sh"), None)
        compile_sh = ""
        if cs_member is not None:
            try:
                compile_sh = tarfile.open(fileobj=io.BytesIO(raw)).extractfile(cs_member).read().decode("utf-8", "replace")
            except Exception:
                compile_sh = ""
        missing = compile_entrypoints_missing(compile_sh, names)
        if missing:
            self._log(f"sync: CAPTURE ERROR — compile.sh references {sorted(missing)} but the pulled "
                      f"tar is missing them (captured {sorted(names)}); NOT overwriting host snapshot "
                      f"(a truncated/corrupt candidate tar would make the candidate undeliverable)")
            return
        for p in self.sol_dir.glob("*"):
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                import shutil
                shutil.rmtree(p, ignore_errors=True)

        tarfile.open(fileobj=io.BytesIO(raw)).extractall(self.sol_dir)
        self._checkpoint_best()

    def _candidate_tar_cmd(self) -> str:

        import shlex
        return ("cd /workspace && tar cf - "
                + candidate_tar_excludes(getattr(self.room, "baseline_files", set())) + " .")

    def _ensure_env_setup(self) -> None:

        import os
        if os.environ.get("PB_ENV_MODE") != "agent":
            return
        if not self._env_scout_done:
            self._env_scout_done = True
            script = self._run_env_scout(self._fm)
            if script and self._lint_env_setup(script):
                self.room.write_file("/workspace/env_setup.sh", script)
                self._log(f"env-scout: wrote /workspace/env_setup.sh ({len(script)} bytes)")
            elif not script:
                self._log("env-scout: no persistent side-process needed (env travels per-case, Type-P)")
        self.room.apply_env_setup()

    def _run_env_scout(self, fm) -> str:

        docs = (f"TOOL --help:\n{(fm.help_text or '')[:4000]}\n\n"
                f"VERSION: {(fm.version or '').strip()[:120]}\n\n"
                f"README (excerpt):\n{(fm.readme or '')[:3000]}")
        resp = self.llm.messages(prompts.load("env_scout_system"),
                                 [{"role": "user", "content": docs}], None)
        return _extract_env_script(
            " ".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text"))

    def _lint_env_setup(self, script: str) -> bool:
        import re
        s = script or ""
        bad = []
        if ".oracle_ref" in s:
            bad.append("references the oracle (.oracle_ref)")
        if re.search(r"\b(objdump|strace|ltrace|gdb|readelf|nm|strings|xxd|hexdump)\b", s):
            bad.append("binary-inspection tool")
        for tok in ("executable", "compile.sh"):
            if re.search(r"(>>?|\btee\b|\bcp\b|\bmv\b|\brm\b|\bchmod\b)[^\n]*" + re.escape(tok), s):
                bad.append(f"writes candidate artefact {tok!r}")
        for m in re.finditer(r"\b(curl|wget|nc|ncat|ssh|scp|sftp|ftp)\b([^\n]*)", s):
            tail = m.group(2)
            if tail.strip() and "127.0.0.1" not in tail and "localhost" not in tail:
                bad.append(f"network egress via {m.group(1)}")
        for m in re.finditer(r">>?\s*([^\s;|&]+)", s):
            t = m.group(1)
            if t.startswith("/") and not t.startswith(("/env", "/tmp", "/dev/")):
                bad.append(f"redirect outside /env·/tmp: {t}")
        for m in re.finditer(r"\b(?:mkdir(?:\s+-\S+)*|touch|ln\s+-s\S*\s+\S+)\s+([^\s;|&]+)", s):
            t = m.group(1)
            if t.startswith("/") and not t.startswith(("/env", "/tmp")):
                bad.append(f"creates outside /env·/tmp: {t}")
        if bad:
            self._log("env script REJECTED by lint: " + "; ".join(dict.fromkeys(bad))[:300])
            return False
        return True

    def _fresh_endpoint(self, endpoint: str) -> bool:
        if endpoint in self._env_endpoints:
            return False
        from tools import _port_listening
        if _port_listening(self.room, endpoint.rsplit(":", 1)[-1]):
            return False
        return True

    def _append_env_setup(self, prep, setup, endpoint) -> None:

        cur = ""
        if self.room.exec(["test", "-f", "/workspace/env_setup.sh"]).code == 0:
            cur = self.room.read_bytes("/workspace/env_setup.sh").decode("utf-8", "replace")
        block = "\n".join([f"# env-proposal: {endpoint}"] + list(prep) + [setup, ""])
        self.room.write_file("/workspace/env_setup.sh", (cur + "\n" + block) if cur else block)
        self._env_endpoints.add(endpoint)

    def _merge_env_proposals(self, proposals) -> int:

        import os
        if os.environ.get("PB_ENV_MODE") != "agent" or not proposals:
            return 0
        banked = 0
        for p in proposals:
            setup = (p.get("setup") or "").strip()
            endpoint = (p.get("endpoint") or "").strip()
            prep = [c for c in (p.get("prep") or []) if isinstance(c, str)]
            if not (setup and endpoint) or not all(self._lint_env_setup(x) for x in [setup] + prep):
                continue
            if not self._fresh_endpoint(endpoint):
                continue
            self._append_env_setup(prep, setup, endpoint)
            self.room.apply_env_setup()
            ev = p.get("evidence") or {}
            self.box._env_tag = {"needs_env": True, "env_endpoint": endpoint}
            try:
                res = self.box.call("diff", {"args": ev.get("args") or [], "stdin": ev.get("stdin", "")})
            finally:
                self.box._env_tag = None
            if res.get("match") is False and "nondeterministic_not_recorded" not in res:
                banked += 1
        if banked:
            self._log(f"env-proposals: banked {banked} Type-S case(s) after main-room re-confirm")
        return banked

    def _discovery_confidence(self) -> tuple[float, int, int]:

        planned=executed=0
        for row in getattr(self,"hunt_log",[]) or []:
            if not row.get("sweep"): continue
            planned += int(row.get("executed_lenses",0) or 0)+int(row.get("failed_lenses",0) or 0)
            executed += int(row.get("executed_lenses",0) or 0)
        health=(executed/planned) if planned else 0.5

        typed_cases = [c for c in (self.box.corpus or []) if isinstance(c, dict)]
        sources={case_source(c) for c in typed_cases}
        modalities={case_modality(c) for c in typed_cases}
        return health,len(sources),len(modalities)

    def _required_regression_metrics(self, reg: dict) -> dict:
        corpus = list(getattr(getattr(self, "box", None), "corpus", []) or [])
        fallback_total = sum(c.get("required", True) is not False
                             for c in corpus if isinstance(c, dict))
        if not fallback_total and corpus and not all(isinstance(c, dict) for c in corpus):
            fallback_total = len(corpus)
        total = int(reg.get("required_scanned", fallback_total) or 0)
        failed = int(reg.get("required_failed", reg.get("failed", 0)) or 0)
        timeouts = int(reg.get("required_timeouts", reg.get("timeouts", 0)) or 0)
        evaluated = int(reg.get("required_evaluated", total) or 0)
        completion = float(reg.get(
            "required_completion_rate", (evaluated / total if total else 1.0)))
        return {"total": total, "failed": failed, "timeouts": timeouts,
                "evaluated": evaluated, "completion_rate": completion,
                "pass_rate": ((total - failed) / total if total else 1.0)}

    def _worker_env(self, mode: str = "") -> dict:
        import os
        env = dict(os.environ)
        if self.subagent_provider:
            env["PB_SUBAGENT_PROVIDER"] = self.subagent_provider
        if mode == "judge" and self.judge_provider:
            env["PB_JUDGE_PROVIDER"] = self.judge_provider

        env["PB_LLM_TIMING_ROLE"] = mode or "worker"
        return env

    def _run_hunt_worker(self, mode: str, n: int) -> dict:
        import subprocess, tempfile, os

        tar_path = f"/tmp/_pb_hunt_candidate_{mode}.tar"
        try:
            tr = self.room.exec(["sh", "-c", self._candidate_tar_cmd() + f" > {tar_path}"], timeout=120)
            tar_bytes = self.room.read_bytes(tar_path) if tr.code == 0 else b""
            self.room.exec(["rm", "-f", tar_path])
        except Exception as e:
            return {"status": "failed", "error": f"candidate tar capture failed: {e}",
                    "planned_lenses": n, "executed_lenses": 0, "failed_lenses": n}
        if not tar_bytes:
            return {"status": "failed", "error": "candidate tar failed/empty",
                    "planned_lenses": n, "executed_lenses": 0, "failed_lenses": n}

        seeds_path = ""
        work_orders_path = ""
        orders = []
        if mode == "fuzz":
            sf = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
            seed_keys = ("args", "stdin", "stdin_b64", "files", "env", "cwd", "struct_pre",
                         "needs_env", "env_endpoint", "oracle_exit", "oracle_stderr",
                         "oracle_newfiles", "found_by", "anchor")
            seeds = [{k: c[k] for k in seed_keys if c.get(k) is not None and c.get(k) != {}}
                     for c in self.box.corpus]
            sf.write(json.dumps(seeds)); sf.flush(); sf.close()
            seeds_path = sf.name
        if (mode in ("hunt", "critic", "judge") and self.test_discovery_v2
                and getattr(self, "_feature_graph", None)):
            wf = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
            orders = make_work_orders(self._feature_graph, self.box.corpus, 8)
            if self.generic_scenarios_enabled and self._capability_profile:
                orders += generic_capability_orders(self._capability_profile, plan_properties(self._feature_graph))
            orders = self._prepare_work_orders(orders, limit=16)
            json.dump([o.to_dict() for o in orders[:16]], wf)
            wf.flush(); wf.close(); work_orders_path = wf.name
        with tempfile.NamedTemporaryFile(suffix=".tar", delete=True) as tf:
            tf.write(tar_bytes); tf.flush()

            focus = ",".join(self._focus_regions()) if mode in ("hunt", "sweep") else ""

            uncov = ",".join(self._uncovered_flags()) if mode in ("hunt", "sweep", "fuzz") else ""
            cmd = [sys.executable, str(Path(__file__).resolve().parent / "hunt_worker.py"),
                   self.instance_id, tf.name, mode, str(n),
                   str(self.hunt_parallel), str(self.hunter_turns),
                   "1" if self.inject_cargo else "0",
                   str(self.artifact / "subagents"), str(self.hunt_rounds_used),
                   seeds_path, focus, uncov, work_orders_path]
            try:
                p = subprocess.run(cmd, capture_output=True,
                                   env=self._worker_env(mode),
                                   preexec_fn=_limit_worker_memory,
                                   timeout=self.hunter_turns * 90 + 600)
            except subprocess.TimeoutExpired:

                for temp_path in (seeds_path, work_orders_path):
                    if temp_path:
                        try: os.unlink(temp_path)
                        except OSError: pass
                self._log(f"hunt worker mode={mode} TIMED OUT at {self.hunter_turns*90+600}s — "
                          f"treating round as empty (wedge guard)")
                return {"status": "failed", "error": f"worker timeout ({mode})",
                        "planned_lenses": n, "executed_lenses": 0, "failed_lenses": n}
        for temp_path in (seeds_path, work_orders_path):
            if temp_path:
                try: os.unlink(temp_path)
                except OSError: pass
        if p.returncode != 0 or not p.stdout:
            return {"status": "failed", "error": (p.stderr or b"")[-1200:].decode("utf-8", "replace"),
                    "planned_lenses": n, "executed_lenses": 0, "failed_lenses": n}
        try:
            res = json.loads(p.stdout.decode("utf-8", "replace").splitlines()[-1])
        except Exception as e:
            return {"status": "failed", "error": f"parse: {e}",
                    "planned_lenses": n, "executed_lenses": 0, "failed_lenses": n}
        if orders:
            self._update_work_order_lifecycle(res.get("order_results") or [])

        if res.get("usage"):
            impl_prov = self.llm.cfg.provider
            sub_prov = self.subagent_provider or impl_prov
            prov = (self.judge_provider or sub_prov) if mode == "judge" else sub_prov
            with self._usage_lock:
                self.llm.usage.add(res["usage"])
                self.llm.calls += res.get("llm_calls", 0)
                self._ledger.add(prov, res["usage"], res.get("llm_calls", 0))

        if res.get("flaky"):
            with self._usage_lock:
                for fc in res["flaky"]:
                    self.box.record_flaky(fc, reason=fc.get("reason", "oracle non-deterministic"))

        if res.get("divergences"):
            with self._usage_lock:
                self._all_divergences.extend(res["divergences"])
        return res

    def _record_case_drop(self, reason: str, case: dict, **detail) -> None:
        counts = getattr(self, "_case_drop_counts", None)
        if counts is None:
            counts = self._case_drop_counts = {}
        counts[reason] = counts.get(reason, 0) + 1
        try:
            row = {"at": time.time(), "turn": getattr(self, "_turns", 0), "reason": reason,
                   "identity": repr(case_identity(case)), "args": case.get("args") or [],
                   "found_by": case.get("found_by", ""), **detail}
            with (self.artifact / "case_drop_log.jsonl").open("a") as f:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass

    def _semantic_cluster_key(self, case: dict) -> str:
        stored = case.get("sealed_challenge") or {}
        if stored.get("semantic_cluster"):

            return str(stored["semantic_cluster"])
        return repr(merge_signature(case, self._case_region(case)))

    def _persist_sealed_challenge(self) -> None:
        artifact = getattr(self, "artifact", None)
        if artifact is None:
            return
        try:
            target = artifact / "sealed_challenge.json"
            tmp = artifact / ".sealed_challenge.json.tmp"
            tmp.write_text(json.dumps(self._sealed_challenge, ensure_ascii=False, default=str))
            tmp.replace(target)
        except Exception as e:
            self._log(f"sealed challenge persistence failed: {type(e).__name__}: {e}")

    def _route_to_sealed_challenge(self, case: dict) -> bool:
        if (not getattr(self, "sealed_challenge_enabled", False)
                or getattr(self, "sealed_challenge_max", 0) <= 0):
            return False
        source = case_source(case)
        if source not in {"hunt", "critic", "fuzz", "judge"}:
            return False
        corpus = getattr(getattr(self, "box", None), "corpus", None) or []
        if len(corpus) < getattr(self, "sealed_challenge_repair_floor", 0):
            return False
        key = self._semantic_cluster_key(case)
        repair_keys = {self._semantic_cluster_key(c) for c in (self.box.corpus or [])}
        if key in repair_keys:
            return False
        if key in getattr(self, "_sealed_challenge_clusters", set()):
            self._record_case_drop("sealed_cluster_duplicate", case, semantic_cluster=key)
            return True
        if len(getattr(self, "_sealed_challenge", [])) >= self.sealed_challenge_max:
            return False
        import hashlib
        bucket = int(hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()[:8], 16)
        threshold = int(getattr(self, "sealed_challenge_fraction", 0.0) * (2 ** 32))
        if bucket >= threshold:
            return False
        import copy
        frozen = copy.deepcopy(case)
        frozen["sealed_challenge"] = {
            "semantic_cluster": key,
            "source": source,
            "turn": getattr(self, "_turns", 0),
        }
        self._sealed_challenge.append(frozen)
        self._sealed_challenge_clusters.add(key)
        self._persist_sealed_challenge()
        self._record_case_drop("sealed_challenge", case, semantic_cluster=key)
        self._telemetry_add("sealed_challenge", calls=1, planned=1, attempted=1,
                            applicable=1, produced=1)
        return True

    def _active_sealed_challenge(self) -> list[dict]:

        repair_keys = {self._semantic_cluster_key(c) for c in (self.box.corpus or [])}
        active, contaminated = [], []
        for case in getattr(self, "_sealed_challenge", []) or []:
            if self._semantic_cluster_key(case) in repair_keys:
                contaminated.append(case)
            else:
                active.append(case)
        if contaminated:
            self._sealed_challenge_contaminated = (
                getattr(self, "_sealed_challenge_contaminated", 0) + len(contaminated))
            try:
                with (self.artifact / "sealed_challenge_contaminated.jsonl").open("a") as f:
                    for case in contaminated:
                        f.write(json.dumps({
                            "at": time.time(), "turn": getattr(self, "_turns", 0),
                            "identity": repr(case_identity(case)),
                            "semantic_cluster": self._semantic_cluster_key(case),
                            "reason": "semantic_cluster_entered_repair_corpus",
                        }, ensure_ascii=False) + "\n")
            except Exception:
                pass
            self._sealed_challenge = active
            self._sealed_challenge_clusters = {
                self._semantic_cluster_key(c) for c in active
            }
            self._persist_sealed_challenge()
        return active

    def _merge_cases(self, cases: list[dict], cap: int | None = None, diverse: bool = False) -> int:
        before = len(self.box.corpus)
        if diverse and cap is not None:
            seen_sig = set()
            primary, secondary = [], []
            for c in cases:
                sig = self._case_sig(c)
                (primary if sig not in seen_sig else secondary).append(c)
                seen_sig.add(sig)
            cases = primary + secondary

        from collections import Counter
        def _merge_sig(case):
            region = self._case_region(case)
            return (merge_signature(case, region) if getattr(self, "case_merge_v2", True)
                    else self._flag_sig(case))
        sig_count = Counter(_merge_sig(c) for c in self.box.corpus)
        per_sig_cap = getattr(self, "per_sig_cap", 3)

        region_count = Counter(self._case_region(c) for c in self.box.corpus)
        surface_count = sum(is_trivial_surface_case(c) for c in self.box.corpus)
        non_root_surface_count = sum(is_trivial_surface_case(c) and not is_root_surface_case(c)
                                     for c in self.box.corpus)
        root_surface_count = sum(is_root_surface_case(c) for c in self.box.corpus)
        per_region_cap = getattr(self, "per_region_cap", 25)
        top_region_cap = getattr(self, "top_region_cap", 12)
        surface_case_cap = getattr(self, "surface_case_cap", 16)
        surface_root_reserve = min(5, max(1, surface_case_cap // 3))

        has_subs = bool(getattr(getattr(self, "_fm", None), "subcommands", []) or [])

        quality_control = os.environ.get("PB_CORPUS_QUALITY_CONTROL", "1") != "0"
        banked = 0
        for c in cases:
            if cap is not None and banked >= cap:
                break

            if self._route_to_sealed_challenge(c):
                continue
            reg = self._case_region(c)
            s = _merge_sig(c)
            if quality_control and is_trivial_surface_case(c):
                at_non_root_cap = (not is_root_surface_case(c)
                                   and non_root_surface_count >=
                                   max(0, surface_case_cap - surface_root_reserve))
                reserved_root_admission = (is_root_surface_case(c)
                                           and root_surface_count < surface_root_reserve)
                if ((surface_count >= surface_case_cap and not reserved_root_admission)
                        or at_non_root_cap):
                    self._record_case_drop("surface_case_cap", c, cap=surface_case_cap)
                    continue
            cap_for_sig = (signature_cap(s, error_cap=per_sig_cap,
                                         success_cap=getattr(self, "success_sig_cap", 12))
                           if getattr(self, "case_merge_v2", True) else per_sig_cap)
            if quality_control and sig_count[s] >= cap_for_sig:
                self._record_case_drop("signature_cap", c, signature=s, cap=cap_for_sig)
                continue

            if quality_control and not (reg == "(top)" and not has_subs):
                limit = (top_region_cap if (reg == "(top)" and has_subs)
                         else self._region_cap(reg, per_region_cap))
                if region_count[reg] >= limit:
                    self._record_case_drop("region_cap", c, region=reg, cap=limit)
                    continue

            if len(self.box.corpus) >= getattr(self, "corpus_target", 350):
                self._evict_one_redundant()
            self.box._record(c)
            sig_count[s] += 1
            region_count[reg] += 1
            surface_count += int(is_trivial_surface_case(c))
            non_root_surface_count += int(is_trivial_surface_case(c)
                                          and not is_root_surface_case(c))
            root_surface_count += int(is_root_surface_case(c))
            banked += 1
            self._region_divergences[reg] = self._region_divergences.get(reg, 0) + 1
        return len(self.box.corpus) - before

    def _evict_one_redundant(self) -> bool:

        corpus = self.box.corpus
        from collections import Counter
        def sig(c):
            return (merge_signature(c, self._case_region(c)) if getattr(self, "case_merge_v2", True)
                    else self._flag_sig(c))
        counts = Counter(sig(c) for c in corpus)
        for i, c in enumerate(corpus):
            cap = (signature_cap(sig(c), error_cap=getattr(self, "per_sig_cap", 3),
                                 success_cap=getattr(self, "success_sig_cap", 12))
                   if getattr(self, "case_merge_v2", True) else getattr(self, "per_sig_cap", 3))
            if counts[sig(c)] <= cap:
                continue
            if (c.get("anchor") or c.get("crash_matchable") or c.get("struct_divergence")
                    or str(c.get("found_by", "")).startswith("doc")):
                continue
            del corpus[i]
            return True
        return False

    def _region_cap(self, region: str, base: int) -> int:
        import os as _os

        if _os.environ.get("PB_REGION_WEIGHTED_CAP", "0") != "1":
            return base
        weights = self._region_obligation_weights()
        if len(weights) < 2 or region not in weights:
            return base
        scaled = base * weights[region] * len(weights)
        return max(max(6, base // 3), min(int(round(scaled)), base * 3))

    def _region_obligation_weights(self) -> dict[str, float]:

        if getattr(self, "_region_weights_cache", None) is not None:
            return self._region_weights_cache
        counts: dict[str, float] = {}
        for ob in getattr(getattr(self, "_feature_graph", None), "obligations", []) or []:
            path = getattr(ob, "feature_path", None) or "(root)"
            counts[("(top)" if path == "(root)" else path)] = counts.get(
                "(top)" if path == "(root)" else path, 0) + 1
        total = sum(counts.values())
        self._region_weights_cache = ({k: v / total for k, v in counts.items()} if total else {})
        return self._region_weights_cache

    def _case_region(self, c: dict) -> str:
        subs = set(getattr(getattr(self, "_fm", None), "subcommands", []) or [])
        if not subs:
            return "(top)"
        for a in (c.get("args") or []):
            if str(a) in subs:
                return str(a)
        return "(top)"

    def _focus_regions(self, max_regions: int = 6) -> list[str]:
        ranked = sorted(self._region_divergences.items(), key=lambda kv: kv[1], reverse=True)
        focus = [r for r, n in ranked if n > 0 and r != "(top)"]
        documented = list(getattr(getattr(self, "_fm", None), "subcommands", []) or [])
        uncovered = [s for s in documented if s not in self._region_divergences]

        depth_target = getattr(self, "region_depth_target", 8)
        from collections import Counter
        region_n = Counter(self._case_region(c) for c in (getattr(self, "box", None).corpus
                                                           if getattr(self, "box", None) else []))
        shallow = [s for s in documented
                   if s not in uncovered and region_n.get(s, 0) < depth_target]

        out: list[str] = []
        for r in focus + uncovered + shallow:
            if r not in out:
                out.append(r)
        return out[:max_regions]

    _TRIVIAL_FLAGS = {"-h", "--help", "--version", "-V", "--unknown", "-Z"}

    def _graph_brief_text(self) -> str:

        graph = getattr(self, "_feature_graph", None)
        if graph is None:
            return ""
        exercised = {(self._case_region(c), str(a).split("=", 1)[0])
                     for c in (self.box.corpus if getattr(self, "box", None) else [])
                     for a in (c.get("args") or [])}

        subs = {p: {f for f in n.flags if f not in self._TRIVIAL_FLAGS}
                for p, n in graph.nodes.items()
                if p != "(root)" and not p.startswith("(behavior)")}
        globals_ = set.intersection(*subs.values()) if len(subs) > 1 else set()
        lines = ["# THE MAP AS IT STANDS", ""]
        if globals_:
            lines += [f"## GLOBAL options (accepted by all {len(subs)} subcommands)",
                      "known flags: " + " ".join(sorted(globals_)), ""]
        unprobed: list[str] = []
        for path, node in sorted(graph.nodes.items()):
            if path.startswith("(behavior)"):
                continue
            flags = [f for f in node.flags if f not in self._TRIVIAL_FLAGS]
            own = [f for f in flags if f not in globals_] if path != "(root)" else flags
            lines.append(f"## {path}   ({len(own)} own flags"
                         + (f", plus the {len(globals_)} global ones)" if globals_ and path != "(root)"
                            else ")"))
            if own:
                lines.append("known flags: " + " ".join(own[:80]))
            for flag in own:
                if (path, flag) not in exercised:
                    unprobed.append(f"{path} {flag}")
            enums = {f: v for f, v in (node.enum_values or {}).items() if v}
            if enums:
                lines.append("known values: " + "; ".join(f"{f}={'|'.join(v[:8])}"
                                                          for f, v in list(enums.items())[:20]))
            lines.append("")
        if unprobed:
            lines += ["# NEVER PROBED YET — the deterministic pass could not reach these",
                      f"# ({len(unprobed)} of them; it probes bare presence only, never arguments)", ""]
            lines += ["  " + u for u in unprobed[:400]]
            if len(unprobed) > 400:
                lines.append(f"  … and {len(unprobed) - 400} more")
        text = "\n".join(lines)
        (self.artifact / "graph_brief.md").write_text(text)
        return text

    def _probe_graph_stage(self) -> None:

        if os.environ.get("PB_PROBE_GRAPH") != "1" or self._feature_graph is None:
            return
        started = time.time()
        budget = float(os.environ.get("PB_PROBE_GRAPH_SECONDS", "5400"))

        budget = max(60.0, min(budget, self.deadline - time.time() - 600))
        findings, status, err, turns, r = {}, "ok", None, 0, None

        _prev_role = os.environ.get("PB_LLM_TIMING_ROLE")
        try:
            from subagents import PROBER_SYSTEM, run_subagent
            from hunt_worker import _extract_probe_findings
            brief = self._graph_brief_text()
            self.room.write_file("/workspace/.pb_graph_brief.md", brief)
            seed = (brief + f"\n\nYou have about {int(budget / 60)} minutes of wall clock. Probe now, "
                    "highest-value first. Respect the evidence rule: report ONLY what the oracle's own "
                    "response proved. Finish with the JSON block and the FEATURE_PROBING_COMPLETE line.")

            os.environ["PB_LLM_TIMING_ROLE"] = "prober"
            r = run_subagent(
                PROBER_SYSTEM, seed, self.room, self.llm,

                max_turns=int(os.environ.get("PB_PROBE_GRAPH_TURNS", "400")),
                deadline=started + budget,
                nearend_warn_s=float(os.environ.get("PB_PROBE_GRAPH_WARN_S", "900")),
                nearend_note=("TIME CHECK: about {left} minutes left before your budget ends. Stop opening "
                              "new lines of investigation. Emit your JSON block NOW with everything you "
                              "have already confirmed — unreported findings are lost — then the "
                              "FEATURE_PROBING_COMPLETE line."),
                final_note=("Your probing budget is now spent. Do not call any more tools. Write your "
                            "final report NOW as the JSON block described in your instructions, "
                            "containing every flag / enum value / subcommand you CONFIRMED against the "
                            "oracle during this session, then the FEATURE_PROBING_COMPLETE line. "
                            "Anything you do not write down here is lost."))
            turns = r.get("turns") or 0
            findings = _extract_probe_findings(r.get("summary") or "", r.get("transcript") or [])
            declared = "FEATURE_PROBING_COMPLETE" in json.dumps(
                [r.get("summary") or "", r.get("transcript") or []])

            from hunt_worker import _dump_capped, _write_atif_worker
            sub = self.artifact / "subagents"; sub.mkdir(parents=True, exist_ok=True)
            blob = {"mode": "probe_graph", "round": 0, "lens": "graph-completion",
                    "turns": turns, "summary": r.get("summary"),
                    "findings": findings, "transcript": r.get("transcript"),

                    "divergences": r.get("divergences") or []}
            _dump_capped(sub / "probe_graph_round0.json", blob)
            _write_atif_worker(sub, "probe_graph_round0", blob, self.instance_id)
        except BaseException as e:
            status, declared, err = "failed", False, f"{type(e).__name__}: {str(e)[:600]}"

            try:
                from hunt_worker import _dump_capped
                sub = self.artifact / "subagents"; sub.mkdir(parents=True, exist_ok=True)
                _dump_capped(sub / "probe_graph_round0.json",
                             {"mode": "probe_graph", "round": 0, "lens": "graph-completion",
                              "status": "failed", "error": err,
                              "turns": (r or {}).get("turns") if isinstance(r, dict) else None,
                              "summary": (r or {}).get("summary") if isinstance(r, dict) else None,
                              "transcript": (r or {}).get("transcript") if isinstance(r, dict) else None})
            except Exception:
                pass

        if _prev_role is None:
            os.environ.pop("PB_LLM_TIMING_ROLE", None)
        else:
            os.environ["PB_LLM_TIMING_ROLE"] = _prev_role
        from feature_graph import absorb_probe_findings
        applied = absorb_probe_findings(
            self._feature_graph, findings,
            max_new=int(os.environ.get("PB_PROBE_GRAPH_MAX_NEW", "60")),
            hard_feature_debt=os.environ.get("PB_HARD_FEATURE_DEBT") == "1")
        graph = self._feature_graph
        _write_json(self.artifact / "probe_graph.json",
                    {"status": status, "error": err,
                     "wall_seconds": round(time.time() - started, 1), "budget_seconds": round(budget, 1),

                     "declared_complete": declared, "turns": turns,
                     "findings": findings, "applied": applied,
                     "graph_after": {"nodes": len(graph.nodes), "obligations": len(graph.obligations),
                                     "flags": sum(len(n.flags) for n in graph.nodes.values())}}, indent=2)
        _write_json(self.artifact / "featuregraph.json", graph.to_dict(), indent=2)
        self._log(f"probe_graph: {status} in {time.time() - started:.0f}s ({turns} turns) — "
                  f"accepted {len(applied['accepted'])} flags, {applied['enum_values_added']} enum values, "
                  f"rejected {len(applied['rejected'])}, capped {applied['capped']}, "
                  f"declared_complete={declared}" + (f" — {err}" if err else ""))

    def _uncovered_flags(self, max_flags: int = 12) -> list[str]:
        import os
        if os.environ.get("PB_DISABLE_UNCOVERED_FLAGS") == "1":
            return []
        fm = getattr(self, "_fm", None)
        documented = list(getattr(fm, "flags", []) or [])
        if not documented:
            return []
        present = set()
        for c in (self.box.corpus if getattr(self, "box", None) else []):
            for a in (c.get("args") or []):
                a = str(a)
                if a.startswith("-"):
                    present.add(a.split("=", 1)[0])
        uncovered = [f for f in documented
                     if f not in present and f not in self._TRIVIAL_FLAGS]
        return uncovered[:max_flags]

    @staticmethod
    def _flag_sig(c: dict) -> tuple:
        import re
        args = c.get("args") or []
        flags = []
        for a in args:
            a = str(a)
            if a.startswith("-"):
                name = a.split("=", 1)[0]
                flags.append(name)
        err = (c.get("oracle_stderr") or "")

        ecls = " ".join(re.findall(r"[A-Za-z]+", err)[:5]).lower()
        return (tuple(sorted(set(flags))), c.get("oracle_exit"), ecls)

    @staticmethod
    def _case_sig(c: dict) -> tuple:
        err = (c.get("oracle_stderr") or "")[:40]

        for marker in ("invalid character", "unexpected EOF", "cannot unmarshal",
                       "Time.UnmarshalJSON", "looking for beginning"):
            if marker in (c.get("oracle_stderr") or ""):
                err = marker; break
        return (tuple(c.get("args") or []), c.get("oracle_exit"), err)

    def _seed_diversity(self) -> int:

        shapes = set()
        for c in self.box.corpus:
            flags = tuple(sorted(a for a in (c.get("args") or []) if str(a).startswith("-")))
            keys = ()
            sd = c.get("stdin") or ""
            try:
                obj = json.loads(sd.splitlines()[0]) if sd.strip() else {}
                if isinstance(obj, dict):
                    keys = tuple(sorted(obj.keys()))
            except (ValueError, IndexError):
                keys = ("<non-json>",) if sd.strip() else ()
            shapes.add((flags, keys))
        return len(shapes)

    def _sweep_note(self, real: int, added: int, found_label: str) -> str:
        if added:
            return (f"{found_label}: {real} real divergence(s) found, {added} NEW case(s) added to "
                    "the regression corpus. Run `run_regression` to see which FAIL, then fix each "
                    "(edit the source with `bash`).")
        if real:
            return (f"{found_label}: {real} real divergence(s) found, but 0 added — they are ALREADY "
                    "covered by your corpus (duplicates) or hit a per-signature/region sampling cap. "
                    "Nothing new to fix; do NOT run_regression expecting FAILs. Keep sweeping for "
                    "uncovered behaviour, or submit if you believe you match the oracle everywhere.")
        return (f"{found_label}: no real divergences — your candidate matches the oracle on "
                "everything probed this round.")

    def _hunt(self, reason: str) -> dict:
        if self.hunters <= 0 or self.hunt_rounds_used >= self.max_hunt_rounds:
            return {"ran": False, "reason": "hunters disabled or round budget exhausted"}
        from monitor import _mem_available_gb
        avail = _mem_available_gb()
        if avail < self.hunt_min_free_gb:
            return {"ran": False, "reason": f"insufficient free RAM ({avail:.0f}G < "
                    f"{self.hunt_min_free_gb}G) — skipping hunt to protect the host"}
        self.hunt_rounds_used += 1
        self._sync_solution_out()
        res = self._run_hunt_worker("hunt", self.hunters)
        self._merge_env_proposals(res.get("env_proposals") or [])
        added = self._merge_cases(res.get("divergences", []), cap=getattr(self, "sweep_intake_cap", 20), diverse=True)
        real = res.get("n_real_divergences", 0)
        real += self._fuzz_round()
        self.hunt_log.append({"round": self.hunt_rounds_used, "trigger": reason,
                              "new_cases": added, "real_divergences": real,
                              "per_lens": res.get("per_lens"),
                              "worker_error": res.get("error")})
        return {"ran": True, "new_divergence_cases": added, "real_divergences": real,
                "hunt_round": self.hunt_rounds_used, "of_max": self.max_hunt_rounds,
                "note": self._sweep_note(real, added, "Hunters")}

    def _critic(self, reason: str) -> dict:
        if self.critics <= 0 or self.hunt_rounds_used >= self.max_hunt_rounds:
            return {"ran": False, "reason": "critics disabled or round budget exhausted"}
        from monitor import _mem_available_gb
        if _mem_available_gb() < self.hunt_min_free_gb:
            return {"ran": False, "reason": "insufficient free RAM — skipping critic"}
        self.hunt_rounds_used += 1
        self._sync_solution_out()
        res = self._run_hunt_worker("critic", self.critics)
        self._merge_env_proposals(res.get("env_proposals") or [])
        added = self._merge_cases(res.get("divergences", []), cap=getattr(self, "sweep_intake_cap", 20), diverse=True)
        real = res.get("n_real_divergences", 0)
        real += self._fuzz_round()
        self.hunt_log.append({"critic": True, "round": self.hunt_rounds_used, "trigger": reason,
                              "new_cases": added, "real_divergences": real,
                              "per_lens": res.get("per_lens"), "worker_error": res.get("error")})
        return {"ran": True, "real_divergences": real, "new_divergence_cases": added,
                "note": self._sweep_note(real, added, "Completeness critic (undocumented behaviour)")}

    def _rank_and_intake_sweep_cases(self, out: dict, total_cap: int) -> tuple[int, list[dict]]:
        if not getattr(self, "test_discovery_v2", True):
            added = 0; remaining = total_cap; decisions = []
            for mode, res in out.items():
                if remaining <= 0: break
                n = self._merge_cases(res.get("divergences", []), cap=remaining, diverse=True)
                added += n; remaining -= n
                decisions.append({"source": mode, "legacy_banked": n})
            return added, decisions
        existing = {merge_signature(c, self._case_region(c)) for c in self.box.corpus}
        uncovered = set(self._uncovered_flags(max_flags=1000))
        if getattr(self, "intake_backlog_recovery", False) and self._intake_backlog:

            corpus_ids = {repr(case_identity(c)) for c in self.box.corpus}
            for k in [k for k in self._intake_backlog if k in corpus_ids]:
                self._intake_backlog.pop(k, None)
            if self._intake_backlog:
                out = {**out, "backlog": {"divergences": [e["case"]
                                                          for e in self._intake_backlog.values()]}}
        ranked_by_source: dict[str, list] = {}; all_ranked = []
        for mode, res in out.items():
            source = "fuzz" if mode == "fuzz" else mode
            vals = [rank_case(c, region=self._case_region(c), existing_signatures=existing,
                              uncovered_flags=uncovered) for c in res.get("divergences", [])]
            vals.sort(key=lambda x: (-x.priority, repr(case_identity(x.case))))
            ranked_by_source[source] = vals
            all_ranked.extend((source, x) for x in vals)
        quotas = allocate_source_quotas(total_cap, set(ranked_by_source))
        ordered = sorted(all_ranked, key=lambda x: (-x[1].priority,
                                                     repr(case_identity(x[1].case))))
        import math
        success_target = max(1, math.ceil(total_cap * .35))
        state_success_target = max(1, math.ceil(total_cap * .20))

        def useful_success(case: dict) -> bool:
            if int(case.get("oracle_exit", 0) or 0) != 0:
                return False

            if os.environ.get("PB_INTAKE_COUNT_SURFACE") == "1":
                return True
            return not is_trivial_surface_case(case)

        def state_success(case: dict) -> bool:
            return (useful_success(case)
                    and case_modality(case) in ("artifact", "stateful", "file"))

        added = 0; decisions = []; attempted_ids = set(); accepted = []
        def accept(source, ranked, phase):
            nonlocal added
            ident = case_identity(ranked.case)
            if ident in attempted_ids or added >= total_cap:
                return False
            attempted_ids.add(ident)
            n = self._merge_cases([ranked.case], cap=1, diverse=False)
            added += n
            if n:
                accepted.append((source, ranked.case))
                if getattr(self, "intake_backlog_recovery", False):
                    self._intake_backlog.pop(repr(ident), None)
            decisions.append({"source": source, "priority": ranked.priority,
                              "reason": list(ranked.reason), "phase": phase,
                              "banked": bool(n), "args": ranked.case.get("args") or []})
            return bool(n)

        for source, ranked in (x for x in ordered if state_success(x[1].case)):
            if sum(state_success(c) for _, c in accepted) >= state_success_target:
                break
            accept(source, ranked, "state-success-floor")
        for source, ranked in (x for x in ordered if useful_success(x[1].case)):
            if sum(useful_success(c) for _, c in accepted) >= success_target:
                break
            accept(source, ranked, "success-floor")
        for source, quota in quotas.items():
            for ranked in ranked_by_source.get(source, []):
                if sum(s == source for s, _ in accepted) >= quota or added >= total_cap:
                    break
                accept(source, ranked, "source-quota")
        for source, ranked in ordered:
            if added >= total_cap:
                break
            accept(source, ranked, "global-fill")

        backlog_on = getattr(self, "intake_backlog_recovery", False) and self.intake_backlog_max
        for source, ranked in all_ranked:
            if case_identity(ranked.case) not in attempted_ids:
                self._record_case_drop("intake_quota", ranked.case, source=source,
                                       priority=ranked.priority, rank_reason=ranked.reason)
                if backlog_on:
                    self._intake_backlog[repr(case_identity(ranked.case))] = {
                        "case": ranked.case, "priority": ranked.priority}
        if backlog_on and len(self._intake_backlog) > self.intake_backlog_max:
            for k in sorted(self._intake_backlog,
                            key=lambda k: self._intake_backlog[k]["priority"]
                            )[:len(self._intake_backlog) - self.intake_backlog_max]:
                self._intake_backlog.pop(k, None)
        accepted_useful = sum(useful_success(c) for _, c in accepted)
        accepted_state = sum(state_success(c) for _, c in accepted)
        try:
            with (self.artifact / "test_discovery_events.jsonl").open("a") as f:
                f.write(json.dumps({"at": time.time(), "turn": self._turns,
                                    "kind": "sweep_intake", "cap": total_cap,
                                    "quotas": quotas,
                                    "portfolio_targets": {"useful_success": success_target,
                                                          "state_success": state_success_target},
                                    "portfolio_accepted": {
                                        "useful_success": accepted_useful,
                                        "state_success": accepted_state},
                                    "portfolio_unmet": {
                                        "useful_success": max(0, success_target - accepted_useful),
                                        "state_success": max(0, state_success_target - accepted_state)},
                                    "decisions": decisions},
                                   ensure_ascii=False, default=str) + "\n")
        except Exception: pass
        return added, decisions

    def _telemetry_add(self, component: str, **counts) -> None:
        telemetry = getattr(self, "_component_telemetry", None)
        if telemetry is None:
            telemetry = self._component_telemetry = {}
        row = telemetry.setdefault(component, {
            "calls": 0, "planned": 0, "attempted": 0, "applicable": 0,
            "produced": 0, "accepted_into_corpus": 0, "fixed": 0,
            "still_failing": 0, "wall_seconds": 0.0})
        for key, value in counts.items():
            row[key] = row.get(key, 0) + value

    def _write_work_order_lifecycle(self) -> None:
        try:
            rows = sorted(getattr(self, "_work_order_lifecycle", {}).values(),
                          key=lambda x: (-int(x.get("priority", 0)), x.get("order_id", "")))
            (self.artifact / "work_order_lifecycle.json").write_text(
                json.dumps({"schema_version": "1.0", "orders": rows}, indent=2,
                           ensure_ascii=False, default=str))
        except Exception:
            pass

    def _update_work_order_lifecycle(self, results: list[dict]) -> None:
        if not results:
            return
        lock = getattr(self, "_usage_lock", None)
        def update():
            ledger = getattr(self, "_work_order_lifecycle", None)
            if ledger is None:
                ledger = self._work_order_lifecycle = {}
            totals = {"attempted": 0, "applicable": 0, "produced": 0, "still_failing": 0}
            for event in results:
                oid = event.get("order_id")
                row = ledger.get(oid)
                if not oid or row is None:
                    continue
                for key in ("attempted", "applicable", "blocked"):
                    inc = int(bool(event.get(key)))
                    row[key] = int(row.get(key, 0)) + inc
                found = int(event.get("found", 0) or 0)
                row["found"] = int(row.get("found", 0)) + found
                row["last_reason"] = str(event.get("reason") or "")[:300]
                row["last_mode"] = event.get("mode")
                row["last_round"] = getattr(self, "hunt_rounds_used", 0)
                if found:
                    row["status"] = "found"
                elif (row["attempted"] >= int(row.get("max_attempts", 2))
                      or row["blocked"] >= 3):
                    row["status"] = "exhausted"
                elif event.get("blocked"):
                    row["status"] = "blocked"
                elif event.get("applicable"):
                    row["status"] = "applicable"
                else:
                    row["status"] = "attempted"
                totals["attempted"] += int(bool(event.get("attempted")))
                totals["applicable"] += int(bool(event.get("applicable")))
                totals["produced"] += found
                totals["still_failing"] += int(row["status"] == "exhausted" and not row["found"])
            self._telemetry_add("work_orders", **totals)
        if lock:
            with lock:
                update()
        else:
            update()
        self._write_work_order_lifecycle()

    @staticmethod
    def _case_provenance_components(case: dict) -> set[str]:
        components: set[str] = set()
        prefix = str(case.get("found_by") or "").split(":", 1)[0].lower()
        aliases = {"hunter": "hunt", "hunt": "hunt", "critic": "critic",
                   "judge": "judge", "fuzz": "fuzz", "scenario": "scenario",
                   "metamorphic": "metamorphic"}
        if prefix in aliases:
            components.add(aliases[prefix])
        if case.get("scenario"):
            components.add("scenario")
        if case.get("metamorphic"):
            components.add("metamorphic")
        if case.get("work_order_id"):
            components.add("work_orders")
        if case.get("minimized_from"):
            components.add("minimizer")
        if case.get("executor") == "pty_screen":
            components.add("pty")
        if case.get("oracle_newfiles") or case.get("oracle_files"):
            components.add("artifact")
        return components

    def _component_case_outcome_snapshot(self) -> dict:

        import hashlib
        corpus = list(getattr(getattr(self, "box", None), "corpus", []) or [])
        outcomes = getattr(self, "_final_regression_outcomes", None)
        source = "final_candidate_replay"
        if outcomes is None:
            cache = getattr(getattr(self, "box", None), "_reg_cache", None) or {}
            outcomes = cache.get("case_outcomes") or []
            source = "current_regression_cache"
        outcome_by_id = {str(row.get("identity")): row for row in outcomes or []}
        rows: dict[str, dict] = {}
        for case in corpus:
            identity = repr(case_identity(case))
            outcome = outcome_by_id.get(identity)
            case_id = hashlib.sha256(identity.encode("utf-8", "surrogateescape")).hexdigest()[:16]
            for component in self._case_provenance_components(case):
                row = rows.setdefault(component, {
                    "accepted": 0, "evaluated": 0, "fixed_current": 0,
                    "still_failing_current": 0, "unevaluated": 0, "case_ids": []})
                row["accepted"] += 1
                row["case_ids"].append(case_id)
                if not outcome or not outcome.get("evaluated"):
                    row["unevaluated"] += 1
                elif outcome.get("passed") is True:
                    row["evaluated"] += 1
                    row["fixed_current"] += 1
                else:
                    row["evaluated"] += 1
                    row["still_failing_current"] += 1
        return {
            "schema_version": "1.0",
            "source": source,
            "source_digest": self._snapshot_source_digest(
                getattr(self, "sol_dir", Path("/__pb_no_candidate__"))),
            "candidate_id": getattr(self, "_final_selected_candidate_id", None),
            "outcome_count": len(outcomes or []),
            "components": rows,
        }

    def _sweep(self, reason: str) -> dict:
        import threading
        if self.hunt_rounds_used >= self.max_hunt_rounds:
            return {"ran": False, "reason": "successful round budget exhausted"}
        from monitor import _mem_available_gb
        if _mem_available_gb() < self.hunt_min_free_gb:
            return {"ran": False, "reason": "insufficient free RAM — skipping sweep"}
        self._sweep_attempts = getattr(self, "_sweep_attempts", 0) + 1
        next_round = self.hunt_rounds_used + 1
        self._heartbeat(f"sweep attempt {self._sweep_attempts} / successful round {next_round}: {reason}")
        self._sync_solution_out()
        clone = self._check_subagent_clone_build()
        if not clone.get("ok"):
            self.hunt_log.append({"sweep": True, "attempt": self._sweep_attempts,
                "round": next_round, "trigger": reason, "status": "clone_build_failed", "executed_lenses": 0,
                "failed_lenses": self.hunters + self.critics + int(bool(self.fuzz_seeds)),
                "new_cases": 0, "real_divergences": 0, "clone_build_error": clone.get("error")})
            self._telemetry_add("clone_build", calls=1, planned=1, attempted=1, still_failing=1)
            self._write_component_telemetry()
            return {"ran": False, "status": "clone_build_failed", "new_divergence_cases": 0,
                    "real_divergences": 0, "note": ("Coverage team cannot run because the candidate "
                    "does not build in a fresh subagent cleanroom: " + clone.get("error", ""))}

        self.hunt_rounds_used = next_round
        self._run_scenario_schedule(f"sweep:{reason}")
        self._run_metamorphic_schedule(f"sweep:{reason}")
        out = {}
        def run_kind(mode, n):
            if n > 0:
                out[mode] = self._run_hunt_worker(mode, n)

        kinds = [("hunt", self.hunters), ("critic", self.critics)]
        if self.fuzz_seeds > 0 and self.box.corpus:
            kinds.append(("fuzz", self.fuzz_seeds))
        threads = [threading.Thread(target=run_kind, args=(m, n)) for m, n in kinds]
        for t in threads: t.start()

        _join_ceiling = getattr(self, "hunter_turns", 14) * 90 + 900
        for t in threads: t.join(timeout=_join_ceiling)
        alive = [t for t in threads if t.is_alive()]
        if alive:
            self._log(f"sweep round {self.hunt_rounds_used}: {len(alive)} worker thread(s) exceeded "
                      f"{_join_ceiling}s join ceiling — abandoning, continuing with what completed (wedge guard)")

        for res in out.values():
            self._merge_env_proposals(res.get("env_proposals") or [])
        added = 0; real = 0; per_lens = []

        for mode, res in out.items():
            self._record_subagents(mode, res.get("per_lens") or [])
            found = int(res.get("n_real_divergences", 0) or 0)
            self._telemetry_add(mode, calls=1, planned=int(res.get("planned_lenses", 0) or 0),
                                attempted=int(res.get("executed_lenses", 0) or 0),
                                produced=found)
            real += found
            per_lens += (res.get("per_lens") or [])
        added, intake_decisions = self._rank_and_intake_sweep_cases(
            out, getattr(self, "sweep_intake_cap", 20))
        self._telemetry_add("sweep_intake", accepted_into_corpus=added)
        self._write_component_telemetry()
        health = summarize_worker_results(list(out.values()))
        sweep_status = health["status"]
        if health["executed_lenses"] > 0:
            self._sweeps_executed = getattr(self, "_sweeps_executed", 0) + 1
            if health["failed_lenses"] == 0:
                self._sweeps_done = getattr(self, "_sweeps_done", 0) + 1
                self._last_sweep_at = time.time()
        else:

            self.hunt_rounds_used = max(0, self.hunt_rounds_used - 1)
        self.hunt_log.append({"sweep": True, "attempt": self._sweep_attempts,
                              "round": (self.hunt_rounds_used if health["executed_lenses"] > 0 else next_round), "trigger": reason,
                              "status": sweep_status,
                              "executed_lenses": health["executed_lenses"],
                              "failed_lenses": health["failed_lenses"],
                              "hunters": self.hunters, "critics": self.critics,
                              "new_cases": added, "real_divergences": real,
                              "intake_decisions": intake_decisions, "per_lens": per_lens})
        self._consecutive_dry_sweeps = (self._consecutive_dry_sweeps + 1
                                        if ((sweep_status == "clean") if getattr(self, "test_discovery_v2", True)
                                            else real == 0) else 0)
        if sweep_status in ("partial", "failed"):
            note = (f"Coverage sweep {sweep_status.upper()}: only {health['executed_lenses']} "
                    f"lens(es) executed; {health['failed_lenses']} failed due to sandbox/build/timeout. "
                    "This is INCONCLUSIVE, not a clean match. Do not treat it as convergence.")
        else:
            note = self._sweep_note(real, added, "Coverage sweep (hunters+critics)")
        return {"ran": True, "status": sweep_status,
                "executed_lenses": health["executed_lenses"],
                "failed_lenses": health["failed_lenses"],
                "real_divergences": real, "new_divergence_cases": added,
                "hunt_round": self.hunt_rounds_used, "of_max": self.max_hunt_rounds,
                "note": note}

    def run(self) -> dict:
        import os
        from monitor import snapshot
        ok, line = snapshot()
        if not ok:
            return {"error": f"resource guard tripped at start: {line}"}
        self._prune_stale_clones()

        os.environ["PB_SANDBOX_ID_LOG"] = str(self.artifact / "sandboxes.jsonl")

        os.environ["PB_LLM_TIMING_LOG"] = str(self.artifact / "llm_timings.jsonl")

        os.environ["PB_EXEC_TIMING_LOG"] = str(self.artifact / "exec_timings.jsonl")
        self.room = make_room(self.instance_id, cpus=self.docker_cpus,
                              memory=os.environ.get("PB_MAIN_MEM_GB", "10") + "g",
                              inject_cargo=self.inject_cargo, role="main")
        try:
            self.room.start()
            self._log(f"cleanroom started: {self.room.name}")
            self.box = ToolBox(self.room, self._exam,
                               max_submissions=self.max_submissions,
                               corpus_path=self.artifact / "corpus.json",
                               reg_parallel=self.reg_parallel, reg_timeout=self.reg_timeout)
            self.box.surface_case_cap = self.surface_case_cap
            self.box.regression_guard_feedback = getattr(self, "regression_guard", True)
            if self.resume_from:

                self.box.corpus = self._resume_corpus
                self.box.submissions = list(self._resume_submissions)
                self.box._reg_dirty = True
                self.hunt_rounds_used = self._resume_hunt_rounds
                self._dev_runs_used = self._resume_dev_runs
                if self._dev_runs_used:
                    self._last_dev_turn = self._resume_turn
            self._seed_candidate()
            self._seed_fixture_probes()
            fm = mine_docs(self.room)
            self._fm = fm
            if getattr(self, "bootstrap_artifact", None):

                self._inject_bootstrap_artifact()
            self._ensure_env_setup()

            self.box.subcommands = fm.subcommands

            self.box._submission_tar_cmd = self._candidate_tar_cmd()
            (self.artifact / "featuremap.json").write_text(json.dumps({
                "flags": fm.flags, "subcommands": fm.subcommands,
                "version": fm.version, "help": fm.help_text[:4000]}))
            self._feature_graph = mine_feature_graph(self.room, fm) if getattr(self, "test_discovery_v2", True) else None
            self._capability_profile = detect_capabilities(fm.help_text, fm.readme) if self._feature_graph else None
            if self._feature_graph:

                if os.environ.get("PB_BEHAVIORAL_OBLIGATIONS") == "1":
                    augment_behavioral_obligations(self._feature_graph, self._capability_profile)
                    self._log(f"behavioral obligations ON: graph now {len(self._feature_graph.obligations)} obligations")
                (self.artifact / "featuregraph.json").write_text(json.dumps(self._feature_graph.to_dict(), indent=2))
            if getattr(self, "bootstrap_artifact", None):

                self._assert_bootstrap_prompt_matches(fm)

            self._probe_graph_stage()
            result = self._agent_loop(fm)
            return result
        finally:
            try:
                if getattr(self, "_feature_graph", None) is not None and getattr(self, "box", None):
                    update_coverage(self._feature_graph, self.box.corpus or [])
                    (self.artifact / "featuregraph.json").write_text(
                        json.dumps(self._feature_graph.to_dict(), indent=2, ensure_ascii=False))
            except Exception as e:
                self._log(f"final featuregraph persistence failed: {e}")
            self._dump_transcript()
            self._write_component_telemetry()

            if getattr(self, "box", None) and self.box.flaky:
                self.box.flush_flaky()
                import flaky_registry
                flaky_registry.merge_run(self.instance_id, self.box.flaky, run_id=self.artifact.name)

            if getattr(self, "_all_divergences", None):
                import divergence_registry
                divergence_registry.merge_run(self.instance_id, self._all_divergences,
                                              run_id=self.artifact.name)

            self._finalize_release()

            try:
                self._deliver_final_solution()
            except Exception:
                pass
            self.room.stop()

    @staticmethod
    def _prune_stale_clones():

        import os
        import subprocess
        from container import DOCKER
        try:

            r = subprocess.run(["pgrep", "-f", "run_task.py"], capture_output=True, timeout=20)
            pids = {int(x) for x in r.stdout.decode().split() if x.strip().isdigit()}
            mine = {os.getpid(), os.getppid()}
            others = pids - mine
            if others:
                return
            ids = [x for x in subprocess.run(
                DOCKER + ["ps", "-aq", "--filter", "name=^pb-re-"],
                capture_output=True, timeout=60).stdout.decode().split() if x]
            if ids:
                subprocess.run(DOCKER + ["rm", "-f", *ids], capture_output=True, timeout=120)
        except Exception:
            pass

    def _seed_candidate(self):

        if self.seed_solution_dir and Path(self.seed_solution_dir).is_dir():
            import subprocess, io, tarfile
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as t:
                for p in Path(self.seed_solution_dir).iterdir():
                    if p.name in ("executable", ".coverage", "target"):
                        continue
                    t.add(p, arcname=p.name)
            buf.seek(0)
            from room_factory import seed_candidate_tar
            seed_candidate_tar(self.room, buf.getvalue(), timeout=120)

            self.room.sh("cd /workspace && chmod +x compile.sh && ./compile.sh 2>&1 || true", timeout=600)
            if self.room.exec(["test", "-x", CAND_EXE]).code == 0:
                self._seeded_from_solution = True
                self._wrote_real_candidate = True
                if getattr(self, "box", None) is not None:
                    self.box._wrote_real_candidate = True
                return
        self.room.write_file("/workspace/main.py", _PLACEHOLDER_MAIN)
        self.room.write_file("/workspace/compile.sh",
            "#!/bin/sh\ncat > executable <<'W'\n#!/bin/sh\n"
            'exec python3 "$(dirname "$0")/main.py" "$@"\nW\nchmod +x executable\n')
        self.room.sh("cd /workspace && sh compile.sh")

    def _seed_fixture_probes(self) -> None:

        if not self.box or getattr(self, "_fixtures_seeded", False):
            return
        self._fixtures_seeded = True

        r = self.box.room.exec(["sh", "-c", "find /fixtures -type f -o -type l 2>/dev/null | head -40"])
        paths = [ln.strip() for ln in (r.stdout or b"").decode("utf-8", "replace").splitlines()
                 if ln.strip().startswith("/fixtures") and not ln.strip().endswith(("/ENV", "setup.sh"))]
        for p in dict.fromkeys(paths):
            for args in (["foo", "X", p], ["a", "", p]):
                self.box._t_diff({"args": args, "stdin": ""})

    def _seed_doc_coverage_probes(self, fm) -> None:

        if not self.box or getattr(self, "_doc_probes_seeded", False):
            return
        self._doc_probes_seeded = True
        seen: set[tuple] = set()

        subs = list(dict.fromkeys(fm.subcommands))
        help_cap = min(len(subs), getattr(self, "subcommand_help_anchor_cap", 6))
        if help_cap >= len(subs):
            help_subs = subs
        elif help_cap <= 0:
            help_subs = []
        elif help_cap == 1:
            help_subs = [subs[0]]
        else:
            indexes = [(i * (len(subs) - 1)) // (help_cap - 1) for i in range(help_cap)]
            help_subs = [subs[i] for i in dict.fromkeys(indexes)]
        essential = [["--help"], ["-h"], ["--version"], []] + [
            [s, "--help"] for s in help_subs]

        sub_probes = [[s] for s in fm.subcommands]

        bare_flags = [[f] for f in fm.flags]

        graph = getattr(self, "_feature_graph", None)
        sub_flag_probes: list[list[str]] = []
        if graph is not None:
            root_known = set(fm.flags)
            for path, node in graph.nodes.items():
                if path == "(root)" or path.startswith("(behavior)"):
                    continue
                for flag in node.flags:
                    if flag in {"-h", "--help", "--version", "-V"}:
                        continue
                    sub_flag_probes.append([path, flag])

            budget = max(0, int(os.environ.get("PB_DOC_PROBE_CAP", "150")))
            if len(sub_flag_probes) > budget:
                by_sub: dict[str, list[list[str]]] = {}
                for p in sub_flag_probes:
                    by_sub.setdefault(p[0], []).append(p)
                fair, i = [], 0
                while len(fair) < budget and any(by_sub.values()):
                    for k in list(by_sub):
                        if by_sub[k] and len(fair) < budget:
                            fair.append(by_sub[k].pop(0))
                    i += 1
                    if i > budget:
                        break
                self._log(f"doc-probe budget {budget}: seeding {len(fair)} of "
                          f"{len(sub_flag_probes)} subcommand-flag probes "
                          f"({len(sub_flag_probes) - len(fair)} dropped)")
                sub_flag_probes = fair

            bare_flags += [[f] for f in (set(graph.nodes["(root)"].flags) - root_known)
                           if f not in {"-h", "--help", "--version", "-V"}]                          if "(root)" in graph.nodes else []
        top_seeded = 0
        seeded = 0

        _left = max(0.0, getattr(self, "deadline", 0.0) - time.time())
        seed_budget = float(os.environ.get("PB_DOC_PROBE_SECONDS") or (_left * 0.15 if _left else 600.0))
        seed_deadline = time.time() + seed_budget
        for args in essential + sub_probes + sub_flag_probes + bare_flags:
            if tuple(args) in seen:
                continue

            is_bare_top = (len(args) == 1 and str(args[0]).startswith("-")
                           and args[0] not in ("--help", "-h", "--version")
                           and self._case_region({"args": args}) == "(top)")
            if is_bare_top:

                cap = getattr(self, "top_region_cap", 12)
                if os.environ.get("PB_TOP_REGION_CAP"):
                    cap = int(os.environ["PB_TOP_REGION_CAP"])
                elif not fm.subcommands:
                    cap = max(cap, min(len(fm.flags), 120))
                if top_seeded >= cap:
                    continue
                top_seeded += 1
            seen.add(tuple(args))

            if time.time() >= seed_deadline:
                self._log(f"doc-probe seeding stopped at wall-clock guard: {seeded} probes seeded, "
                          f"{len(seen)} attempted (budget {seed_budget:.0f}s of the run's deadline)")
                break
            seeded += 1
            self.box._t_diff({"args": args, "stdin": "",
                              "_force_record": getattr(self, "regression_guard", True)})

        if getattr(self, "regression_guard", True):
            try:
                self.box._anchor_pass_prev, _ = self.box.anchor_pass_set()
            except Exception:
                pass

    @staticmethod
    def _changed_candidate_files(before: Path, after: Path) -> set[str]:
        import hashlib, os
        skip = {"AGENT_REPORT.md", "pb_record_probe", ".pb/probe_journal.jsonl",
                "BEST_local_rate.txt", "executable"}
        normalize = os.environ.get("PB_DEVELOPER_SCOPE_NORMALIZE", "1") == "1"
        wide = normalize and os.environ.get("PB_SCOPE_WIDE_BYPRODUCTS", "1") == "1"
        if normalize:
            skip |= {".gitignore", ".gitattributes", ".DS_Store"}
        if wide:
            skip |= {"compile.sh"}
        def _dirs(root):
            if not root.exists():
                return set()
            return {d.relative_to(root).as_posix() for d in root.rglob("*") if d.is_dir()}
        new_dirs = (_dirs(after) - _dirs(before)) if wide else set()
        def byproduct(rel):
            if not normalize:
                return False
            name = rel.rsplit("/", 1)[-1]
            if (name.startswith("-")
                    or name.endswith((".bak", ".orig", ".rej", ".tmp", ".swp", "~"))):
                return True
            if not wide:
                return False
            if name == "a.out" or name.endswith((".o", ".obj", ".pyc", ".class")):
                return True
            parts = rel.split("/")
            return any("/".join(parts[:i + 1]) in new_dirs for i in range(len(parts) - 1))
        def tree(root):
            out = {}
            if not root.exists(): return out
            for f in root.rglob("*"):
                if not f.is_file(): continue
                rel = f.relative_to(root).as_posix()
                if (rel in skip or byproduct(rel)
                        or any(part in _SNAPSHOT_SKIP_DIRS for part in f.relative_to(root).parts)):
                    continue
                try: out[rel] = hashlib.sha256(f.read_bytes()).digest()
                except OSError: pass
            return out
        a, b = tree(before), tree(after)
        return {k for k in a.keys() | b.keys() if a.get(k) != b.get(k)}

    def _record_developer_attempt(self, row: dict) -> str:
        attempts = getattr(self, "_developer_attempts", None)
        if attempts is None:
            attempts = self._developer_attempts = []
        self._developer_request_seq = int(getattr(self, "_developer_request_seq", 0)) + 1
        request_id = row.get("request_id") or f"developer-request-{self._developer_request_seq:03d}"
        stored = {"request_id": request_id, "at": time.time(),
                  "turn": getattr(self, "_turns", 0), **row}
        trade = _trade_ledger_entry(stored)
        if trade:
            stored["trade_ledger"] = trade
            if trade["lost_unprotected"]:

                self._log(f"TRADE: {stored.get('mode')} edit {request_id} accepted="
                          f"{stored.get('validation_accepted')} — gained {trade['gained']}, "
                          f"LOST {trade['lost_unprotected']} unprotected "
                          f"(+{trade['lost_anchor']} anchor) of {trade['pre_pass']} passing")
        attempts.append(stored)
        try:
            (self.artifact / "developer_attempts.json").write_text(json.dumps(
                {"schema_version": "1.0", "attempts": attempts}, indent=2,
                ensure_ascii=False, default=str))
        except Exception:
            pass
        return request_id











    def _agent_loop(self, fm) -> dict:
        self._fm = fm
        kickoff = prompts.load("kickoff_from_scratch")
        if self.lang_hint:
            kickoff += "\n\n" + self.lang_hint

        help_note = "" if fm.flags else prompts.load("help_note_no_flags")

        handoff = ""

        if os.environ.get("PB_IMPLEMENTOR_DEBT_BRIEF") == "1":
            kickoff += "\n\n" + self._dev_capability_brief(fm)
        msgs = [{"role": "user", "content":
                 f"Instance: {self.instance_id}\n\nReference feature map (from --help/README):\n"
                 f"{fm.summary()}\n\nREADME:\n{fm.readme[:4000]}{help_note}\n\n"
                 f"There is NO practice exam: you can `submit` only ONCE, and that is the only time "
                 f"you ever see your hidden-test score. Drive correctness by exploiting the "
                 f"documentation and differential testing vs the oracle.\n\n{handoff}{kickoff}"}]
        schemas = tool_schemas()
        if self.hunters:
            schemas = schemas + [{"name": "request_hunt",
                "description": prompts.load("tool_request_hunt"),
                "input_schema": {"type": "object", "properties": {
                    "reason": {"type": "string", "description": "what you're stuck on"}}}}]
        if self.critics:
            schemas = schemas + [{"name": "request_critic",
                "description": prompts.load("tool_request_critic"),
                "input_schema": {"type": "object", "properties": {
                    "reason": {"type": "string", "description": "why you suspect a hidden behaviour"}}}}]

        turns = 0
        _forced = False
        self._loop_started_at = time.time()
        system = SYSTEM
        if self.hunters or self.judges or self.critics:
            system = SYSTEM + MULTIAGENT_NOTE
        if self.resume_from:

            msgs = _truncate_msgs([msgs[0]] + self._resume_msgs, keep=40)
            msgs.append({"role": "user", "content":
                         "[resumed] This run was interrupted (sandbox crash / wedge) and is now "
                         "continuing from where it stopped: your divergence corpus and best candidate "
                         "are restored, and the remaining time is the leftover of the original budget. "
                         "Keep going from here."})
            turns = self._resume_turn
            self.max_turns = max(self.max_turns, self._resume_turn + self.phase2_max_turns)
            now = time.time()
            self._phase2_started_at = now - (self.phase2_wall_clock - self._resume_remaining)
            self.deadline = now + self._resume_remaining
        while turns < self.max_turns and time.time() < self.deadline:
            turns += 1
            self._turns = turns

            if turns % 5 == 0 and self.room is not None and not self.room.alive():
                self._log("FATAL: cleanroom container is gone (room.alive()=False) — aborting loop")
                try:
                    self._sync_solution_out()
                except Exception:
                    pass
                break

            if (self._phase1_done and not _forced and not self.box.submissions
                    and turns >= int(0.8 * self.max_turns)):
                _forced = True
                msgs.append({"role": "user", "content":
                             "You are running out of turns and have NOT submitted yet. "
                             "Make the candidate build and run (even partially), then call `submit` "
                             "NOW with whatever you have — a partial score beats 0. Do this "
                             "in your next action."})

            timewarn = self._phase2_time_reminder()
            if timewarn:
                if msgs and msgs[-1].get("role") == "user" and isinstance(msgs[-1].get("content"), str):
                    msgs[-1] = {"role": "user", "content": msgs[-1]["content"] + timewarn}
                else:
                    msgs.append({"role": "user", "content": timewarn.lstrip("\n")})
            resp = self.llm.messages(system, msgs, schemas)
            content = resp.get("content", [])
            self._record_turn(turns, assistant=content)
            if turns % 5 == 0:
                self._dump_transcript()
            if turns % 10 == 0 and self._phase1_done:

                self._sync_solution_out()
            self._archive_candidate_by_time()
            msgs.append({"role": "assistant", "content": content})
            tool_uses = [b for b in content if b.get("type") == "tool_use"]
            if not tool_uses:
                if resp.get("stop_reason") == "end_turn" and self.box.done:
                    break

                msgs.append({"role": "user", "content":
                             "Continue: keep differentially probing the oracle and fixing "
                             "divergences, or submit if you are confident. Do not stop early."})
                continue
            results = []
            for tu in tool_uses:
                out = self._dispatch(tu["name"], tu.get("input", {}))
                results.append({"type": "tool_result", "tool_use_id": tu["id"],
                                "content": json.dumps(out)[:8000]})
            msgs.append({"role": "user", "content": results})
            self._record_turn(turns, tool_results=results)

            if not self._phase1_done and (self._bootstrap_declared
                                          or turns >= self.phase1_max_turns
                                          or time.time() >= self.deadline):
                self._enter_phase2(turns, msgs)

            sweep_cadence = (max(8, self.sweep_every_turns // 2)
                             if self._seeded_from_solution else self.sweep_every_turns)
            periodic = (turns - self._last_sweep_turn) >= sweep_cadence
            reg_status = (self.box.count_regression_failures(limit=1, detail=0)
                          if self.box.corpus else {})
            has_unfixed = bool(self.box.corpus) and self._required_regression_metrics(
                reg_status)["failed"] > 0

            dry = self._consecutive_dry_sweeps >= self.dry_sweeps_to_stop
            if (dry and not self._steered_to_submit and not self.box.done
                    and not has_unfixed and self._phase1_done):
                self._steered_to_submit = True
                msgs.append({"role": "user", "content":
                             f"[converged] The coverage team has run {self._consecutive_dry_sweeps} "
                             "sweeps in a row with ZERO new real divergences and your candidate "
                             "passes every recorded case. It is converged. STOP probing and "
                             "`submit` NOW — the adversarial judge panel will gate it; if they "
                             "find anything you'll get it back to fix, otherwise it counts."})

            self._consecutive_clean_turns = 0 if has_unfixed else self._consecutive_clean_turns + 1
            steer = self._clean_streak_steer()
            if steer:
                msgs.append({"role": "user", "content": steer})

            writes_now = self.box.writes if self.box else 0
            if writes_now > self._last_writes_seen:
                self._last_writes_seen = writes_now
                self._turns_since_edit = 0
            else:
                self._turns_since_edit += 1
            if self._edit_drought_steer(has_unfixed):
                self._turns_since_edit = 0
                fails = self.box.count_regression_failures(limit=3, detail=2)
                primary_fails = self._required_regression_metrics(fails)
                names = "; ".join(str(e.get("args")) for e in fails.get("examples", [])
                                  if e.get("required", True) is not False)[:300]
                msgs.append({"role": "user", "content":
                             f"[fix-now] You have FAILING required cases ({primary_fails['failed']} of "
                             f"{primary_fails['total']}) but haven't edited the candidate in "
                             f"{self.edit_drought_turns} turns — you are over-probing. STOP probing "
                             "the oracle. Pick ONE failing case"
                             + (f" (e.g. {names})" if names else "") +
                             ", diff candidate vs oracle output on it, change the SOURCE to match "
                             "byte-for-byte with `bash`, and `run_regression`. Make an edit THIS "
                             "turn. Diagnosing without editing does not move the score."})

            if (self._phase1_done and not self.box.submissions and not self.box.done
                    and not has_unfixed and turns >= int(0.6 * self.max_turns)
                    and turns - self._last_submit_steer_turn >= self.submit_steer_every):
                self._steered_to_submit = True
                self._last_submit_steer_turn = turns
                msgs.append({"role": "user", "content":
                             "[submit-now] Your candidate passes EVERY recorded corpus case and "
                             "you've used most of the turn budget without a counted "
                             "submission. STOP probing and call `submit` THIS TURN. You get ONE "
                             "counted submission — submitting now at high confidence is the "
                             "right move; if the judge panel finds a real issue it comes back "
                             "for you to fix (and does NOT consume your submission), otherwise it "
                             "banks your score. Do not probe further first."})

            if (self._phase1_done and not self.box.submissions and not self.box.done
                    and not self._steered_last_resort
                    and turns >= int(0.8 * self.max_turns) and self.box.corpus):
                reg = self.box.count_regression_failures(limit=1, detail=0)
                local_rate = self._required_regression_metrics(reg)["pass_rate"]
                if self._should_last_resort_submit(turns, local_rate):
                    self._steered_last_resort = True
                    msgs.append({"role": "user", "content":
                                 f"[submit-now-final] You are past 80% of the turn budget with ZERO "
                                 f"counted submissions and your candidate passes {local_rate:.0%} of "
                                 "its own corpus. The last few failing cases may be flaky/over-strict "
                                 "or not in the hidden suite. A counted submit NOW at this confidence "
                                 "strictly beats ending the run at 0. Call `submit` THIS TURN — the "
                                 "readiness gate still checks it first; if the judge panel rejects it, "
                                 "it comes back for you to fix without consuming your submission. "
                                 "Do NOT keep probing."})
            if (self._phase1_done
                    and not self.box.done
                    and (self.hunters or self.critics)
                    and periodic
                    and not dry
                    and self.hunt_rounds_used < self.max_hunt_rounds
                    and not has_unfixed):
                self._last_sweep_turn = turns
                sw = self._sweep("periodic")
                if sw.get("ran"):
                    msgs.append({"role": "user", "content":
                                 f"[coverage sweep] {sw['note']} Parallel divergence-hunters "
                                 "AND completeness-critics (which probe behaviour the docs "
                                 "omit — hidden aliases/flags/env/protocol) ran together. "
                                 "FIX every new divergence (run_regression to list them, edit, "
                                 "rebuild). Do not submit until a sweep comes back clean — "
                                 "your FIRST submission should pass everything."})
            if self.box.done:
                break

            if len(msgs) > 80:
                msgs = _truncate_msgs(msgs, keep=40)

        if getattr(self, "final_pool_replay_enabled", False):
            self._sync_solution_out()
            try:
                self._archive_pool_snapshot(self.sol_dir, "terminal_live")
            except Exception as e:
                self._log(f"terminal_live archive failed: {e}")
        if not self.box.submissions:
            self._restore_best_into_cand()
            self._terminal_submit_result = self._dispatch("submit", {
                "_terminal": True,
                "rationale": "auto-submit: turn/wall-clock budget exhausted with no manual submission"})
        return self._final_result(turns)

    def _archive_pool_snapshot(self, source: Path, kind: str) -> Path | None:
        if not source.is_dir():
            return None
        digest = self._snapshot_source_digest(source)
        if not digest:
            return None
        root = self.artifact / "candidate_pool"
        root.mkdir(parents=True, exist_ok=True)
        for existing in sorted(p for p in root.iterdir() if p.is_dir()):
            if self._snapshot_source_digest(existing) == digest:
                return existing
        index = len([p for p in root.iterdir() if p.is_dir()])
        dst = root / f"{index:02d}_{kind}_t{getattr(self, '_turns', 0):04d}"
        _snapshot_copytree(source, dst)
        if not dst.exists():
            return None
        provenance = {"kind": kind, "turn": getattr(self, "_turns", 0),
                      "source_digest": digest}
        (dst / "PROVENANCE.json").write_text(json.dumps(provenance, indent=2))
        getattr(self, "_candidate_pool_labels", set()).add(kind)
        return dst

    def _candidate_pool_entries(self) -> list[dict]:

        items: list[dict] = []

        def push(path: Path, kind: str, sort_key: str = ""):
            if path.is_dir():
                items.append({"path": path, "kind": kind, "sort_key": sort_key})

        push(self.artifact / "phase1_candidate", "bootstrap")
        pool = self.artifact / "candidate_pool"
        if pool.is_dir():
            for path in sorted(p for p in pool.iterdir() if p.is_dir()):
                kind = "checkpoint"
                prov = path / "PROVENANCE.json"
                if prov.is_file():
                    try:
                        kind = str(json.loads(prov.read_text()).get("kind") or kind)
                    except Exception:
                        pass
                elif "bootstrap" in path.name:
                    kind = "bootstrap"
                push(path, kind)
        push(self.artifact / "best_candidate", "instantaneous_best")
        push(self.sol_dir, "final_live")
        if getattr(self, "final_pool_wide_intake", True):
            for path in sorted((self.artifact / "best_history").glob("t*")):
                push(path, "best_history", path.name)
            for path in sorted((self.artifact / "developer-snapshots").glob("run-*-pre")):
                push(path, "developer_pre", path.name)
            for path in sorted((self.artifact / "candidate_snapshots").glob("t*min")):
                push(path, "snapshot", path.name)
        return bounded_candidates(items, self._snapshot_source_digest,
                                  getattr(self, "final_pool_max_candidates", 12),
                                  getattr(self, "final_pool_snapshot_samples", 3))

    @staticmethod
    def _valid_candidate_relpath(raw: str) -> str | None:
        p = Path(str(raw))
        if not raw or p.is_absolute() or ".." in p.parts or p.as_posix() in (".", ""):
            return None
        rel = p.as_posix()
        return rel[2:] if rel.startswith("./") else rel

    def _clean_candidate_files(self, snapshot_roots=()) -> bool:

        if not self.room:
            return False
        lock_state = self._clear_stale_git_index_lock("candidate_cleanup_start") or {}
        if lock_state.get("present") and not lock_state.get("removed"):
            self._log("candidate cleanup blocked by an active/unverifiable git index lock")
            return False
        baseline = {self._valid_candidate_relpath(x) for x in
                    (getattr(self.room, "baseline_files", set()) or set())}
        baseline.discard(None)
        current: set[str] = set()
        try:
            current = set(self.room._list_workspace_files())
        except Exception:
            pass
        files: set[str] = set()
        for raw in current:
            rel = self._valid_candidate_relpath(raw)
            if rel and rel not in baseline and not rel.startswith(".git/"):
                files.add(rel)
        for root in snapshot_roots:
            root = Path(root)
            if not root.is_dir():
                continue
            for p in root.rglob("*"):
                if not p.is_file():
                    continue
                rel = self._valid_candidate_relpath(p.relative_to(root).as_posix())
                if rel and rel not in baseline and not rel.startswith(".git/"):
                    files.add(rel)

        files.add("executable")
        dirs = sorted({Path(f).parent.as_posix() for f in files
                       if Path(f).parent.as_posix() not in (".", "")},
                      key=lambda p: (p.count("/"), p), reverse=True)
        try:
            file_blob = b"\0".join(f.encode("utf-8", "surrogateescape") for f in sorted(files)) + b"\0"
            dir_blob = b"\0".join(d.encode("utf-8", "surrogateescape") for d in dirs) + b"\0"
            self.room.write_bytes("/tmp/_pb_candidate_files.nul", file_blob)
            self.room.write_bytes("/tmp/_pb_candidate_dirs.nul", dir_blob)

            cleaned = self.room.exec(["sh", "-c",
                "cd /workspace && xargs -0 -r rm -f -- < /tmp/_pb_candidate_files.nul; RC=$?; "
                "xargs -0 -r rmdir -- < /tmp/_pb_candidate_dirs.nul 2>/dev/null || true; "
                "rm -f /tmp/_pb_candidate_files.nul /tmp/_pb_candidate_dirs.nul 2>/dev/null || true; "
                "exit $RC"], timeout=180)
            if cleaned.code != 0:
                self._log(f"candidate cleanup workspace rm exited {cleaned.code}")
                return False
            return True
        except Exception as e:
            self._log(f"candidate cleanup manifest failed: {type(e).__name__}: {e}")
            return False

    def _restore_final_replay_corpus(self, original_corpus: list[dict]) -> None:
        self.box.corpus = original_corpus

        if getattr(self.box, "corpus_path", None):
            self.box.corpus_path.write_text(json.dumps(original_corpus))
        self.box._reg_dirty = True
        self.box._reg_cache = None

    def _select_final_candidate_from_pool(self) -> dict | None:

        if (not getattr(self, "final_pool_replay_enabled", False)
                or getattr(self, "_final_pool_selected", False)
                or getattr(self, "_final_pool_selection_in_progress", False)
                or not getattr(self, "box", None) or not getattr(self, "room", None)):
            return None
        self._final_pool_selection_in_progress = True
        original_corpus = self.box.corpus
        try:
            import copy, hashlib, shutil
            self._archive_pool_snapshot(self.sol_dir, "final_live")
            entries = self._candidate_pool_entries()
            if not entries:
                return None
            repair_all = copy.deepcopy(self.box.corpus or [])
            challenge_all = copy.deepcopy(self._active_sealed_challenge())

            frozen = [c for c in repair_all if c.get("required", True) is not False]
            challenge = [c for c in challenge_all if c.get("required", True) is not False]
            diagnostic = ([{"suite": "repair", "case": c} for c in repair_all
                           if c.get("required", True) is False] +
                          [{"suite": "challenge", "case": c} for c in challenge_all
                           if c.get("required", True) is False])
            repair_clusters = {self._semantic_cluster_key(c) for c in frozen}
            challenge_clusters = {self._semantic_cluster_key(c) for c in challenge}
            overlap = repair_clusters & challenge_clusters
            if overlap:
                raise RuntimeError(f"sealed challenge leaked {len(overlap)} semantic cluster(s) "
                                   "into the repair corpus")
            repair_canonical = json.dumps(frozen, sort_keys=True, ensure_ascii=False,
                                          separators=(",", ":"), default=str).encode()
            challenge_canonical = json.dumps(challenge, sort_keys=True, ensure_ascii=False,
                                             separators=(",", ":"), default=str).encode()
            diagnostic_canonical = json.dumps(diagnostic, sort_keys=True, ensure_ascii=False,
                                              separators=(",", ":"), default=str).encode()
            repair_sha = hashlib.sha256(repair_canonical).hexdigest()
            challenge_sha = hashlib.sha256(challenge_canonical).hexdigest()
            diagnostic_sha = hashlib.sha256(diagnostic_canonical).hexdigest()
            combined = json.dumps({"repair": repair_sha, "challenge": challenge_sha},
                                  sort_keys=True, separators=(",", ":")).encode()
            suite = {
                "kind": "adaptive_discovery_sealed_challenge_not_hidden_eval",
                "case_count": len(frozen) + len(challenge),
                "repair_case_count": len(frozen),
                "challenge_case_count": len(challenge),
                "sha256": hashlib.sha256(combined).hexdigest(),
                "repair_sha256": repair_sha,
                "challenge_sha256": challenge_sha,
                "diagnostic_case_count": len(diagnostic),
                "diagnostic_sha256": diagnostic_sha,
                "diagnostic_cases_excluded_from_selection": True,
                "candidate_count": len(entries),
                "semantic_cluster_disjoint": True,
                "challenge_outcomes_exposed_during_repair": False,
                "contaminated_challenge_cases_excluded": getattr(
                    self, "_sealed_challenge_contaminated", 0),
                "repair_case_identities": [repr(case_identity(c)) for c in frozen],
                "challenge_case_identities": [repr(case_identity(c)) for c in challenge],
                "diagnostic_case_identities": [repr(case_identity(row["case"]))
                                               for row in diagnostic],

                "case_identities": [repr(case_identity(c)) for c in frozen + challenge],
            }
            (self.artifact / "final_challenge_suite.json").write_text(
                json.dumps(suite, indent=2, ensure_ascii=False))

            rows: list[dict] = []
            outcomes_by_candidate: dict[str, list[dict]] = {}
            cleanup_roots = [e["path"] for e in entries]
            bootstrap = next((e for e in entries if e["kind"] == "bootstrap"), None)
            bootstrap_path = bootstrap["path"] if bootstrap else None
            replay_deadline = time.time() + getattr(self, "final_replay_wall_clock", 1800)
            budget_skipped = 0
            for entry in entries:
                if rows and time.time() > replay_deadline:
                    budget_skipped += 1
                    continue
                row = {"candidate_id": entry["candidate_id"], "kind": entry["kind"],
                       "source_digest": entry["digest"],
                       "snapshot": str(entry["path"].relative_to(self.artifact))}
                try:
                    loaded = False
                    restore_attempts = 0
                    for restore_attempts in (1, 2):
                        loaded = self._lay_snapshot_into_cand(
                            entry["path"], cleanup_roots=cleanup_roots)
                        if loaded:
                            break
                    row["restore_attempts"] = restore_attempts
                    row["replay_ok"] = bool(loaded)
                    if not loaded:
                        row.update({"delivery_ok": False, "error": "snapshot did not build",
                                    "anchor_pass_keys": [], "selection_key": (0,)})
                        rows.append(row)
                        continue
                    smoke = self.box._delivery_smoke_base()

                    def replay_suite(cases: list[dict]) -> tuple[dict, int]:
                        self.box.corpus = copy.deepcopy(cases)
                        self.box._reg_dirty = True
                        self.box._reg_cache = None
                        if not cases:
                            return ({"failed": 0, "scanned": 0, "timeouts": 0,
                                     "region_pass": {}}, 0)
                        first_error = None
                        for attempt in (1, 2):
                            if attempt > 1:
                                self.box.corpus = copy.deepcopy(cases)
                                self.box._reg_dirty = True
                                self.box._reg_cache = None
                            try:
                                return (self.box.count_regression_failures(
                                    limit=6, max_scan=max(1, len(cases)), detail=0), attempt)
                            except Exception as error:
                                first_error = error
                                if attempt == 1 and self._lay_snapshot_into_cand(
                                        entry["path"], cleanup_roots=cleanup_roots):
                                    continue
                                raise first_error
                        raise first_error

                    train_reg, train_attempts = replay_suite(frozen)
                    outcomes_by_candidate[entry["candidate_id"]] = list(
                        train_reg.get("case_outcomes") or [])
                    row["repair_replay_attempts"] = train_attempts

                    passing, failing = self.box.anchor_pass_set()
                    challenge_reg, challenge_attempts = replay_suite(challenge)
                    row["challenge_replay_attempts"] = challenge_attempts
                    row["regression_replay_attempts"] = train_attempts + challenge_attempts

                    train_total = len(frozen)
                    challenge_total = len(challenge)
                    train_failed = int(train_reg.get("failed", 0) or 0)
                    challenge_failed = int(challenge_reg.get("failed", 0) or 0)
                    train_scanned = int(train_reg.get("scanned", train_total) or train_total)
                    challenge_scanned = int(
                        challenge_reg.get("scanned", challenge_total) or challenge_total)
                    train_timeouts = int(train_reg.get("timeouts", 0) or 0)
                    challenge_timeouts = int(challenge_reg.get("timeouts", 0) or 0)
                    failed = train_failed + challenge_failed
                    scanned = train_scanned + challenge_scanned
                    timeouts = train_timeouts + challenge_timeouts
                    completion = ((scanned - timeouts) / scanned) if scanned else 1.0
                    train_completion = ((train_scanned - train_timeouts) / train_scanned
                                        if train_scanned else 1.0)
                    challenge_completion = (
                        (challenge_scanned - challenge_timeouts) / challenge_scanned
                        if challenge_scanned else 1.0)
                    train_rate = ((train_total - train_failed) / train_total
                                  if train_total else 1.0)
                    challenge_rate = ((challenge_total - challenge_failed) / challenge_total
                                      if challenge_total else train_rate)
                    region_pass: dict[str, list[int]] = {}
                    for result in (train_reg, challenge_reg):
                        for region, values in (result.get("region_pass") or {}).items():
                            dest = region_pass.setdefault(region, [0, 0])
                            dest[0] += int(values[0]); dest[1] += int(values[1])
                    risk = (len(self._changed_candidate_files(bootstrap_path, entry["path"]))
                            if bootstrap_path else 0)
                    key = selection_vector(
                        delivery_ok=bool(smoke.get("ok")),
                        region_pass=region_pass,
                        anchor_pass=len(passing), anchor_total=len(passing) + len(failing),
                        challenge_rate=challenge_rate, train_rate=train_rate, risk=risk,
                        completion_rate=completion)
                    row.update({"delivery_ok": bool(smoke.get("ok")),
                                "delivery_kind": smoke.get("kind"),
                                "failed": failed, "scanned": scanned, "timeouts": timeouts,
                                "completion_rate": completion,
                                "repair_failed": train_failed,
                                "repair_scanned": train_scanned,
                                "repair_timeouts": train_timeouts,
                                "repair_completion_rate": train_completion,
                                "train_rate": train_rate,
                                "challenge_available": bool(challenge_total),
                                "challenge_failed": challenge_failed,
                                "challenge_scanned": challenge_scanned,
                                "challenge_timeouts": challenge_timeouts,
                                "challenge_completion_rate": challenge_completion,
                                "challenge_rate": challenge_rate,
                                "region_pass": region_pass,
                                "region_floor": minimum_region_floor(region_pass),
                                "anchor_pass": len(passing),
                                "anchor_total": len(passing) + len(failing),
                                "anchor_pass_keys": sorted(repr(x) for x in passing),
                                "risk_changed_files_from_bootstrap": risk,
                                "selection_key": key})
                except Exception as e:
                    row.update({"replay_ok": False, "delivery_ok": False,
                                "anchor_pass_keys": [], "selection_key": (0,),
                                "error": f"{type(e).__name__}: {str(e)[:300]}"})
                rows.append(row)
            if budget_skipped:
                self._log(f"final replay wall-clock cap ({getattr(self, 'final_replay_wall_clock', 1800)}s) "
                          f"hit: replayed {len(rows)}/{len(entries)} candidates, {budget_skipped} skipped")
            self.box.corpus = original_corpus
            self.box._reg_dirty = True
            self.box._reg_cache = None

            bootstrap_id = bootstrap["candidate_id"] if bootstrap else None
            initial_winner, _ = select_replayed_candidate(rows, bootstrap_id)
            attempted_deep: set[str] = set()
            winner = None
            annotated = []
            selected = None

            while True:
                proposed, annotated = select_replayed_candidate(rows, bootstrap_id)
                if not proposed or proposed["candidate_id"] in attempted_deep:
                    break
                candidate_id = proposed["candidate_id"]
                attempted_deep.add(candidate_id)
                selected = next(e for e in entries if e["candidate_id"] == candidate_id)
                source_row = next(r for r in rows if r["candidate_id"] == candidate_id)
                final_loaded = False
                final_restore_attempts = 0
                for final_restore_attempts in (1, 2):
                    final_loaded = self._lay_snapshot_into_cand(
                        selected["path"], cleanup_roots=cleanup_roots)
                    if final_loaded:
                        break
                source_row["final_restore_attempts"] = final_restore_attempts
                if not final_loaded:
                    source_row.update({"replay_ok": False, "delivery_ok": False,
                                       "final_restore_ok": False,
                                       "error": "winner failed final restore after retry"})
                    continue
                source_row["final_restore_ok"] = True
                deep = None
                deep_attempts = 0
                deep_fn = getattr(self.box, "_delivery_smoke", None)
                for deep_attempts in (1, 2):
                    deep = (deep_fn(deep=True) if deep_fn
                            else self.box._delivery_smoke_base())
                    if deep.get("ok"):
                        break
                    if deep_attempts == 1:

                        self._lay_snapshot_into_cand(
                            selected["path"], cleanup_roots=cleanup_roots)
                source_row.update({"deep_delivery_attempts": deep_attempts,
                                   "deep_delivery_ok": bool(deep and deep.get("ok")),
                                   "deep_delivery_kind": (deep or {}).get("kind"),
                                   "deep_delivery_message": (deep or {}).get("message", "")[:1200]})

                if deep and deep.get("ok"):

                    fresh = {"ok": None}
                    import os as _os
                    if (_os.environ.get("PB_FRESH_CLONE_WINNER_GATE", "1") == "1"
                            and getattr(self, "box", None) is not None
                            and getattr(self.box, "room", None) is not None):
                        try:
                            self._invalidate_clone_build_health("final_replay_winner")
                            fresh = self._check_subagent_clone_build()
                        except Exception as e:
                            self._log(f"fresh-cleanroom winner check unavailable: "
                                      f"{type(e).__name__}: {e}")
                            fresh = {"ok": None}
                    if fresh.get("ok") is False:
                        deep = {"ok": False, "kind": "fresh_clone_startup",
                                "message": ("winner fails in a FRESH cleanroom (depends on something "
                                            "only present in the agent's own sandbox): "
                                            + str(fresh.get("error", ""))[:800])}
                        source_row.update({"fresh_clone_ok": False,
                                           "fresh_clone_error": str(fresh.get("error", ""))[:800],
                                           "deep_delivery_ok": False,
                                           "deep_delivery_kind": deep["kind"],
                                           "deep_delivery_message": deep["message"][:1200]})
                    else:
                        source_row["fresh_clone_ok"] = fresh.get("ok")
                if deep and deep.get("ok"):
                    winner, annotated = select_replayed_candidate(rows, bootstrap_id)
                    if winner and winner["candidate_id"] == candidate_id:
                        break

                source_row["base_delivery_ok"] = source_row.get("delivery_ok")
                source_row["delivery_ok"] = False
                key = tuple(source_row.get("selection_key") or ())
                source_row["selection_key"] = ((0,) + key[1:]) if key else (0,)
                winner = None

            if winner is None:
                _fallback, annotated = select_replayed_candidate(rows, bootstrap_id)
                if initial_winner:
                    fallback_entry = next(
                        e for e in entries if e["candidate_id"] == initial_winner["candidate_id"])
                    self._lay_snapshot_into_cand(
                        fallback_entry["path"], cleanup_roots=cleanup_roots)
                    self._final_regression_outcomes = outcomes_by_candidate.get(
                        initial_winner["candidate_id"], [])
            report = {**suite, "bootstrap_candidate_id": bootstrap_id,
                      "winner_candidate_id": (winner or {}).get("candidate_id"),
                      "fallback_candidate_id": ((initial_winner or {}).get("candidate_id")
                                                if winner is None else None),
                      "tie_policy": "earliest_snapshot", "bootstrap_floor": "anchor_superset_or_net_gain",

                      "bootstrap_floor_state": (annotated[0].get("bootstrap_floor_state")
                                                if annotated else "VOID_no_candidates"),
                      "bootstrap_floor_enforced": bool(annotated
                                                       and annotated[0].get("bootstrap_floor_enforced")),
                      "deep_delivery_required": True,
                      "final_replay_wall_clock": getattr(self, "final_replay_wall_clock", 1800),
                      "candidates_replayed": len(rows), "candidates_skipped_budget": budget_skipped,
                      "candidates": annotated}
            if annotated and not annotated[0].get("bootstrap_floor_enforced"):
                self._log(f"WARNING bootstrap floor NOT enforced "
                          f"({annotated[0].get('bootstrap_floor_state')}): no usable bootstrap in the "
                          f"replay pool, so no candidate was checked for documented-behaviour "
                          f"regression against the seed")

            _write_json(self.artifact / "candidate_pool_replay.json", report, indent=2)
            if not winner or selected is None:
                self._final_pool_selected = True
                self._log("uniform candidate replay produced no deep-deliverable candidate; "
                          "keeping the best base-delivery fallback")
                return report

            tmp = self.artifact / ".final_selected_candidate.tmp"
            shutil.rmtree(tmp, ignore_errors=True)
            _snapshot_copytree(selected["path"], tmp)
            if (not tmp.is_dir()
                    or self._snapshot_source_digest(tmp) != selected["digest"]):
                raise RuntimeError("selected candidate durable snapshot copy was incomplete")
            best = self.artifact / "best_candidate"
            shutil.rmtree(best, ignore_errors=True)
            tmp.rename(best)
            if self._snapshot_source_digest(best) != selected["digest"]:
                raise RuntimeError("best_candidate digest differs from replay winner")
            selection = {"selection_mode": "uniform_final_replay", "turn": getattr(self, "_turns", 0),
                         "candidate_id": winner["candidate_id"],
                         "snapshot": winner["snapshot"], "suite_sha256": suite["sha256"],
                         "repair_sha256": suite["repair_sha256"],
                         "challenge_sha256": suite["challenge_sha256"],
                         "repair_case_count": suite["repair_case_count"],
                         "challenge_case_count": suite["challenge_case_count"],
                         "diagnostic_case_count": suite["diagnostic_case_count"],
                         "diagnostic_sha256": suite["diagnostic_sha256"],
                         "train_rate": winner.get("train_rate"),
                         "challenge_rate": winner.get("challenge_rate"),
                         "selection_key": winner["selection_key"],
                         "bootstrap_floor_missing": winner.get("bootstrap_floor_missing", []),
                         "bootstrap_floor_state": winner.get("bootstrap_floor_state"),
                         "bootstrap_floor_enforced": winner.get("bootstrap_floor_enforced"),
                         "tie_policy": "earliest_snapshot"}
            _write_json(self.artifact / "candidate_selection.json", selection, indent=2)
            self._best_selection_key = tuple(winner["selection_key"])
            self._best_anchor_pass_set = set(winner.get("anchor_pass_keys") or ())
            self._best_anchor_pass = int(winner.get("anchor_pass", 0) or 0)
            self._final_selected_candidate_id = winner["candidate_id"]
            self._final_regression_outcomes = outcomes_by_candidate.get(
                winner["candidate_id"], [])
            self._final_pool_selection_committed = True
            self._final_pool_selected = True
            self._log(f"uniform candidate replay selected {winner['candidate_id']} from "
                      f"{len(entries)} snapshots on {len(frozen)} repair + "
                      f"{len(challenge)} sealed-challenge cases")
            return report
        except Exception as e:

            import traceback
            self._log(f"uniform candidate replay failed: {type(e).__name__}: {e}\n"
                      + "".join(traceback.format_exc()).rstrip())
            return None
        finally:
            try:
                self._restore_final_replay_corpus(original_corpus)
            except Exception:
                pass
            self._final_pool_selection_in_progress = False

    def _deliver_final_solution(self) -> "Path | None":
        import shutil
        best = self.artifact / "best_candidate"
        p1 = self.artifact / "phase1_candidate"
        if getattr(self, "box", None) and self.box.solved:
            return None
        src_exts = (".py", ".c", ".cpp", ".cc", ".go", ".rs", ".js")
        def _has_src(d):
            return d.exists() and any(p.suffix in src_exts for p in d.rglob("*") if p.is_file())
        committed = getattr(self, "_final_pool_selection_committed", False)
        deliver = best
        if (not committed and _has_src(p1)
                and (_is_placeholder_candidate(best) or getattr(self, "_final_pool_selected", False))):
            deliver = p1
        if not _has_src(deliver):
            return None
        if self.sol_dir.exists():
            shutil.rmtree(self.sol_dir)
        shutil.copytree(deliver, self.sol_dir, symlinks=True)
        (self.sol_dir / "BEST_local_rate.txt").unlink(missing_ok=True)
        if deliver == p1:
            self._log("host-side bootstrap floor: no replay-committed winner and best_candidate "
                      "untrusted/placeholder — delivered phase1_candidate.")
        return deliver

    def _finalize_release(self):

        if (not getattr(self, "final_pool_replay_enabled", False)
                or getattr(self, "_final_pool_selected", False)
                or not getattr(self, "box", None) or not getattr(self, "room", None)):
            return
        try:
            self._sync_solution_out()
            self._select_final_candidate_from_pool()
            self._guard_not_worse_than_p1()
            self._sync_solution_out()
        except Exception as e:
            self._log(f"end-of-run finalization failed: {type(e).__name__}: {e}")

    def _restore_best_into_cand(self):

        try:
            best = self.artifact / "best_candidate"
            if best.exists() and self.box and self.box.corpus:
                reg = self.box.count_regression_failures(limit=1, detail=0)
                if self._required_regression_metrics(reg)["failed"] != 0:
                    self._lay_snapshot_into_cand(best)

        except Exception:
            pass

    def _required_case_keys(self) -> set[str]:
        return {repr(case_identity(c)) for c in (getattr(self.box, "corpus", []) or [])
                if isinstance(c, dict) and c.get("required", True) is not False}

    def _submit_attempt_fingerprint(self, reg: dict) -> str:
        import hashlib
        failing = sorted(str(row.get("identity")) for row in reg.get("case_outcomes") or []
                         if row.get("required", True) is not False
                         and row.get("evaluated") and row.get("passed") is False)
        if not failing:
            failing = sorted(f"{row.get('signature')}:{row.get('count')}"
                             for row in reg.get("clusters") or [])
        payload = {
            "candidate_revision": self._candidate_validation_revision_key(),
            "required_suite": sorted(self._required_case_keys()),
            "failing": failing,
            "metrics": self._required_regression_metrics(reg),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, default=str).encode()
        return hashlib.sha256(raw).hexdigest()

    def _architecture_ceiling_note(self) -> str:
        rows = getattr(self, "_persistent_architecture_clusters", []) or []
        if not rows:
            return ""
        limit = getattr(self, "developer_targeted_max_failures", 12)
        ids = [str(row.get("signature")) for row in rows[:limit]]
        return (" The same large root-cause cluster has survived multiple source revisions. "
                "Stop nibbling individual cases: identify the smallest explicit allowed_files "
                "set and call request_developer(mode=targeted) with failure_ids=" + repr(ids) + ".")

    def _dispatch(self, name: str, args: dict) -> dict:
        terminal_submit = bool(name == "submit" and args.get("_terminal"))

        if name in ("submit", "request_hunt", "request_critic") and self.box.corpus:
            reg = self.box.count_regression_failures()
            primary = self._required_regression_metrics(reg)
            required_failed = primary["failed"]
            shown = []
            for d in reg.get("diffs", []):
                if d.get("required", True) is False:
                    continue
                shown.append(
                    f"  • args={d['args']} stdin={d['stdin']!r} field={d['field']}\n"
                    f"      ORACLE   : {d['oracle']!r}\n"
                    f"      CANDIDATE: {d['candidate']!r}")
            detail = ("\n\nEXACT required divergences to fix:\n" + "\n".join(shown)) if shown else ""
            if name in ("request_hunt", "request_critic") and required_failed:
                return {"blocked": True, "reason": "candidate fails required corpus cases",
                        "regression_failures": required_failed,
                        "examples": [x for x in reg.get("examples", [])
                                     if x.get("required", True) is not False],
                        "note": (f"Fix the {required_failed} current REQUIRED regression(s) before "
                                 "asking the coverage team to add more cases." + detail +
                                 self._architecture_ceiling_note())}
            if name == "submit":
                fingerprint = self._submit_attempt_fingerprint(reg)
                attempted = getattr(self, "_submit_attempt_fingerprints", None)
                if attempted is None:
                    attempted = self._submit_attempt_fingerprints = set()
                if not terminal_submit and fingerprint in attempted:
                    return {"submit_blocked": True, "reason": "duplicate_submit_cooldown",
                            "candidate_failure_fingerprint": fingerprint,
                            "note": ("This exact candidate and required-failure vector already "
                                     "attempted submission. Edit the candidate or add/fix evidence "
                                     "before retrying; repeated submits cannot improve readiness.")}
                if not terminal_submit:
                    attempted.add(fingerprint)

                try:
                    passing, failing_anchors = self.box.anchor_pass_set()
                    current = {repr(x) for x in passing}
                    protected = set(getattr(self, "_best_anchor_pass_set", set()) or set())
                    missing = sorted(protected - current)
                except Exception:
                    failing_anchors, missing = [], []
                if not terminal_submit and (failing_anchors or missing):
                    return {"submit_blocked": True, "reason": "protected_anchor_regression",
                            "failing_anchor_count": len(failing_anchors),
                            "missing_protected_anchor_count": len(missing),
                            "note": "A previously confirmed trunk/documented behavior regressed. "
                                    "Restore or repair it before submission."}
                if not terminal_submit and (primary["timeouts"] or primary["completion_rate"] < 1.0):
                    return {"submit_blocked": True, "reason": "required_replay_incomplete",
                            "required_timeouts": primary["timeouts"],
                            "required_completion_rate": primary["completion_rate"],
                            "note": "Required local replay timed out or did not complete; fix the "
                                    "hang/replay health before submission."}
                floor = float(getattr(self, "submit_required_rate_floor", 0.85))
                if not terminal_submit and primary["pass_rate"] < floor:
                    return {"submit_blocked": True,
                            "reason": "required_local_pass_rate_below_floor",
                            "required_pass_rate": primary["pass_rate"],
                            "required_pass_rate_floor": floor,
                            "required_failures": required_failed,
                            "diagnostic_failures": int(reg.get("diagnostic_failed", 0) or 0),
                            "note": (f"Required local pass rate is {primary['pass_rate']:.1%}, below "
                                     f"the {floor:.0%} submission floor. A 70%-class candidate is "
                                     "not ready: fix the largest root-cause cluster first." + detail +
                                     self._architecture_ceiling_note())}

        if (name in ("run_oracle", "diff")
                and getattr(self, "_turns", 0) >= self.write_by_turn
                and not (getattr(self.box, "_wrote_real_candidate", False)
                         or getattr(self, "_wrote_real_candidate", False))):
            return {"blocked": True, "reason": "no real candidate yet",
                    "note": (f"You are at turn {self._turns} and the candidate is still a stub — you "
                             "have been probing the oracle without implementing. STOP probing. "
                             "You already understand enough: WRITE THE IMPLEMENTATION NOW with "
                             "`bash` (`cat > main.py <<'EOF' … EOF`, then run compile.sh — a real, "
                             "building candidate covering the happy path + the behaviour you've "
                             "observed), then resume differential testing. run_oracle/diff are "
                             "blocked until the candidate has a substantive implementation. bash and "
                             "run_regression remain open.")}
        if name == "diff" and self._phase1_done and is_trivial_surface_case(args):
            probe = {"args": args.get("args") or [], "stdin": args.get("stdin", ""),
                     "stdin_b64": args.get("stdin_b64", ""), "env": args.get("env") or {}}
            duplicate = any(case_identity(probe) == case_identity(case)
                            for case in (self.box.corpus or []))
            cap = int(getattr(self, "surface_case_cap", 16))
            reserve = min(5, max(1, cap // 3))
            non_root = sum(is_trivial_surface_case(case) and not is_root_surface_case(case)
                           for case in (self.box.corpus or []))
            root = sum(is_root_surface_case(case) for case in (self.box.corpus or []))
            saturated = (duplicate or
                         (is_root_surface_case(probe) and root >= reserve) or
                         (not is_root_surface_case(probe)
                          and non_root >= max(0, cap - reserve)))
            if saturated:
                return {"blocked": True, "reason": "help_version_surface_saturated",
                        "note": ("Help/version coverage is already represented in the corpus. "
                                 "Stop spending turns on CLI surface text; probe a real successful "
                                 "workflow with files, repository/service state, artifacts, or "
                                 "command sequences, then implement the resulting behavior. Use "
                                 "run_regression to verify an already-recorded help case after edits.")}
        if name == "diff" and not self._phase1_done:

            args = {**args, "_no_record": True}
            return self.box.call(name, args)

        if (name in ("run_oracle", "diff")
                and self._phase1_done and self.box
                and not getattr(self.box, "submissions", None)
                and not getattr(self.box, "done", False)
                and getattr(self.box, "corpus", None)
                and self._turns >= self._phase2_started_turn + int(self.submit_redirect_frac * self.phase2_max_turns)):
            redirect_reg = self.box.count_regression_failures(limit=1, detail=0)
            if not self._required_regression_metrics(redirect_reg)["failed"]:
                return {"blocked": True, "reason": "converged — submit now",
                        "note": ("Your candidate passes EVERY recorded corpus case and you are deep "
                                 "into phase 2 with ZERO counted submissions — you have been probing "
                                 "instead of submitting. STOP. Call `submit` THIS TURN: this is your "
                                 "ONE counted submission, the adversarial judge panel gates it (a real "
                                 "issue comes back for you to fix and does NOT consume it), and a "
                                 "submission at full internal confidence is strictly better than more "
                                 "probing. run_oracle/diff are blocked until you submit; submit, bash, "
                                 "run_regression remain open.")}
        if name in ("write_file", "bash"):

            before_writes = getattr(self.box, "writes", 0)
            with self._candidate_state_guard():
                lock_state = self._clear_stale_git_index_lock("main_agent_command_start") or {}
                if lock_state.get("present") and not lock_state.get("removed"):
                    return {"blocked": True,
                            "reason": f"candidate_git_lock_{lock_state.get('reason', 'unavailable')}",
                            "note": "A prior git mutation is still active; retry after it finishes."}
                try:
                    result = self.box.call(name, args)
                finally:
                    self._clear_stale_git_index_lock("main_agent_command_end")
            if getattr(self.box, "writes", 0) != before_writes:
                self._invalidate_clone_build_health("main_agent_edit")
            return result
        if name == "bootstrap_done":
            if not self.two_phase:
                return {"error": "two-phase mode not enabled"}
            if self._phase1_done:
                return {"note": "Already in phase 2 — the helper team is live."}

            if not (getattr(self, "_wrote_real_candidate", False)
                    or getattr(self, "box", None) and getattr(self.box, "_wrote_real_candidate", False)):
                return {"bootstrap_blocked": True, "reason": "no candidate yet",
                        "note": ("You have NOT written a real implementation yet — the candidate is empty/a "
                                 "stub, so there is nothing to finalize. WRITE THE IMPLEMENTATION NOW "
                                 "with `bash` (`cat > main.py <<'EOF' … EOF`, then compile.sh — a "
                                 "building candidate covering the happy path + everything you've "
                                 "observed), differentially align it with `diff`, "
                                 "THEN call bootstrap_done.")}

            floor = int(self.phase1_min_turns_frac * self.phase1_max_turns) if self.phase1_min_turns_frac else 0
            if self._turns < floor:
                return {"bootstrap_blocked": True, "reason": "not complete yet",
                        "note": (f"Too early to finalize: you are at turn {self._turns} and must keep "
                                 f"building until at least turn {floor}. Your implementation is almost "
                                 "certainly not yet complete. Right now your job is to implement and "
                                 "differentially align EVERY documented flag, option, and subcommand "
                                 "yourself (read --help + README in full, `diff` each one vs the "
                                 "oracle, fix every mismatch). Keep going.")}
            self._bootstrap_declared = True
            return {"acknowledged": True,
                    "note": ("Implementation finalized. A team of automated reviewers will now "
                             "stress-test your candidate and surface any remaining divergences.")}
        if name == "request_hunt":
            if not self.hunters:
                return {"error": "hunters not enabled for this run"}
            if not self._phase1_done:
                return {"blocked": True, "reason": "phase 1 (solo bootstrap)",
                        "note": ("You are still in PHASE 1: build a strong candidate YOURSELF "
                                 "first by differential testing, then call `bootstrap_done`. "
                                 "The hunter/critic team activates in phase 2.")}
            if self._steered_to_submit:
                return {"blocked": True, "reason": "converged (loop-until-dry)",
                        "note": ("The coverage team already converged "
                                 f"({self._consecutive_dry_sweeps} consecutive sweeps with 0 "
                                 "real divergences) and your candidate passes every recorded "
                                 "case. More sweeps only add passing anchors. STOP probing and "
                                 "`submit` NOW — the judge panel will gate it.")}
            return self._hunt(args.get("reason", "agent requested"))
        if name == "request_critic":
            if not self.critics:
                return {"error": "critics not enabled for this run"}
            if not self._phase1_done:
                return {"blocked": True, "reason": "phase 1 (solo bootstrap)",
                        "note": ("You are still in PHASE 1: build a strong candidate YOURSELF "
                                 "first, then call `bootstrap_done`. The completeness-critic "
                                 "team activates in phase 2.")}
            if self._steered_to_submit:
                return {"blocked": True, "reason": "converged (loop-until-dry)",
                        "note": ("The coverage team already converged "
                                 f"({self._consecutive_dry_sweeps} consecutive sweeps with 0 "
                                 "real divergences) and your candidate passes every recorded "
                                 "case. STOP probing and `submit` NOW — the judge panel gates it.")}
            return self._critic(args.get("reason", "agent requested"))
        if name == "submit" and len(self.box.submissions) < self.max_submissions:
            if ((getattr(self, "hunters", 0) or getattr(self, "critics", 0))
                    and getattr(self, "_sweeps_done", 0) < getattr(self, "sweep_budget_floor", 0)):
                attempted = getattr(self, "_submit_attempt_fingerprints", None)
                if attempted is not None:
                    attempted.discard(locals().get("fingerprint"))
                return {"submit_blocked": True, "reason": "successful_sweep_floor_unmet",
                        "successful_sweeps": getattr(self, "_sweeps_done", 0),
                        "required_successful_sweeps": getattr(self, "sweep_budget_floor", 0),
                        "note": "Coverage-team execution is incomplete; external clone/worker failures do not count toward the sweep floor."}

            if (not terminal_submit and not self.box.submissions
                    and self._seed_diversity() < self.min_seed_diversity_for_submit):
                div = self._seed_diversity()
                sw = self._sweep("seed-floor: grow corpus before first submit")
                return {"submit_blocked": True, "reason": "insufficient seed diversity",
                        "seed_shapes": div, "needed": self.min_seed_diversity_for_submit,
                        "sweep_added": sw.get("new_divergence_cases", 0),
                        "note": (f"Your corpus has only {div} distinct input SHAPES — too few for "
                                 "the readiness fuzzer to construct the structural edge cases that "
                                 "break candidates (it mutates EXISTING seeds, so it can't probe "
                                 "shapes you've never recorded). A coverage sweep was run to grow "
                                 "the corpus. Keep differentially probing NEW input shapes "
                                 "(different flag combos, nested/empty/missing JSON fields, "
                                 "malformed structures) with `diff`/`request_hunt` to widen "
                                 "coverage, THEN submit. This gate did NOT consume your submission.")}

            gate_real = 0; sources = []

            self._heartbeat("submit-gate: delivery smoke")
            sm = self.box._delivery_smoke()
            if not sm["ok"]:
                return {"submit_blocked": True, "reason": "undeliverable candidate",
                        "submissions_remaining": self.max_submissions - len(self.box.submissions),
                        "note": ("Your candidate would score 0 at the exam even though it runs here: "
                                 + sm["message"] + " Fix compile.sh / the executable, rebuild, then "
                                 "submit — this gate did NOT consume your submission.")}
            self._heartbeat("submit-gate: fuzz burst")
            before_all = len(self.box.corpus)
            before_required = self._required_case_keys()
            fz_reported = self._fuzz_round(burst=True)
            fuzz_health = getattr(self, "_last_fuzz_round_result", None)

            fuzz_inconclusive = (fuzz_health is not None and
                                 (fuzz_health.get("status") in ("failed", "partial")
                                  or int(fuzz_health.get("executed_lenses", 1) or 0) == 0))

            if fuzz_inconclusive and not terminal_submit:
                attempted = getattr(self, "_submit_attempt_fingerprints", None)
                if attempted is not None:
                    attempted.discard(locals().get("fingerprint"))
                return {"submit_blocked": True, "reason": "fuzz_infrastructure_inconclusive",
                        "note": "The required submit-time fuzz pass did not execute cleanly; retry after sandbox/build infrastructure recovers.",
                        "fuzz_status": fuzz_health.get("status"), "fuzz_error": fuzz_health.get("error")}
            if fuzz_inconclusive:
                self._terminal_gate_infra = {"fuzz_status": fuzz_health.get("status"),
                                             "fuzz_error": fuzz_health.get("error")}
            after_required = self._required_case_keys()

            fz = (len(after_required - before_required)
                  if len(self.box.corpus) != before_all else fz_reported)
            if fz:
                gate_real += fz; sources.append(f"fuzz-burst:{fz}")
            judge_hit = 0
            if self.judges:
                self._heartbeat("submit-gate: judge panel")
                before_all = len(self.box.corpus)
                before_required = self._required_case_keys()
                verdict = self._judge()
                if verdict.get("inconclusive"):
                    if not terminal_submit:
                        attempted = getattr(self, "_submit_attempt_fingerprints", None)
                        if attempted is not None:
                            attempted.discard(locals().get("fingerprint"))
                        return {"submit_blocked": True, "reason": "judge_infrastructure_inconclusive",
                                "note": "The adversarial judge panel did not execute cleanly; retry after sandbox/build infrastructure recovers.",
                                "judge_reason": verdict.get("reason")}
                    self._terminal_gate_infra = {**(getattr(self, "_terminal_gate_infra", None) or {}),
                                                 "judge_reason": verdict.get("reason")}
                if not verdict["clean"]:
                    judge_hit = (len(self._required_case_keys() - before_required)
                                 if len(self.box.corpus) != before_all
                                 else verdict["new_cases"])
                if judge_hit:
                    gate_real += judge_hit; sources.append(f"judges:{judge_hit}")
            self._heartbeat("submit-gate: done")
            if gate_real:
                if terminal_submit:

                    self._terminal_gate_findings = {"new_divergence_cases": gate_real,
                                                    "broken_by": sources,
                                                    "fuzz": fz, "judge": judge_hit}
                else:

                    trickle = (judge_hit == 0 and 0 < fz <= self.gate_trickle_max)
                    self._gate_trickle_streak = self._gate_trickle_streak + 1 if trickle else 0
                    if not (trickle and self._gate_trickle_streak >= self.gate_converge_streak):
                        return {"submit_blocked": True,
                                "new_divergence_cases": gate_real, "broken_by": sources,
                                "submissions_remaining": self.max_submissions - len(self.box.submissions),
                                "note": (f"Readiness gate BROKE your candidate ({', '.join(sources)} new "
                                         "divergence case(s) added to the corpus) — this gate did NOT "
                                         "consume your submission. The mechanical fuzzer and/or adversarial "
                                         "judges found inputs where you differ from the oracle. Run "
                                         "`run_regression`, fix EVERY divergence (edit the source with "
                                         "`bash`), then submit. Do not submit until run_regression is "
                                         "fully clean.")}

        return self.box.call(name, args)

    def _phase_durations(self) -> dict:

        now = time.time()
        start = self._loop_started_at
        if start is None:
            return {"phase1": 0.0, "phase2": 0.0, "total": 0.0}
        p2_start = self._phase2_started_at
        if not self.two_phase:
            return {"phase1": 0.0, "phase2": round(now - start, 1), "total": round(now - start, 1)}
        if p2_start is None:
            return {"phase1": round(now - start, 1), "phase2": 0.0, "total": round(now - start, 1)}
        return {"phase1": round(p2_start - start, 1), "phase2": round(now - p2_start, 1),
                "total": round(now - start, 1)}

    def _final_result(self, turns: int) -> dict:
        subs = self.box.submissions
        best_sub = max((s["pass_rate"] or 0 for s in subs), default=0.0)
        solved_at = next((s["submission"] for s in subs if s["solved"]), None)
        final = {
            "instance_id": self.instance_id,
            "solved": self.box.solved,
            "solved_at_submission": solved_at,
            "pass_at_1": self.box.solved,
            "best_pass_rate": best_sub,
            "best_submission_rate": best_sub,
            "pass_rate": (subs[-1]["pass_rate"] if subs else None),
            "submissions": subs,
            "n_submissions": len(subs),
            "turns": turns,
            "corpus_size": len(self.box.corpus),
            "sealed_challenge_size": len(getattr(self, "_sealed_challenge", []) or []),
            "sealed_challenge_contaminated": getattr(
                self, "_sealed_challenge_contaminated", 0),
            "flaky_count": len(self.box.flaky),
            "flaky_sample": self.box.flaky[:10],
            "gate_trickle_streak": getattr(self, "_gate_trickle_streak", 0),
            "two_phase": False if self.plan_a else self.two_phase,
            "reached_phase2": None if self.plan_a else (self._phase1_done if self.two_phase else None),
            "bootstrap_declared": self._bootstrap_declared,
            "phase_seconds": self._phase_durations(),
            "plan_a": self.plan_a,
            "dev_runs": getattr(self, "_dev_runs_used", 0),

            "ctx_trims": getattr(self, "_ctx_trims", 0),
            "ctx_trim_tokens": getattr(self, "ctx_trim_tokens", 0),
            "selected_candidate": (json.loads((self.artifact / "candidate_selection.json").read_text())
                                   if (self.artifact / "candidate_selection.json").exists() else None),
            "hunt_rounds": self.hunt_rounds_used,
            "hunt_attempts": getattr(self, "_sweep_attempts", self.hunt_rounds_used),
            "executed_sweeps": getattr(self, "_sweeps_executed", 0),
            "successful_sweeps": getattr(self, "_sweeps_done", 0),
            "successful_fuzz_rounds": getattr(self, "_successful_fuzz_rounds", 0),
            "successful_judge_panels": getattr(self, "_successful_judge_panels", 0),
            "sweep_budget_floor": getattr(self, "sweep_budget_floor", 0),
            "generation_complete": (bool(subs)
                                    and getattr(self, "_sweeps_done", 0) >= getattr(self, "sweep_budget_floor", 0)
                                    and (self.fuzz_seeds <= 0 or getattr(self, "_successful_fuzz_rounds", 0) > 0)
                                    and (self.judges <= 0 or getattr(self, "_successful_judge_panels", 0) > 0)),
            "terminal_submit_result": getattr(self, "_terminal_submit_result", None),
            "terminal_gate_findings": getattr(self, "_terminal_gate_findings", None),

            "terminal_gate_infra": getattr(self, "_terminal_gate_infra", None),
            "hunt_log": self.hunt_log,
            "llm_calls": self.llm.calls,
            "usage": self.llm.usage.as_dict(),
            "usage_by_provider": self._provider_ledger(),
        }
        return final

    def _provider_ledger(self) -> dict:

        from llm import UsageLedger as _UL
        led = _UL()

        sub_tot = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0, "calls": 0}
        for prov, u in self._ledger.by_provider.items():
            led.add(prov, u.as_dict(), self._ledger.calls.get(prov, 0))
            for k in ("input_tokens", "output_tokens", "cache_read", "cache_write"):
                sub_tot[k] += getattr(u, k)
            sub_tot["calls"] += self._ledger.calls.get(prov, 0)

        tot = self.llm.usage.as_dict()
        impl = {k: max(0, tot.get(k, 0) - sub_tot[k]) for k in
                ("input_tokens", "output_tokens", "cache_read", "cache_write")}
        impl_calls = max(0, self.llm.calls - sub_tot["calls"])
        led.add(self.llm.cfg.provider, impl, impl_calls)
        return led.as_dict()

    def _record_turn(self, turn: int, *, assistant=None, tool_results=None, **extra):

        if assistant is not None:
            entry = {"turn": turn, "assistant": assistant}
        elif tool_results is not None:
            entry = {"turn": turn, "tool_results": tool_results}
        else:
            entry = {"turn": turn, **extra}
        entry["at"] = round(time.time(), 3)

        sink = getattr(self, "_record_sink", None)
        if getattr(self, "plan_a", False):
            entry["ctx"] = "developer" if sink is not None else "implementer"
        else:
            entry["phase"] = 2 if getattr(self, "_phase1_done", False) else 1
        if sink is not None:
            sink["list"].append(entry)
            with open(sink["path"], "a") as f:
                f.write(json.dumps(entry) + "\n")
                f.flush()
            return
        self.transcript.append(entry)
        with (self.artifact / "transcript.jsonl").open("a") as f:
            f.write(json.dumps(entry) + "\n")
            f.flush()

    def _record_subagents(self, kind: str, agents: list) -> None:
        if not agents:
            return
        sink, self._record_sink = getattr(self, "_record_sink", None), None
        try:
            self._record_turn(getattr(self, "_turns", 0), subagents={"kind": kind, "agents": agents})
        except Exception as e:
            self._log(f"[traj] sub-agent record failed: {e}")
        finally:
            self._record_sink = sink


    def _atif_model(self):

        cfg = getattr(getattr(self, "llm", None), "cfg", None)
        return getattr(cfg, "model", None)


    def _dump_transcript(self):
        (self.artifact / "transcript.json").write_text(json.dumps(self.transcript))

        try:
            import atif
            (self.artifact / "transcript.atif.json").write_text(json.dumps(
                atif.main_trajectory(self.transcript, artifact=self.artifact,
                                     instance_id=self.instance_id,
                                     model_name=self._atif_model()),
                ensure_ascii=False)[:32_000_000])
        except Exception as e:
            self._log(f"[atif] main transcript: {type(e).__name__}: {e}")

        try:
            (self.artifact / "score.json").write_text(json.dumps(self._final_result(
                getattr(self, "_turns", 0)), indent=2)[:5_000_000])
        except Exception:
            pass

    def _heartbeat(self, stage: str):
        try:
            (self.artifact / "heartbeat.json").write_text(json.dumps(
                {"stage": stage, "turn": getattr(self, "_turns", 0), "at": time.time(),
                 "corpus": len(self.box.corpus), "flaky": len(self.box.flaky),
                 "hunt_rounds": self.hunt_rounds_used}))
        except Exception:
            pass
