#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUNS = REPO / "runs"

sys.path.insert(0, str(REPO / "src"))
from monitor import snapshot
from upstream import add_programbench_to_path

add_programbench_to_path()
from programbench.eval.eval import EvaluationResult
from programbench.utils.load_data import (
    get_active_branches,
    get_ignored_tests,
    load_all_instances,
)


def docker(*args: str, timeout: int = 1800) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args],
                          capture_output=True, text=True, timeout=timeout)


TASK_TAG = "task" + (("_" + os.environ["PB_IMAGE_TAG"]) if os.environ.get("PB_IMAGE_TAG") else "")


def guard_or_die(stage: str) -> None:
    ok, line = snapshot()
    print(f"[guard:{stage}] {line}", flush=True)
    if not ok:
        raise RuntimeError(f"ABORT before {stage}: resource guard tripped")


def ensure_task_image(image: str) -> None:
    ref = f"{image}:{TASK_TAG}"
    if docker("image", "inspect", ref, timeout=60).returncode == 0:
        print(f"[image] {ref} present", flush=True)
        return
    guard_or_die(f"pull {ref}")
    print(f"[image] pulling {ref} ...", flush=True)
    r = docker("pull", ref, timeout=2400)
    if r.returncode != 0:
        raise RuntimeError(f"image pull failed: {r.stderr.strip()[-400:]}")


def pack_submission(solution_dir: Path, instance_id: str) -> Path:
    run_dir = RUNS / instance_id / instance_id
    run_dir.mkdir(parents=True, exist_ok=True)
    archive = run_dir / "submission.tar.gz"
    src = solution_dir
    files = sorted(p for p in src.rglob("*")
                   if p.is_file() and "__pycache__" not in p.parts and p.name != "env_setup.sh")
    with tarfile.open(archive, "w:gz") as tar:
        for f in files:
            tar.add(f, arcname=str(f.relative_to(src)))
    print(f"[pack] {archive} ({len(files)} files)", flush=True)
    return archive


def official_score(result_dict: dict, instance: dict) -> dict:
    res = EvaluationResult.model_validate(result_dict)
    off = res.for_branches(get_active_branches(instance)).without_ignored(get_ignored_tests(instance))
    by_status: dict[str, int] = {}
    for t in off.test_results:
        by_status[t.status] = by_status.get(t.status, 0) + 1
    total = len(off)
    return {
        "resolved": off.n_resolved,
        "total": total,
        "pass_rate": round(off.n_resolved / total, 4) if total else 0.0,
        "solved": total > 0 and off.n_resolved == total,
        "by_status": by_status,
        "error_code": result_dict.get("error_code"),
        "executable_hash": result_dict.get("executable_hash"),
    }


def run_exam(instance_id: str, solution_dir: Path, *, docker_cpus: int = 6,
             keep_image: bool = True, timeout: int = 7200) -> dict:
    instance = {i["instance_id"]: i for i in load_all_instances()}[instance_id]
    image = instance["image_name"]
    run_root = RUNS / instance_id

    pack_submission(solution_dir, instance_id)
    ensure_task_image(image)
    guard_or_die("eval")

    tmpdir = REPO / "tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    tmpdir.chmod(0o755)
    env = {
        "PATH": os.environ["PATH"],
        "PROGRAMBENCH_DOCKER_EXECUTABLE": "docker",
        "HOME": os.environ["HOME"],
        "TMPDIR": str(tmpdir),
        "HF_HUB_DISABLE_PROGRESS_BARS": "1",
    }
    cmd = ["uv", "run", "programbench", "eval", str(run_root),
           "--workers", "1", "--branch-workers", "1", "--image-tag", TASK_TAG,
           "--docker-cpus", str(docker_cpus), "--branch-retries", "1", "--force"]
    print(f"[exam] {' '.join(cmd)}", flush=True)

    proc = subprocess.Popen(cmd, cwd=REPO, env=env, start_new_session=True)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        _kill_eval_containers(instance_id)
        return {"resolved": 0, "total": 0, "pass_rate": 0.0, "solved": False,
                "by_status": {}, "error_code": f"eval_timeout_{timeout}s",
                "eval_json": None,
                "instance": {k: instance.get(k) for k in ("language", "difficulty", "repository")}}

    eval_json = run_root / instance_id / f"{instance_id}.eval.json"
    result = json.loads(eval_json.read_text())
    score = official_score(result, instance)
    score["eval_json"] = str(eval_json)
    score["instance"] = {k: instance.get(k) for k in ("language", "difficulty", "repository")}

    if not keep_image:
        guard_or_die("cleanup")
        docker("rmi", "-f", f"{image}:task", timeout=120)
        docker("image", "prune", "-f", timeout=120)
    return score


def _kill_eval_containers(instance_id: str) -> None:
    img_substr = instance_id.replace("__", "_1776_")
    r = docker("ps", "--format", "{{.Names}} {{.Image}}", timeout=30)
    for line in (r.stdout or "").splitlines():
        name, _, image = line.partition(" ")
        if instance_id in image or img_substr in image or "programbench-compiled" in image:
            docker("rm", "-f", name, timeout=60)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("instance_id")
    ap.add_argument("solution_dir", type=Path)
    ap.add_argument("--docker-cpus", type=int, default=6)
    ap.add_argument("--rm-image", action="store_true")
    a = ap.parse_args()
    s = run_exam(a.instance_id, a.solution_dir, docker_cpus=a.docker_cpus, keep_image=not a.rm_image)
    print(json.dumps(s, indent=2))
    raise SystemExit(0 if s["solved"] else 1)