#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "src"))

from llm import LLMClient
from orchestrator import Orchestrator
import run_task
from upstream import add_programbench_to_path

add_programbench_to_path()
from programbench.utils.load_data import load_all_instances

_DIFF_ORDER = {"easy": 0, "medium": 1, "hard": 2, None: 3}


def _total_ram_kb() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1])
    return 0


def _avail_ram_kb() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1])
    return 0


def _free_disk_gb() -> float:
    import shutil
    return shutil.disk_usage(str(REPO)).free / 1e9


class RamGovernor:

    def __init__(self, cap_frac: float = 0.65, per_agent_kb: int = 1_200_000,
                 min_disk_gb: float = 14.0, max_concurrent_evals: int = 3,
                 eval_headroom_kb: int = 6_000_000):
        self.total = _total_ram_kb()
        self.ceiling = int(self.total * cap_frac)
        self.per_agent = per_agent_kb
        self.min_disk_gb = min_disk_gb
        self.eval_headroom = eval_headroom_kb
        self._lock = threading.Lock()
        self._reserved = 0
        self._eval_sema = threading.Semaphore(max_concurrent_evals)
        self._eval_lock = threading.Lock()

    def acquire(self, poll: float = 5.0, timeout: float = 7200) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                used = self.total - _avail_ram_kb()
                ram_ok = used + self._reserved + self.per_agent <= self.ceiling
                disk_ok = _free_disk_gb() >= self.min_disk_gb
                if ram_ok and disk_ok:
                    self._reserved += self.per_agent
                    return True
            time.sleep(poll)
        return False

    def release(self):
        with self._lock:
            self._reserved = max(0, self._reserved - self.per_agent)

    def eval_enter(self, poll: float = 4.0, timeout: float = 5400) -> bool:
        if not self._eval_sema.acquire(timeout=timeout):
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            avail = _avail_ram_kb()
            used = self.total - avail

            if used + self.eval_headroom <= self.ceiling:
                return True
            time.sleep(poll)
        self._eval_sema.release()
        return False

    def eval_exit(self):
        self._eval_sema.release()

    def status(self) -> str:
        used = self.total - _avail_ram_kb()
        return (f"used={used//1024}M reserved={self._reserved//1024}M "
                f"ceiling={self.ceiling//1024}M total={self.total//1024}M "
                f"free_disk={_free_disk_gb():.0f}G")


def select_tasks(difficulties: list[str], limit: int, only: list[str] | None) -> list[dict]:
    inst = load_all_instances()
    if only:
        idx = {i["instance_id"]: i for i in inst}
        return [idx[o] for o in only if o in idx]
    want = set(difficulties)
    sel = [i for i in inst if (i.get("difficulty") or "none").lower() in want
           or (i.get("difficulty") in difficulties)]
    sel.sort(key=lambda i: (_DIFF_ORDER.get(i.get("difficulty"), 3), i["instance_id"]))
    if limit:
        sel = sel[:limit]
    return sel


def run_one(seq: int, instance: dict, run_dir: Path, cfg: dict, gov: RamGovernor) -> dict:
    iid = instance["instance_id"]
    art = run_dir / f"{seq:03d}_{iid}"
    art.mkdir(parents=True, exist_ok=True)
    rec = {"seq": seq, "instance": iid, "difficulty": instance.get("difficulty"),
           "language": instance.get("language"), "solved": False, "solved_at": None,
           "best_pass_rate": 0.0, "n_submissions": 0, "error": None,
           "wall_clock_s": 0.0, "usage": {}}
    if not gov.acquire():
        rec["error"] = "ram governor timeout"
        (art / "score.json").write_text(json.dumps(rec, indent=2))
        return rec
    t0 = time.time()
    try:
        llm = LLMClient()
        orch = Orchestrator(iid, llm, max_turns=cfg["max_turns"],
                            max_submissions=cfg["max_submissions"],
                            wall_clock_s=cfg["wall_clock"], docker_cpus=cfg["docker_cpus"],
                            hunters=cfg["hunters"], critics=cfg["critics"],
                            judges=cfg["judges"],
                            artifact_dir=art,
                            eval_gate=(gov.eval_enter, gov.eval_exit))
        score = orch.run()
        rec.update({
            "solved": bool(score.get("solved")),
            "solved_at": score.get("solved_at_submission"),
            "best_pass_rate": score.get("best_pass_rate", 0.0),
            "n_submissions": score.get("n_submissions", 0),
            "submissions": score.get("submissions", []),
            "turns": score.get("turns"),
            "usage": score.get("usage", {}),
            "error": score.get("error"),
        })
        (art / "score.json").write_text(json.dumps(score, indent=2))
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {e}"
        (art / "error.txt").write_text(traceback.format_exc())
    finally:
        rec["wall_clock_s"] = round(time.time() - t0, 1)
        gov.release()
        if not cfg.get("keep_images"):
            _reclaim_task_images(iid)
    return rec


