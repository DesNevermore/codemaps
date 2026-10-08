from __future__ import annotations

import json
import os
import subprocess
import shlex
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DOCKER = ["docker"]
WORKSPACE = "/workspace"


CAND = "/workspace"
CAND_EXE = "/workspace/executable"
ORACLE = "/workspace/.oracle_ref"


def image_for(instance_id: str) -> str:
    return f"programbench/{instance_id.replace('__', '_1776_')}"


import os as _os
_TAG_SUFFIX = (("_" + _os.environ["PB_IMAGE_TAG"]) if _os.environ.get("PB_IMAGE_TAG") else "")
CLEANROOM_TAG = "task_cleanroom" + _TAG_SUFFIX
TASK_TAG = "task" + _TAG_SUFFIX


def record_exec_timing(backend: str, argv, t0: float, t1: float, code: int | None = None) -> None:
    path = os.environ.get("PB_EXEC_TIMING_LOG")
    if not path:
        return
    try:
        head = (argv[0] if isinstance(argv, (list, tuple)) and argv else str(argv))[:60]
        with open(path, "a") as fh:
            fh.write(json.dumps({"at": round(t0, 3), "wall_s": round(t1 - t0, 3),
                                 "backend": backend, "argv0": head, "code": code,
                                 "role": os.environ.get("PB_LLM_TIMING_ROLE", "main")},
                                ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError):
        pass


@dataclass
class ExecResult:
    stdout: bytes
    stderr: bytes
    code: int

    def text(self) -> str:
        return self.stdout.decode("utf-8", "replace")


DEFAULT_ENV = {"PAGER": "cat", "MANPAGER": "cat", "LESS": "-R",
               "PIP_PROGRESS_BAR": "off", "TQDM_DISABLE": "1",
               "TERM": "xterm"}


class Cleanroom:
    def __init__(self, instance_id: str, cpus: int = 4, memory: str = "4g",
                 inject_cargo: bool = False):
        self.instance_id = instance_id
        self.image = image_for(instance_id) + ":" + CLEANROOM_TAG
        self.name = "pb-re-" + uuid.uuid4().hex[:10]
        self.cpus = cpus
        self.memory = memory
        self.inject_cargo = inject_cargo
        self.started = False
        self._died = False

    def ensure_image(self) -> None:
        ref = self.image
        if subprocess.run(DOCKER + ["image", "inspect", ref],
                          capture_output=True).returncode == 0:
            return
        r = subprocess.run(DOCKER + ["pull", ref], capture_output=True, text=True, timeout=2400)
        if r.returncode != 0:
            raise RuntimeError(f"pull failed for {ref}: {r.stderr[-400:]}")

    def start(self) -> None:
        self.ensure_image()
        subprocess.run(DOCKER + ["rm", "-f", self.name], capture_output=True)
        r = subprocess.run(
            DOCKER + ["run", "-d", "--name", self.name, "--network", "none",
                      "--cpus", str(self.cpus), "--memory", self.memory,
                      self.image, "sleep", "infinity"],
            capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(f"container start failed: {r.stderr[-400:]}")
        self.started = True

        self.exec(["cp", "-a", "/workspace/executable", ORACLE])

        self.exec(["chmod", "0111", ORACLE])
        self.exec(["mkdir", "-p", "/tmp/work"])

        self.baseline_files = self._list_workspace_files()

        self.sh('git config --global user.name "mini-swe-agent" && '
                'git config --global user.email "mini-swe-agent@proton.me"', timeout=15)
        self.apply_env_setup()
        if self.inject_cargo:
            self._inject_cargo_registry()

    def _list_workspace_files(self) -> set[str]:

        r = self.exec(["sh", "-c",
                       "cd /workspace && find . -type f -not -path './.git/*' 2>/dev/null"])
        if r.code != 0:
            return set()
        return {line.lstrip("./") for line in r.stdout.decode("utf-8", "replace").splitlines() if line.strip()}

    def apply_env_setup(self) -> None:

        if self.exec(["test", "-f", "/workspace/env_setup.sh"]).code != 0:
            return
        try:
            self.exec(["sh", "-c", "cd /workspace && sh env_setup.sh"], timeout=120)
        except Exception:
            pass

    def _inject_cargo_registry(self) -> None:

        task_img = image_for(self.instance_id) + ":" + TASK_TAG
        if subprocess.run(DOCKER + ["image", "inspect", task_img],
                          capture_output=True).returncode != 0:
            return
        tmp = "pb-cargo-" + uuid.uuid4().hex[:8]
        subprocess.run(DOCKER + ["create", "--name", tmp, task_img], capture_output=True, timeout=120)
        try:
            tar = subprocess.run(DOCKER + ["cp", f"{tmp}:/usr/local/cargo/registry", "-"],
                                 capture_output=True, timeout=300)
            if tar.returncode == 0 and tar.stdout:
                self.exec(["mkdir", "-p", "/usr/local/cargo"])
                subprocess.run(DOCKER + ["cp", "-", f"{self.name}:/usr/local/cargo/"],
                               input=tar.stdout, capture_output=True, timeout=300)
        finally:
            subprocess.run(DOCKER + ["rm", "-f", tmp], capture_output=True)

    def stop(self) -> None:
        if self.started:
            subprocess.run(DOCKER + ["rm", "-f", self.name], capture_output=True)
            self.started = False

    def __enter__(self):
        self.start(); return self

    def __exit__(self, *a):
        self.stop()

    def exec(self, argv: list[str], stdin: bytes | None = None,
             workdir: str | None = None, timeout: int = 60,
             out_cap: int | None = None, argv0: str | None = None,
             env: dict | None = None) -> ExecResult:
        t0 = time.time()
        res = None
        try:
            res = self._exec_inner(argv, stdin, workdir, timeout, out_cap, argv0, env)
            return res
        finally:
            record_exec_timing("docker", argv, t0, time.time(),
                               getattr(res, "code", None))

    def _exec_inner(self, argv: list[str], stdin: bytes | None = None,
                    workdir: str | None = None, timeout: int = 60,
                    out_cap: int | None = None, argv0: str | None = None,
                    env: dict | None = None) -> ExecResult:
        cmd = DOCKER + ["exec", "-i"]
        if workdir:
            cmd += ["-w", workdir]

        for k, v in {**DEFAULT_ENV, **(env or {})}.items():
            cmd += ["-e", f"{k}={v}"]

        if argv0 is not None:
            body = "exec -a " + shlex.quote(argv0) + " " + " ".join(shlex.quote(a) for a in argv)
            inner = f"( {body} )"
        else:
            inner = " ".join(shlex.quote(a) for a in argv)
        if out_cap:

            inner = f"{inner} | head -c {out_cap}; exit ${{PIPESTATUS[0]}}"
        guarded = f"timeout -s KILL {timeout}s bash -c {shlex.quote(inner)}"
        cmd += [self.name, "bash", "-c", guarded]
        try:
            p = subprocess.run(cmd, input=stdin, capture_output=True, timeout=timeout + 15)
        except subprocess.TimeoutExpired as e:

            self._reap(argv)
            return ExecResult(e.stdout or b"", (e.stderr or b"") +
                              f"\n[timeout after {timeout}s]".encode(), 124)

        if p.returncode == 137:
            self._reap(argv)
            return ExecResult(p.stdout, (p.stderr or b"") +
                              f"\n[timeout after {timeout}s — killed in container]".encode(), 124)

        if p.returncode != 0 and (b"No such container" in (p.stderr or b"")
                                  or b"is not running" in (p.stderr or b"")):
            self._died = True
        return ExecResult(p.stdout, p.stderr, p.returncode)

    def bash_login(self, command: str, *, cwd: str = "/", env: dict | None = None,
                   timeout: int = 120) -> ExecResult:
        cmd = DOCKER + ["exec", "-i", "-w", cwd]
        for k, v in {**DEFAULT_ENV, **(env or {})}.items():
            cmd += ["-e", f"{k}={v}"]
        guarded = f"timeout -s KILL {timeout}s bash -lc {shlex.quote(command)}"
        cmd += [self.name, "bash", "-lc", guarded]
        try:
            p = subprocess.run(cmd, capture_output=False, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=timeout + 15)
        except subprocess.TimeoutExpired as e:
            self._reap([command])
            return ExecResult((e.stdout or b"") + f"\n[timeout after {timeout}s]".encode(), b"", 124)
        if p.returncode == 137:
            self._reap([command])
            return ExecResult((p.stdout or b"") + f"\n[timeout after {timeout}s — killed]".encode(), b"", 124)
        if p.returncode != 0 and (b"No such container" in (p.stdout or b"")
                                  or b"is not running" in (p.stdout or b"")):
            self._died = True
        return ExecResult(p.stdout or b"", b"", p.returncode)

    def alive(self) -> bool:

        if self._died:
            return False
        r = subprocess.run(DOCKER + ["inspect", "-f", "{{.State.Running}}", self.name],
                           capture_output=True, text=True)
        if r.returncode != 0 or "true" not in r.stdout:
            self._died = True
            return False
        return True

    def _reap(self, argv: list[str]) -> None:

        subprocess.run(
            DOCKER + ["exec", self.name, "sh", "-c",
                      'self=$$; for d in /proc/[0-9]*; do p=${d#/proc/}; '
                      '[ "$p" = 1 ] && continue; [ "$p" = "$self" ] && continue; '
                      'kill -9 "$p" 2>/dev/null; done; true'],
            capture_output=True, timeout=20)

    def sh(self, script: str, stdin: bytes | None = None, timeout: int = 60) -> ExecResult:
        return self.exec(["sh", "-c", script], stdin=stdin, timeout=timeout)

    def write_file(self, container_path: str, content) -> None:

        if isinstance(content, str):
            content = content.encode("utf-8")
        parent = str(Path(container_path).parent)
        self.exec(["mkdir", "-p", parent])
        self.exec(["sh", "-c", f"cat > {shlex.quote(container_path)}"], stdin=content)

    def write_bytes(self, abs_path: str, data: bytes) -> None:
        self.write_file(abs_path, data)

    def read_file(self, container_path: str, timeout: int = 60) -> bytes:
        r = self.exec(["cat", container_path], timeout=timeout)
        return r.stdout

    def read_bytes(self, abs_path: str) -> bytes:
        return self.read_file(abs_path)

    def cp_in(self, host_path: Path, container_path: str) -> None:
        subprocess.run(DOCKER + ["cp", str(host_path), f"{self.name}:{container_path}"],
                       capture_output=True, timeout=300)

    _OUT_CAP = 1024 * 1024

    def run_oracle(self, args: list[str], stdin: bytes | None = None,
                   workdir: str = "/tmp/work", timeout: int = 30,
                   env: dict | None = None) -> ExecResult:

        return self.exec([ORACLE] + args, stdin=stdin, workdir=workdir, timeout=timeout,
                         out_cap=self._OUT_CAP, argv0=CAND_EXE, env=env)

    def run_candidate(self, args: list[str], stdin: bytes | None = None,
                      workdir: str = "/tmp/work", timeout: int = 30,
                      env: dict | None = None) -> ExecResult:
        return self.exec([CAND_EXE, *args], stdin=stdin, workdir=workdir,
                         timeout=timeout, out_cap=self._OUT_CAP, env=env)