def _reclaim_task_images(instance_id: str):
    import subprocess
    base = f"programbench/{instance_id.replace('__', '_1776_')}"
    for tag in ("task_cleanroom", "task"):
        subprocess.run(["docker", "rmi", "-f", f"{base}:{tag}"],
                       capture_output=True, timeout=180)
    subprocess.run(["docker", "image", "prune", "-f"],
                   capture_output=True, timeout=180)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--difficulty", default="easy,medium,hard",
                    help="comma list: easy,medium,hard")
    ap.add_argument("--only", default="", help="comma list of explicit instance_ids")
    ap.add_argument("--limit", type=int, default=0, help="cap # tasks (0 = all)")
    ap.add_argument("--max-parallel", type=int, default=10)
    ap.add_argument("--run-id", default="")
    a = ap.parse_args()

    difficulties = [d.strip() for d in a.difficulty.split(",") if d.strip()]
    only = [s.strip() for s in a.only.split(",") if s.strip()] or None
    tasks = select_tasks(difficulties, a.limit, only)

    run_id = a.run_id or f"codemaps-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    run_dir = REPO / "experiments" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    cfg = {"max_turns": run_task.MAX_TURNS, "max_submissions": run_task.MAX_SUBMISSIONS,
           "wall_clock": run_task.WALL_CLOCK_S, "docker_cpus": run_task.DOCKER_CPUS,
           "hunters": run_task.HUNTERS, "critics": run_task.CRITICS, "judges": run_task.JUDGES,
           "keep_images": True}
    (run_dir / "manifest.json").write_text(json.dumps({
        "run_id": run_id, "config": cfg, "difficulties": difficulties,
        "n_tasks": len(tasks),
        "tasks": [{"seq": i + 1, "instance": t["instance_id"],
                   "difficulty": t.get("difficulty"), "language": t.get("language")}
                  for i, t in enumerate(tasks)],
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, indent=2))

    print(f"[experiment] run_id={run_id}  tasks={len(tasks)}  parallel={a.max_parallel}",
          flush=True)

    gov = RamGovernor()
    results: list[dict] = []
    lock = threading.Lock()

    def task_wrapper(args):
        seq, inst = args
        print(f"[start {seq:03d}] {inst['instance_id']} ({inst.get('difficulty')}) | RAM {gov.status()}", flush=True)
        r = run_one(seq, inst, run_dir, cfg, gov)
        with lock:
            results.append(r)
            _write_summary(run_dir, results)
        print(f"[done  {seq:03d}] {inst['instance_id']} solved={r['solved']} "
              f"@{r['solved_at']} best={r['best_pass_rate']:.3f} {r['wall_clock_s']}s "
              f"err={r['error']}", flush=True)
        return r

    with ThreadPoolExecutor(max_workers=a.max_parallel) as ex:
        list(ex.map(task_wrapper, list(enumerate(tasks, 1))))

    _write_summary(run_dir, results)
    n_solved = sum(1 for r in results if r["solved"])
    mean_best = sum(r["best_pass_rate"] for r in results) / max(1, len(results))
    print(f"\n[experiment] DONE  pass@{a.max_submissions}: {n_solved}/{len(results)} "
          f"| mean best pass_rate: {mean_best:.3f}  | dir: {run_dir}", flush=True)
    (run_dir / "final.json").write_text(json.dumps({
        "run_id": run_id, "n_tasks": len(results), "pass_at_k": n_solved,
        "k": a.max_submissions, "mean_best_pass_rate": mean_best,
        "finished": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, indent=2))
    return 0


def _write_summary(run_dir: Path, results: list[dict]):
    rows = sorted(results, key=lambda r: r["seq"])
    with (run_dir / "summary.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["seq", "instance", "difficulty", "language", "solved", "solved_at",
                    "best_pass_rate", "n_submissions", "turns", "wall_clock_s",
                    "in_tok", "out_tok", "error"])
        for r in rows:
            u = r.get("usage", {})
            w.writerow([r["seq"], r["instance"], r.get("difficulty"), r.get("language"),
                        r["solved"], r.get("solved_at"), f"{r['best_pass_rate']:.4f}",
                        r.get("n_submissions"), r.get("turns"), r.get("wall_clock_s"),
                        u.get("input_tokens", 0), u.get("output_tokens", 0), r.get("error")])


if __name__ == "__main__":
    raise SystemExit(main())