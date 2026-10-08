from __future__ import annotations

import base64
import json
import os
import posixpath
import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path

from container import Cleanroom, CAND, CAND_EXE, ORACLE
try:
    from .comparators import compare as typed_compare
except ImportError:
    from comparators import compare as typed_compare
try:
    from .case_model import (case_identity, merge_signature, _output_kind,
                             is_trivial_surface_case, is_root_surface_case)
except ImportError:
    from case_model import (case_identity, merge_signature, _output_kind,
                            is_trivial_surface_case, is_root_surface_case)
try:
    from .case_ranker import case_modality
except ImportError:
    from case_ranker import case_modality
try:
    from .comparison_policy import classify_cli_case, compare_stream, structured_output_comparator
except ImportError:
    from comparison_policy import classify_cli_case, compare_stream, structured_output_comparator

try:
    from .metamorphic import RunObservation, MetamorphicCase, evaluate, evaluate_idempotence, idempotence_case, order_invariance_relation
except ImportError:
    from metamorphic import RunObservation, MetamorphicCase, evaluate, evaluate_idempotence, idempotence_case, order_invariance_relation
try:
    from .case_spec import BlobStore
    from .scenario_registry import build_scenario
    from .pty_executor import run_pty_in_room
except ImportError:
    from case_spec import BlobStore
    from scenario_registry import build_scenario
    from pty_executor import run_pty_in_room
try:
    from .minimizer import minimize_case_bounded
except ImportError:
    from minimizer import minimize_case_bounded


_SRC_EXTS = ("py", "go", "rs", "c", "cc", "cpp", "cxx", "h", "hpp", "hh", "js", "ts", "jsx", "tsx",
             "sh", "bash", "hs", "lhs", "rb", "pl", "pm", "lua", "zig", "nim", "ml", "mli", "java",
             "kt", "scala", "jl", "ex", "exs", "cr", "d", "f", "f90", "swift", "php", "tcl", "r",
             "clj", "erl", "vala", "cs", "fs", "el", "scm", "rkt")
_SRC_SKIP_DIRS = ("target", "vendor", "node_modules", "__pycache__", ".git", "dist", "build",
                  ".stack-work", "_build", "elm-stuff", "zig-cache", "zig-out")
_SRC_PROBE_MARK = "__PB_SRC_PROBE__"


_SRC_STAMP = "/tmp/.pb_src_stamp"


_DEEP_DELIVERY_IMPORT_SIGNAL = re.compile(r"ModuleNotFoundError|ImportError|No module named")


def _struct_fs_path(path: str, workdir: str = "/tmp/work") -> str:
    return posixpath.normpath(path if posixpath.isabs(path) else posixpath.join(workdir, path))


_LOOPBACK_RE = re.compile(r"(?:https?://)?(?:127\.0\.0\.1|localhost|0\.0\.0\.0|\[::1\]):(\d{2,5})")


def _loopback_endpoint(args) -> str | None:

    for a in args or ():
        if isinstance(a, str):
            m = _LOOPBACK_RE.search(a)
            if m:
                return f"127.0.0.1:{m.group(1)}"
    return None


def _fixture_paths(files=None, tree=None, tree_root=None) -> tuple[set, str | None]:

    have = {posixpath.normpath(p) for p in (files or {})}
    for entry in tree or ():
        if isinstance(entry, dict) and entry.get("path"):
            have.add(posixpath.normpath(str(entry["path"])))
    root = posixpath.normpath(str(tree_root)) if tree_root else None
    return have, root


def _cwd_dependent_case(args, files=None, tree=None, tree_root=None, absent=None) -> bool:

    have, root = _fixture_paths(files, tree, tree_root)

    pinned_absent = {posixpath.normpath(str(x)) for x in (absent or ())}
    abs_too = os.environ.get("PB_ANCHOR_REQUIRE_SELF_CONTAINED") == "1"
    for a in args or ():
        if not isinstance(a, str) or not a or a.startswith("-"):
            continue
        if posixpath.isabs(a) and not abs_too:
            continue

        if "/" not in a and "." not in a[1:]:
            continue
        n = posixpath.normpath(a)
        if n in have or n in pinned_absent:
            continue

        if root and (n == root or n.startswith(root + "/")):
            continue
        return True
    return False


def _struct_target_path(link_path: str, target: str, workdir: str = "/tmp/work") -> tuple[str, str]:
    link_fs = _struct_fs_path(link_path, workdir)
    target_fs = (posixpath.normpath(target) if posixpath.isabs(target)
                 else posixpath.normpath(posixpath.join(posixpath.dirname(link_fs), target)))
    return link_fs, target_fs


def _write_room_bytes(room, path: str, data: bytes) -> None:
    if hasattr(room, "write_bytes"):
        room.write_bytes(path, data)
    else:
        room.exec(["sh", "-c", f"cat > {shlex.quote(path)}"], stdin=data)


def _src_find_expr(sentinel: str) -> str:
    names = " -o ".join(f"-name '*.{e}'" for e in _SRC_EXTS)
    prunes = " ".join(f"-path '*/{d}/*' -prune -o" for d in _SRC_SKIP_DIRS)
    base = f"find {CAND} {prunes} -type f \\( {names} \\)"
    sq = shlex.quote(sentinel)

    return (f"if [ -e {sq} ]; then {base} -newer {sq} -print -quit 2>/dev/null; "
            f"else {base} -print -quit 2>/dev/null; fi")


def _b(s) -> bytes:
    if s is None:
        return b""
    if isinstance(s, bytes):
        return s
    return s.encode("utf-8", "replace")


def _case_stdin(case: dict) -> bytes:
    b64 = case.get("stdin_b64")
    if b64:
        return base64.b64decode(b64)
    return _b(case.get("stdin"))


def _port_listening(room, port) -> bool:

    if not str(port).isdigit():
        return False
    return room.exec(["python3", "-c",
                      "import socket,sys; s=socket.socket(); s.settimeout(1); "
                      f"sys.exit(0 if s.connect_ex(('127.0.0.1',{port}))==0 else 1)"]).code == 0


def _localized_fix_hint(field: str, oracle: str, candidate: str) -> str | None:
    if oracle is None or candidate is None or oracle == candidate:
        return None
    o, c = oracle, candidate

    if field in ("stdout", "stderr") and o.startswith(c) and len(o) > len(c):
        tail = o[len(c):]
        if len(tail) <= 200:
            return (f"LOCALIZED FIX: your output is a PREFIX of the oracle's — you are MISSING a "
                    f"trailing piece. Append exactly: {tail!r}")

    if field in ("stdout", "stderr") and c.startswith(o) and len(c) > len(o):
        extra = c[len(o):]
        if len(extra) <= 200:
            return (f"LOCALIZED FIX: you emit EXTRA trailing output the oracle does not. Remove: {extra!r}")

    if o.lower() == c.lower() and o != c:
        return ("LOCALIZED FIX: output differs ONLY in CASE. Match the oracle's casing exactly "
                "(or accept the input case-insensitively if this is a flag/enum value).")

    if field == "exit":
        return ("LOCALIZED FIX: exit codes differ. If the ORACLE succeeds (exit 0) where YOU error, "
                "you REJECT an input the oracle ACCEPTS — widen the accepted set. If the ORACLE "
                "errors where you succeed, add the missing validation.")

    if o.rstrip() == c.rstrip() and o != c:
        return ("LOCALIZED FIX: outputs match except TRAILING WHITESPACE/NEWLINE. Adjust the final "
                f"newline(s) — oracle ends {o[-3:]!r}, you end {c[-3:]!r}.")

    if field == "stderr":
        import os as _os
        pre = _os.path.commonprefix([o, c])
        if len(pre) >= 12 and o.startswith(pre) and c.startswith(pre):
            return (f"LOCALIZED FIX: error messages share the prefix {pre!r} then DIVERGE. Oracle "
                    f"continues {o[len(pre):][:120]!r}; you continue {c[len(pre):][:120]!r} — "
                    "match the oracle's exact wording/reason clause.")
    return None


def tool_schemas(allow: set[str] | None = None) -> list[dict]:
    schemas = [
        {"name": "bash", "description":
            "Run ONE bash command in your candidate workspace (/workspace), the offline container. "
            "This is how you READ, WRITE, and BUILD your candidate — there is no separate file tool. "
            "Write source with a here-doc (`cat > main.rs <<'EOF' ... EOF`), inspect with `cat`/`ls`/"
            "`grep`, build by running your `compile.sh`. No network. Returns stdout (stderr merged) + "
            "exit code. When you change candidate SOURCE, the candidate is rebuilt automatically and "
            "checked (delivery smoke + trunk-regression) — so always leave `./compile.sh` producing a "
            "runnable `./executable`.",
         "input_schema": {"type": "object", "properties": {
             "command": {"type": "string", "description": "the bash command to execute"}},
             "required": ["command"]}},
        {"name": "run_oracle", "description":
            "Run the reference binary (the oracle) with args and optional stdin. "
            "Returns stdout, stderr, exit code. This is your only source of ground truth. "
            "For BINARY / invalid-UTF-8 stdin (null bytes, high bytes), pass stdin_b64 (base64 of the "
            "raw bytes) instead of stdin — a str can't carry those and the oracle's lenient parsing "
            "of binary-as-malformed-HTML is a behaviour the tests check.",
         "input_schema": {"type": "object", "properties": {
             "args": {"type": "array", "items": {"type": "string"}},
             "stdin": {"type": "string", "description": "optional stdin text"},
             "stdin_b64": {"type": "string", "description": "optional base64-encoded raw stdin bytes (use for binary/non-UTF-8 input)"},
             "env": {"type": "object", "description": "optional environment variables, e.g. {\"NO_COLOR\":\"1\",\"COLUMNS\":\"40\"}"}},
             "required": ["args"]}},
        {"name": "diff", "description":
            "Run the SAME args+stdin through both oracle and your candidate and "
            "byte-compare stdout/stderr/exit. Returns match=true, or the exact "
            "divergence. On a real divergence, also auto-records it to the regression corpus. "
            "For binary/invalid-UTF-8 stdin pass stdin_b64 (base64 raw bytes) instead of stdin. "
            "Pass env={\"NO_COLOR\":\"1\",...} to run BOTH under those environment variables (probes "
            "output-mode/color forks); the env is stored with the case so the regression replay reruns it.",
         "input_schema": {"type": "object", "properties": {
             "args": {"type": "array", "items": {"type": "string"}},
             "stdin": {"type": "string"},
             "stdin_b64": {"type": "string", "description": "base64-encoded raw stdin bytes (binary/non-UTF-8 input)"},
             "env": {"type": "object", "description": "optional environment variables to set for BOTH oracle and candidate, e.g. {\"NO_COLOR\":\"1\",\"TERM\":\"dumb\",\"COLUMNS\":\"40\"}"},
             "files": {"type": "object", "description": "optional absolute-path to base64 file payload map; all files are materialized and stored for replay"}},
             "required": ["args"]}},
        {"name": "diff_batch", "description":
            "PREFERRED over many single `diff` calls, and over hand-rolling `.oracle_ref` comparisons in "
            "bash. Runs up to 60 probes through the SAME machinery as `diff` in ONE call and returns only "
            "what diverged. Use it to go DEEP on a flag you already reached: one call carrying 30-60 input "
            "variants of that flag (empty / 0 / negative / huge / malformed / non-UTF-8 / case and "
            "separator variants / stdin crossings) costs one round trip instead of 60. Every divergence is "
            "auto-recorded to the regression corpus and protected from later regressions — a comparison you "
            "run yourself in bash is NOT recorded and does not protect anything.",
         "input_schema": {"type": "object", "properties": {
             "cases": {"type": "array", "description": "up to 60 probes; each takes the same fields as `diff`",
                       "items": {"type": "object", "properties": {
                           "args": {"type": "array", "items": {"type": "string"}},
                           "stdin": {"type": "string"},
                           "stdin_b64": {"type": "string", "description": "base64-encoded raw stdin bytes"},
                           "env": {"type": "object"},
                           "files": {"type": "object"}},
                           "required": ["args"]}}},
             "required": ["cases"]}},
        {"name": "run_regression", "description":
            "Re-run every recorded differential case against the current candidate. Returns "
            "pass/fail counts AND, for the first few failing cases, the EXACT output "
            "divergence (oracle's observed output vs your candidate's output, byte for byte) "
            "so you know precisely what to change. Run after each fix; fix what it shows.",
         "input_schema": {"type": "object", "properties": {}}},
        {"name": "submit", "description":
            "Declare the candidate ready; finalizes and runs the final scored exam. This is "
            "your ONLY observation of the hidden tests (submission@1 — you get ONE counted "
            "submission): it returns the aggregate pass_rate (never the assertions). There is "
            "NO free practice exam — drive correctness entirely by differential testing against "
            "the oracle (`diff`) and the coverage sweeps, then submit when you believe you match "
            "the oracle everywhere. A readiness gate runs first; if it rejects the candidate it "
            "comes back for you to fix WITHOUT consuming your submission.",
         "input_schema": {"type": "object", "properties": {
             "rationale": {"type": "string"}}, "required": ["rationale"]}},
    ]
    import os

    if os.environ.get("PB_NATIVE_TEST_TOOLS", "0") != "1":
        schemas = [s for s in schemas
                   if s["name"] not in {"propose_env", "scenario"}]
    if allow is not None:
        schemas = [s for s in schemas if s["name"] in allow]

    if os.environ.get("PB_DIFF_BATCH", "1") != "1":
        schemas = [s for s in schemas if s["name"] != "diff_batch"]
    return schemas


@dataclass
class Divergence:
    args: list
    stdin: str
    field: str
    oracle: str
    candidate: str


_CRASH_EXITS = (101, 132, 133, 134, 135, 136, 137, 139, 141)


_CRASH_STDERR_MARKERS = (

    "panicked at", "note: run with `RUST_BACKTRACE",

    "panic:", "goroutine ", "[signal SIGSEGV", "nil pointer dereference",
    "fatal error:",

    "Segmentation fault", "Floating point exception", "Bus error", "Illegal instruction",
    "Aborted", "core dumped",
    "terminate called",
    "*** stack smashing detected",
    "*** buffer overflow detected",
    "free(): ", "malloc(): ", "double free", "corrupted",
    "munmap_chunk", "realloc():",
    "AddressSanitizer", "runtime error:",
    "Trace/breakpoint trap",

    "Killed",
)


def _is_crash(exit_code: int, stderr: str) -> bool:
    if exit_code in _CRASH_EXITS:
        return True
    s = stderr or ""
    return any(m in s for m in _CRASH_STDERR_MARKERS)


_NONDET_SETTLE_S = 1.1


def attach_exact_streams(case: dict, stdout: bytes, stderr: bytes, cap: int = 200000) -> dict:
    if os.environ.get("PB_EXACT_BINARY_STDOUT", "1") != "1":
        return case
    for name, raw in (("oracle_stdout_b64", stdout), ("oracle_stderr_b64", stderr)):
        if raw and raw.decode("utf-8", "replace").encode("utf-8", "replace") != raw:
            case[name] = base64.b64encode(raw[:cap]).decode("ascii")
    return case


_HOME_PATH_RE = re.compile(rb"/(?:home|Users)/[A-Za-z0-9._-]+|/root(?=/|\b)")


_PERTURB_ENV = {"HOME": "/tmp/pb-perturb-home", "TERM": "dumb", "USER": "pbperturb",
                "LOGNAME": "pbperturb", "COLUMNS": "132", "LINES": "48", "TZ": "UTC"}


def env_dependent_spans(baseline: bytes, perturbed: bytes) -> list[bytes]:
    if baseline == perturbed:
        return []
    b_toks, p_toks = baseline.split(), perturbed.split()
    changed = {t for t in b_toks if t not in p_toks}

    return sorted((t for t in changed if len(t) >= 4), key=len, reverse=True)


def _neutralize_home_in_expectation(case: dict) -> dict:
    if os.environ.get("PB_HOME_NEUTRAL_EXPECTATION", "1") != "1":
        return case
    context = (" ".join(str(x) for x in (case.get("args") or []))
               + " " + str(case.get("stdin") or "")).encode("utf-8", "replace")
    for key in ("oracle_stdout", "oracle_stderr"):
        text = case.get(key)
        if not text or not isinstance(text, str):
            continue
        raw = text.encode("utf-8", "replace")
        if not _HOME_PATH_RE.search(raw):
            continue
        if any(m.group(0) in context for m in _HOME_PATH_RE.finditer(raw)):
            continue
        case[key] = _HOME_PATH_RE.sub(b"<HOME>", raw).decode("utf-8", "replace")
        case["home_neutralized"] = True

    return case


def exact_streams_match(case: dict, observed: dict) -> bool:
    if case.get("comparator") not in (None, "", "exact", "contract_exact"):
        return True
    for key, raw in observed.items():
        want = case.get(key)
        if not want:
            continue
        stored = base64.b64decode(want)
        if _normalize_volatile(stored) != _normalize_volatile(raw[:len(stored)]):
            return False
    return True


def _normalize_volatile(b: bytes) -> bytes:
    s = b if isinstance(b, bytes) else str(b).encode()
    s = re.sub(rb"(thread '[^']*') \(\d+\)", rb"\1 (PID)", s)
    s = re.sub(rb"0x[0-9a-fA-F]+", b"0xADDR", s)

    s = re.sub(rb"\r[^\n]*(?=\r)", b"<PROGRESS>", s)

    if os.environ.get("PB_MASK_HOME_PATHS", "1") == "1":

        s = re.sub(rb"/(?:home|Users)/[A-Za-z0-9._-]+", b"<HOME>", s)
        s = re.sub(rb"/root(?=/|\b)", b"<HOME>", s)
        s = re.sub(rb"/tmp/tmp[A-Za-z0-9._-]{4,}", b"<TMPDIR>", s)
    return s


def _norm_str(x) -> str:

    return _normalize_volatile(x.encode("utf-8", "replace") if isinstance(x, str) else x).decode("utf-8", "replace")


_ARTIFACT_MAGIC = {
    b"\x89PNG\r\n\x1a\n": "png", b"GIF8": "gif", b"\xff\xd8\xff": "jpeg",
    b"%PDF": "pdf", b"PK\x03\x04": "zip", b"<?xml": "svg", b"<svg": "svg",
    b"RIFF": "riff", b"\x1f\x8b": "gzip", b"BZh": "bzip2", b"\xfd7zXZ": "xz",
}


def _artifact_kind(b: bytes) -> str | None:
    if not b:
        return None
    head = b[:16]
    for magic, kind in _ARTIFACT_MAGIC.items():
        if head.startswith(magic) or (kind == "svg" and magic in b[:200]):
            return kind
    return None


def _binary_artifact_equiv(o: bytes, c: bytes) -> bool:
    ko, kc = _artifact_kind(o), _artifact_kind(c)
    if ko is None or ko != kc:
        return False
    if len(o) < 32 or len(c) < 32:
        return False
    if ko == "riff":
        return typed_compare("wav", o, c, sample_tolerance=1).equal
    if ko == "svg":
        return typed_compare("html", o, c).equal

    return o == c


def _artifact_b64_equiv(oracle_b64: str | None, candidate_b64: str | None) -> bool:
    import base64 as _base64
    if oracle_b64 == candidate_b64:
        return True
    try:
        return _binary_artifact_equiv(_base64.b64decode(oracle_b64 or ""),
                                      _base64.b64decode(candidate_b64 or ""))
    except Exception:
        return False


def _oracle_is_nondeterministic(room, args, stdin, prev=None, settle_s: float = _NONDET_SETTLE_S,
                                env: dict | None = None) -> bool:
    try:
        o1 = room.run_oracle(args, stdin, env=env)
        o2 = room.run_oracle(args, stdin, env=env)
        if (_normalize_volatile(o1.stdout) != _normalize_volatile(o2.stdout)
                or _normalize_volatile(o1.stderr) != _normalize_volatile(o2.stderr)
                or o1.code != o2.code):
            return True
        time.sleep(settle_s)
        o3 = room.run_oracle(args, stdin, env=env)
    except Exception:
        return False
    return (_normalize_volatile(o3.stdout) != _normalize_volatile(o1.stdout)
            or _normalize_volatile(o3.stderr) != _normalize_volatile(o1.stderr)
            or o3.code != o1.code)


def _is_help_probe(case) -> bool:

    return is_trivial_surface_case(case if isinstance(case, dict) else {"args": case or []})


def _balanced_pick(pool: dict, limit: int) -> list:
    out: list = []
    queues = [_flatten_bucket(v) for v in pool.values()]
    queues = [q for q in queues if q]
    while queues and len(out) < limit:
        for q in queues:
            if len(out) >= limit:
                break
            out.append(q.pop(0))
        queues = [q for q in queues if q]
    return out


def _flatten_bucket(bucket) -> list:

    if isinstance(bucket, dict):
        return _balanced_pick(bucket, sum(len(v) for v in bucket.values()))
    return list(bucket)


class ToolBox:
    def __init__(self, room: Cleanroom, exam_fn, max_submissions: int = 1,
                 corpus_path: Path | None = None,
                 reg_parallel: int = 8, reg_timeout: int = 10):
        self.room = room

        self.reg_parallel = max(1, reg_parallel)
        self.reg_timeout = max(1, reg_timeout)
        self.exam_fn = exam_fn
        self.max_submissions = max_submissions
        self.submissions: list[dict] = []
        self.corpus: list[dict] = []
        import os as _os
        self.surface_case_cap = max(4, int(_os.environ.get("PB_SURFACE_CASE_CAP", "16")))
        self.corpus_path = corpus_path

        self.flaky: list[dict] = []
        self._flaky_keys: set = set()
        self.flaky_path = (corpus_path.with_name("flaky.json") if corpus_path else None)
        self.divergences: list[Divergence] = []

        self.env_proposals: list[dict] = []
        self._env_tag: dict | None = None
        self.last_exam: dict | None = None
        self.writes = 0
        self.solved = False
        self.done = False

        self._reg_cache: dict | None = None
        self._reg_dirty = True

        self.subcommands: list[str] = []

        self.regression_guard_feedback = True
        self._anchor_pass_prev: set | None = None

        self.component_telemetry: dict[str, dict[str, int | float]] = {}

    def telemetry_add(self, component: str, **counts) -> None:
        row = self.component_telemetry.setdefault(component, {
            "calls": 0, "planned": 0, "attempted": 0, "applicable": 0,
            "produced": 0, "accepted_into_corpus": 0, "fixed": 0,
            "still_failing": 0, "wall_seconds": 0.0})
        for key, value in counts.items():
            row[key] = row.get(key, 0) + value

    def call(self, name: str, args: dict) -> dict:
        fn = getattr(self, "_t_" + name, None)
        if fn is None:
            return {"error": f"unknown tool {name}"}
        component = name if name in {"scenario", "metamorphic"} else None
        started = time.time()
        if component:
            self.telemetry_add(component, calls=1, attempted=1)
        try:
            return fn(args)
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}
        finally:
            if component:
                self.telemetry_add(component, wall_seconds=time.time() - started)

    _SEE_CAP = 8000
    _CORPUS_CAP = 32768
    _FLAKY_FLUSH_EVERY = 25

    def _file_args(self, args) -> list:

        out = []
        for a in args:
            if not isinstance(a, str) or not (a.startswith("/") or a.endswith((".csv", ".tsv", ".txt"))):
                continue
            if a.startswith("/workspace"):
                continue

            if a.startswith("/") and not (a.startswith(self._WORKDIR)
                                             or a.startswith("/tmp")
                                             or a.startswith("/fixtures")):
                continue
            if self.room.exec(["sh", "-c", f"test -f {shlex.quote(a)}"]).code == 0:
                out.append(a)
        return out

    _WORKDIR = "/tmp/work"

    def _declared_output_paths(self, args=()) -> list[str]:

        out, i = [], 0
        while i < len(args):
            a = args[i] if isinstance(args[i], str) else ""
            value = ""
            if a in ("-o", "--output", "--out", "--output-file") and i + 1 < len(args):
                value = str(args[i + 1]); i += 1
            elif a.startswith(("--output=", "--out=", "--output-file=", "-o=")):
                value = a.split("=", 1)[1]
            elif a.startswith("-o") and len(a) > 2:
                value = a[2:]
            i += 1
            if not value.startswith("/") or value.startswith(("/dev/", "/proc/", "/sys/")):
                continue
            if value.startswith("/workspace"):
                continue
            if not (value.startswith(self._WORKDIR) or value.startswith("/tmp")
                    or value.startswith("/fixtures")):
                continue
            out.append(value)
        return out

    def _artifact_scan_dirs(self, args=()) -> list[str]:

        dirs = {self._WORKDIR}
        for a in args:
            if not isinstance(a, str) or not a.startswith("/") or a.startswith("/workspace"):
                continue
            if not (a.startswith(self._WORKDIR) or a.startswith("/tmp") or a.startswith("/fixtures")):
                continue
            parent = a.rsplit("/", 1)[0] or "/"
            if parent in ("/", "/tmp", "/fixtures"):
                continue
            dirs.add(parent)

        for value in self._declared_output_paths(args):
            if value in ("/", "/tmp", "/fixtures"):
                continue
            if self.room.exec(["sh", "-c", f"test -d {shlex.quote(value)}"]).code == 0:
                dirs.add(value)

        if os.environ.get("PB_SCAN_HOME_STATE") == "1":
            r = self.room.exec(["sh", "-c",
                                'H="$(printf %s "${HOME:-/home/agent}")"; '
                                'for d in "$H" "${XDG_DATA_HOME:-$H/.local/share}" '
                                '"${XDG_CONFIG_HOME:-$H/.config}" "${XDG_CACHE_HOME:-$H/.cache}"; do '
                                '[ -d "$d" ] && printf "%s\\n" "$d"; done'])
            for line in r.stdout.decode("utf-8", "replace").splitlines():
                if line.strip() and line.strip() != "/":
                    dirs.add(line.strip())
        return sorted(dirs)

    def _workdir_listing(self, scan_dirs=None, extra_paths=()) -> set:

        dirs = " ".join(shlex.quote(d) for d in (scan_dirs or [self._WORKDIR]))
        found = set()
        r = self.room.exec(["sh", "-c", f"find {dirs} -maxdepth 4 -type f 2>/dev/null"])
        found |= {ln for ln in r.stdout.decode("utf-8", "replace").splitlines() if ln.strip()}
        for p in extra_paths:
            if p not in found and self.room.exec(
                    ["sh", "-c", f"test -f {shlex.quote(p)}"]).code == 0:
                found.add(p)
        return found

    def _new_files_since(self, baseline: set, scan_dirs=None, extra_paths=()) -> list:
        cap = int(os.environ.get("PB_ARTIFACT_FILE_CAP", "200"))
        return sorted(self._workdir_listing(scan_dirs, extra_paths) - baseline)[:cap]

    def _read_files_b64(self, paths) -> dict:
        snap = {}
        for p in paths:
            r = self.room.exec(["sh", "-c", f"test -f {shlex.quote(p)} && base64 {shlex.quote(p)}"])
            snap[p] = (r.stdout or b"").decode("ascii", "replace").strip() if r.code == 0 else None
        return snap

    def _env_dependent_spans(self, args, stdin, observed, env) -> list[bytes]:

        if os.environ.get("PB_ENV_PERTURB_PROBE", "0") != "1":
            return []
        try:
            alt = self.room.run_oracle(args, stdin, env={**(env or {}), **_PERTURB_ENV})
        except Exception:
            return []
        if alt.code != observed.code:
            return []
        spans = (env_dependent_spans(observed.stdout, alt.stdout)
                 + env_dependent_spans(observed.stderr, alt.stderr))
        return spans[:8]

    def _file_struct(self, paths) -> dict:

        snap = {}
        for p in paths:
            fs_path = _struct_fs_path(p, self._WORKDIR)
            r = self.room.exec(["sh", "-c",
                f"p={shlex.quote(fs_path)}; "
                f"if test -L \"$p\"; then printf 'L|%s|' \"$(readlink \"$p\")\"; "
                f"else printf 'F||'; fi; "
                f"base64 \"$(readlink -f \"$p\" 2>/dev/null || echo \"$p\")\" 2>/dev/null | tr -d '\\n'"])
            snap[p] = r.stdout.decode("ascii", "replace").strip() if r.code == 0 else None
        return snap

    def _restore_struct(self, struct: dict):
        import base64 as _b64
        for p, s in struct.items():
            if not s or "|" not in s:
                continue
            kind, target, b64 = s.split("|", 2)
            try:
                data = _b64.b64decode(b64, validate=True)
            except Exception:
                data = _b64.b64decode(b64)
            if kind == "L" and target:
                link_fs, target_fs = _struct_target_path(p, target, self._WORKDIR)
                self.room.exec(["mkdir", "-p", posixpath.dirname(target_fs)])
                self.room.exec(["mkdir", "-p", posixpath.dirname(link_fs)])
                _write_room_bytes(self.room, target_fs, data)
                self.room.exec(["ln", "-sf", target, link_fs])
            else:
                fs_path = _struct_fs_path(p, self._WORKDIR)
                self.room.exec(["rm", "-f", fs_path])
                self.room.exec(["mkdir", "-p", posixpath.dirname(fs_path)])
                _write_room_bytes(self.room, fs_path, data)

    def _struct_paths(self, args) -> list:
        return [a for a in args if isinstance(a, str)
                and (a.startswith("/") or a.endswith((".txt", ".csv", ".tsv")))]

    def _file_struct_str(self, args) -> str:

        parts = []
        for a in self._struct_paths(args):
            fs_path = _struct_fs_path(a, self._WORKDIR)
            r = self.room.exec(["sh", "-c",
                f"p={shlex.quote(fs_path)}; test -e \"$p\" -o -L \"$p\" || exit 0; "
                f"if test -L \"$p\"; then printf 'L|%s|' \"$(readlink \"$p\")\"; else printf 'F||'; fi; "
                f"base64 \"$(readlink -f \"$p\" 2>/dev/null || echo \"$p\")\" 2>/dev/null | tr -d '\\n'"])
            val = r.stdout.decode("ascii", "replace").strip() if r.code == 0 else ""
            if val:
                parts.append(f"{a}={val}")
        return ";".join(parts)[:4000]

    def _restore_struct_str(self, fingerprint: str):
        for part in fingerprint.split(";"):
            if "=" not in part:
                continue
            path, val = part.split("=", 1)
            if "|" not in val:
                continue
            kind, target, b64 = val.split("|", 2)

            try:
                data = base64.b64decode(b64 + "=" * (-len(b64) % 4), validate=False)
            except Exception:
                continue
            if kind == "L" and target:
                link_fs, target_fs = _struct_target_path(path, target, self._WORKDIR)
                self.room.exec(["mkdir", "-p", posixpath.dirname(target_fs)])
                self.room.exec(["mkdir", "-p", posixpath.dirname(link_fs)])
                _write_room_bytes(self.room, target_fs, data)
                self.room.exec(["ln", "-sf", target, link_fs])
            else:
                fs_path = _struct_fs_path(path, self._WORKDIR)
                self.room.exec(["rm", "-f", fs_path])
                self.room.exec(["mkdir", "-p", posixpath.dirname(fs_path)])
                _write_room_bytes(self.room, fs_path, data)

    def _snapshot_files(self, args) -> dict:

        snap = {}
        for a in args:
            if not isinstance(a, str) or not (a.startswith("/") or a.endswith((".csv", ".tsv", ".txt"))):
                continue
            fs = shlex.quote(_struct_fs_path(a, self._WORKDIR))
            r = self.room.exec(["sh", "-c", f"test -f {fs} && base64 {fs}"])
            if r.code == 0 and r.stdout:
                b64 = r.stdout.decode("ascii", "replace").strip()
                if len(b64) <= 400000:
                    snap[a] = b64
        return snap

    def _absent_path_args(self, args) -> list:

        out = []
        for a in args or ():
            if not isinstance(a, str) or not a or a.startswith("-"):
                continue
            if "/" not in a and "." not in a[1:]:
                continue
            if a.startswith("/workspace"):
                continue
            fs = _struct_fs_path(a, self._WORKDIR)
            if not (fs.startswith(self._WORKDIR) or fs.startswith("/tmp") or fs.startswith("/fixtures")):
                continue
            if self.room.exec(["sh", "-c", f"test -e {shlex.quote(fs)}"]).code != 0:
                out.append(a)
        return out

    def _restore_absent(self, case: dict) -> None:
        for a in case.get("absent") or ():
            fs = _struct_fs_path(str(a), self._WORKDIR)
            if not (fs.startswith(self._WORKDIR) or fs.startswith("/tmp") or fs.startswith("/fixtures")):
                continue
            if fs.rstrip("/") in ("/tmp", self._WORKDIR.rstrip("/"), "/fixtures"):
                continue
            self.room.exec(["rm", "-rf", "--", fs])

    def _restore_files(self, case: dict):

        import base64 as _b64
        for path, b64 in (case.get("files") or {}).items():
            try:
                data = _b64.b64decode(b64, validate=True)
            except Exception:
                data = _b64.b64decode(b64)
            fs_path = _struct_fs_path(path, self._WORKDIR)
            self.room.exec(["mkdir", "-p", str(Path(fs_path).parent)])
            _write_room_bytes(self.room, fs_path, data)

    def _restore_tree(self, case: dict) -> None:
        entries = case.get("tree") or []
        root = str(case.get("tree_root") or "")
        prefix = "/tmp/pb-scenarios/"
        if (not hasattr(self.room, "exec")
                or not root.startswith(prefix) or root == prefix.rstrip("/")):
            return
        root_path = Path(root)
        if ".." in root_path.parts:
            return
        normalized = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            path = str(entry.get("path") or "")
            if not (path == root or path.startswith(root.rstrip("/") + "/")):
                return
            normalized.append((path, entry))
        self.room.exec(["rm", "-rf", "--", root])
        self.room.exec(["mkdir", "-p", root])
        order = {"dir": 0, "file": 1, "symlink": 2}
        for path, entry in sorted(normalized,
                                  key=lambda x: (x[0].count("/"), order.get(x[1].get("kind"), 9))):
            kind = entry.get("kind")
            self.room.exec(["mkdir", "-p", str(Path(path).parent)])
            if kind == "dir":
                self.room.exec(["mkdir", "-p", path])
            elif kind == "file":
                encoded = entry.get("data_b64") or (case.get("files") or {}).get(path) or ""
                try:
                    data = base64.b64decode(encoded, validate=True)
                except Exception:
                    data = base64.b64decode(encoded)
                _write_room_bytes(self.room, path, data)
            elif kind == "symlink":
                self.room.exec(["rm", "-f", "--", path])
                self.room.exec(["ln", "-s", str(entry.get("target") or ""), path])
            mode = entry.get("mode")
            if mode is not None and kind != "symlink":
                self.room.exec(["chmod", format(int(mode), "o"), path])

    def _t_run_oracle(self, a):
        r = self.room.run_oracle(a["args"], _case_stdin(a), env=a.get("env") or None)
        so = r.stdout.decode("utf-8", "replace"); se = r.stderr.decode("utf-8", "replace")
        out = {"stdout": so[:self._SEE_CAP], "stderr": se[:self._SEE_CAP], "exit": r.code}
        if len(so) > self._SEE_CAP or len(se) > self._SEE_CAP:
            out["truncated"] = {"stdout_len": len(so), "stderr_len": len(se)}
        return out

    def _t_diff(self, a):

        force = bool(a.get("_force_record"))
        record = force or not a.get("_no_record")
        args = a["args"]; stdin = _case_stdin(a)
        env = a.get("env") or None
        explicit_files = a.get("files") or {}
        explicit_tree = a.get("tree") or []
        if explicit_tree:
            self._restore_tree(a)
        if explicit_files:
            self._restore_files({"files": explicit_files})

        fpaths = self._file_args(args)
        absent_pre = self._absent_path_args(args)
        pre = self._read_files_b64(fpaths) if fpaths else {}
        pre_struct = self._file_struct(fpaths) if fpaths else {}

        wide = os.environ.get("PB_ARTIFACT_WIDE_SCAN", "1") == "1"
        scan_dirs = self._artifact_scan_dirs(args) if wide else [self._WORKDIR]
        declared = self._declared_output_paths(args) if wide else []
        wd_baseline = self._workdir_listing(scan_dirs, declared)
        _t0 = time.time()
        o = self.room.run_oracle(args, stdin, env=env)
        oracle_secs = time.time() - _t0
        o_files = self._read_files_b64(fpaths) if fpaths else {}
        o_struct = self._file_struct(fpaths) if fpaths else {}
        o_new = self._new_files_since(wd_baseline, scan_dirs, declared)
        o_newfiles = self._read_files_b64(o_new) if o_new else {}
        for p in o_new:
            self.room.exec(["sh", "-c", f"rm -f {shlex.quote(p)}"])
        if explicit_tree:
            self._restore_tree(a)
        elif explicit_files:

            self._restore_files({"files": explicit_files})
        if fpaths:
            self._restore_struct(pre_struct)
        c = self.room.run_candidate(args, stdin, env=env)
        c_files = self._read_files_b64(fpaths) if fpaths else {}
        c_struct = self._file_struct(fpaths) if fpaths else {}
        c_newfiles = self._read_files_b64(o_new) if o_new else {}

        for p in self._new_files_since(wd_baseline, scan_dirs, declared):
            self.room.exec(["sh", "-c", f"rm -f {shlex.quote(p)}"])
        out = {"match": True, "args": args}
        diffs = []
        if o.stdout != c.stdout:
            diffs.append(("stdout", o.stdout, c.stdout))
        if o.stderr != c.stderr:
            diffs.append(("stderr", o.stderr, c.stderr))
        if o.code != c.code:
            diffs.append(("exit", str(o.code).encode(), str(c.code).encode()))
        for p in fpaths:
            if o_files.get(p) != c_files.get(p):
                import base64 as _b64
                def _dec(x): return _b64.b64decode(x) if x else b""
                ob, cb = _dec(o_files.get(p)), _dec(c_files.get(p))
                if _binary_artifact_equiv(ob, cb):
                    continue
                diffs.append((f"file:{p}", ob, cb))
        for p in fpaths:
            if o_struct.get(p) != c_struct.get(p):
                diffs.append((f"struct:{p}", (o_struct.get(p) or "").encode(), (c_struct.get(p) or "").encode()))
        for p in o_new:
            if o_newfiles.get(p) != c_newfiles.get(p):
                import base64 as _b64
                def _dec(x): return _b64.b64decode(x) if x else b""
                ob, cb = _dec(o_newfiles.get(p)), _dec(c_newfiles.get(p))
                if _binary_artifact_equiv(ob, cb):
                    continue
                diffs.append((f"newfile:{p}", ob, cb))
        for p in o_new:
            self.room.exec(["sh", "-c", f"rm -f {shlex.quote(p)}"])

        oracle_crash = _is_crash(o.code, o.stderr.decode("utf-8", "replace"))
        nondet = _oracle_is_nondeterministic(self.room, args, stdin, o, env=env) if diffs else False
        env_spans = self._env_dependent_spans(args, stdin, o, env) if diffs else []

        if fpaths:
            self._restore_files({"files": {p: b64 for p, b64 in pre.items() if b64 is not None}})
        case = {"args": args, "stdin": (a.get("stdin", "") or "")[:self._CORPUS_CAP],
                "files": {**explicit_files, **{p: pre[p] for p in pre if pre[p] is not None}},
                "tree": explicit_tree or None, "tree_root": a.get("tree_root"),
                "absent": absent_pre or None,
                "oracle_stdout": o.stdout.decode("utf-8", "replace")[:self._CORPUS_CAP],
                "oracle_stderr": o.stderr.decode("utf-8", "replace")[:self._CORPUS_CAP],
                "oracle_exit": o.code,
                "oracle_files": {p: o_files[p] for p in o_files if o_files[p] is not None} or None,
                "oracle_secs": round(oracle_secs, 3)}
        if explicit_tree:
            self._restore_tree(a)
        import os as _os
        case.update(classify_cli_case(args, o.stdout, o.stderr, o.code,
                                      tiered=_os.environ.get("PB_TIERED_COMPARATORS", "0") == "1"))
        if self._env_tag:
            case.update(self._env_tag)
        if env:
            case["env"] = env
        if o_newfiles:
            case["oracle_newfiles"] = {p: o_newfiles[p] for p in o_newfiles if o_newfiles[p] is not None} or None

        if stdin and stdin.decode("utf-8", "replace").encode("utf-8", "replace") != stdin:
            case["stdin_b64"] = base64.b64encode(stdin).decode("ascii")

        attach_exact_streams(case, o.stdout, o.stderr, self._CORPUS_CAP)
        if env_spans:

            for key in ("oracle_stdout", "oracle_stderr"):
                text = case.get(key)
                if not text:
                    continue
                raw = text.encode("utf-8", "replace")
                masked = raw
                for span in env_spans:
                    masked = masked.replace(span, b"<ENV>")
                if masked != raw:
                    case[key] = masked.decode("utf-8", "replace")
                    case["env_masked"] = True
        if any(o_struct.get(p) != c_struct.get(p) for p in fpaths):
            case["struct_pre"] = ";".join(f"{p}={pre_struct[p]}" for p in pre_struct if pre_struct.get(p))[:200000]
            case["oracle_struct"] = ";".join(f"{p}={o_struct[p]}" for p in o_struct if o_struct.get(p))[:4000]
        if oracle_crash and not nondet:

            case.update({"crash_matchable": True, "comparator": "crash_skeleton",
                         "severity": "diagnostic", "required": False,
                         "exact_bonus": False})
        if diffs:
            out["match"] = False
            f, ov, cv = diffs[0]
            out["field"] = f
            out["oracle"] = ov.decode("utf-8", "replace")[:4000]
            out["candidate"] = cv.decode("utf-8", "replace")[:4000]
            if nondet:
                self.record_flaky(case, reason="oracle non-deterministic (run-twice probe)")
                out["nondeterministic_not_recorded"] = (
                    "the ORACLE produced DIFFERENT output on a second run with the same input — "
                    "this output is non-deterministic (e.g. embedded wallclock timestamp / PID / "
                    "address). NOT recorded as a corpus defect. NOTE: you can often still MATCH "
                    "it by reproducing the same FORMAT (e.g. print your own wallclock in the same "
                    "layout) — the hidden test typically tolerates the varying token. Reproduce "
                    "the deterministic skeleton; don't give up on it.")
            elif record:
                self._record(case)
                self.divergences.append(Divergence(args, a.get("stdin", ""), f,
                                                   out["oracle"], out["candidate"]))
                if oracle_crash:
                    out["crash_is_matchable"] = (
                        "the oracle CRASHED with a stable semantic skeleton. Recorded as "
                        "LOW-PRIORITY diagnostic evidence. Match the panic/deadlock class and "
                        "symbolic function/source skeleton; addresses, absolute paths, line "
                        "numbers and runtime offsets are normalized and do not gate release.")
        else:
            if record:

                if force and _oracle_is_nondeterministic(self.room, args, stdin, o, env=env):
                    self.record_flaky(case, reason="oracle non-deterministic (anchor probe)")
                    out["anchor_skipped_flaky"] = True
                elif _cwd_dependent_case(args, case.get("files"), case.get("tree"),
                                         case.get("tree_root"), case.get("absent")):

                    self._record(case)
                    out["anchor_skipped_cwd_dependent"] = True
                else:
                    case["anchor"] = True
                    self._record(case)
        return out

    _DIFF_BATCH_CAP = 60

    def _t_diff_batch(self, a):

        cases = a.get("cases")
        if not isinstance(cases, list) or not cases:
            return {"error": "diff_batch needs a non-empty `cases` array of {args, stdin?, env?, files?} objects"}
        if len(cases) > self._DIFF_BATCH_CAP:
            return {"error": f"diff_batch takes at most {self._DIFF_BATCH_CAP} cases ({len(cases)} given); "
                             f"split it — a larger reply would be truncated and waste the probes"}
        shared = {k: a[k] for k in ("_no_record", "_force_record") if k in a}
        results, diverged, matched, errors = [], 0, 0, 0
        for i, c in enumerate(cases):
            if not isinstance(c, dict) or not c.get("args"):
                results.append({"i": i, "error": "each case needs `args`"})
                errors += 1
                continue

            try:
                r = self._t_diff({**shared, **c})
            except Exception as e:
                results.append({"i": i, "args": c.get("args"), "error": f"{type(e).__name__}: {e}"})
                errors += 1
                continue
            if r.get("match"):
                matched += 1
                results.append({"i": i, "args": c.get("args"), "match": True})
            else:
                diverged += 1
                results.append({"i": i, **r})
        return {"n": len(cases), "diverged": diverged, "matched": matched, "errors": errors,
                "results": results}

    def _t_propose_env(self, a):

        setup = (a.get("setup") or "").strip()
        endpoint = (a.get("endpoint") or "").strip()
        if not setup or not endpoint:
            return {"error": "propose_env needs `setup` (a repeatable start command) and `endpoint`"}
        args = a.get("args") or []
        stdin = _case_stdin(a)
        prep = [c for c in (a.get("prep") or []) if isinstance(c, str)]
        for cmd in prep + [setup]:
            self.room.exec(["sh", "-c", cmd], timeout=60)
        o = self.room.run_oracle(args, stdin)
        c = self.room.run_candidate(args, stdin)
        if o.stdout == c.stdout and o.stderr == c.stderr and o.code == c.code:
            return {"recorded": False,
                    "note": "oracle and candidate MATCH under this env — nothing to propose. If you "
                            "expected a difference, check your setup/endpoint or the args."}
        self.env_proposals.append({
            "kind": a.get("kind", "server"), "setup": setup, "endpoint": endpoint, "prep": prep,
            "evidence": {"args": args, "stdin": (a.get("stdin", "") or "")[:8000]}})
        return {"recorded": True, "endpoint": endpoint,
                "note": "env-proposal recorded. The orchestrator will stand this env up in the main "
                        "room, re-confirm the divergence, and bank it — you need not keep it alive."}

    def _delivery_smoke(self, deep: bool = False) -> dict:
        if not deep:
            return self._delivery_smoke_base()
        return self._delivery_smoke_deep()

    def _delivery_smoke_base(self) -> dict:

        r = self.room.sh(
            "rm -rf /tmp/_pb_smoke /tmp/_pb_smoke_tmp && mkdir -p /tmp/_pb_smoke /tmp/_pb_smoke_tmp && "
            "cd /workspace && tar cf - --exclude=./executable --exclude=./.oracle_ref "
            "--exclude=./.git --exclude=./target --exclude=./vendor --exclude=./node_modules "
            "--exclude=./__pycache__ . | tar xf - -C /tmp/_pb_smoke && "
            "cd /tmp/_pb_smoke && rm -f executable && chmod +x compile.sh && "
            "./compile.sh >/tmp/_pb_build_err 2>&1; "
            "if [ ! -e executable ]; then echo SMOKE_NOEXE; tail -c 900 /tmp/_pb_build_err; "
            "elif [ -L executable ]; then echo SMOKE_SYMLINK; "
            "elif [ ! -x executable ]; then echo SMOKE_NOEXE; "
            "else TMPDIR=/tmp/_pb_smoke_tmp HOME=/tmp/_pb_smoke_tmp "
            "     timeout 10 ./executable --help </dev/null >/dev/null 2>/tmp/_pb_smoke_err; "
            "  rc=$?; if [ $rc -eq 124 ]; then echo SMOKE_HANG; "
            "  elif grep -qE 'Traceback \\(most recent call last\\)|ModuleNotFoundError|ImportError|"
            "FileNotFoundError|error while loading shared libraries' /tmp/_pb_smoke_err 2>/dev/null; "
            "  then echo SMOKE_STARTUP_CRASH; head -c 500 /tmp/_pb_smoke_err; "
            "  else echo SMOKE_OK; fi; fi",
            timeout=60)
        tok = (r.stdout or b"").decode("utf-8", "replace")

        first = next((t for line in tok.splitlines()
                      for t in ("SMOKE_STARTUP_CRASH", "SMOKE_OK", "SMOKE_SYMLINK", "SMOKE_HANG",
                                "SMOKE_NOEXE")
                      if line.strip().startswith(t)), "")
        if first == "SMOKE_STARTUP_CRASH":
            return {"ok": False, "kind": "startup_crash", "message": (
                "./executable CRASHED AT STARTUP when run from a fresh copy with a clean TMPDIR/HOME. "
                "The candidate depends on a file that exists only because YOU created it in this "
                "sandbox (e.g. something under /tmp). The grader runs in a fresh container where that "
                "file does not exist, so every test would fail. EMBED the data in your source instead "
                "of reading it at import time. Startup error:\n"
                + tok.split("SMOKE_STARTUP_CRASH", 1)[-1].strip()[:500])}
        if first == "SMOKE_OK":
            return {"ok": True, "kind": "ok", "message": ""}
        if first == "SMOKE_SYMLINK":
            return {"ok": False, "kind": "symlink", "message": (
                "compile.sh made ./executable a SYMLINK. The exam HASHES the executable and a symlink "
                "fails (hash_executable_failed -> score 0), even though it runs here. Make compile.sh "
                "produce a REAL file — e.g. write a wrapper script with `cat > executable <<'W' ... W` "
                "then `chmod +x executable`, NOT `ln -s`.")}
        if first == "SMOKE_HANG":
            return {"ok": False, "kind": "hang", "message": (
                "./executable HANGS on `--help` with empty stdin — it blocks reading stdin when none "
                "is given. The exam runs many no-stdin cases and will TIME OUT (score 0). Read stdin "
                "ONLY when the args call for it (e.g. a `-` file arg or no file given); never block on "
                "stdin for --help/--version or when a file/positional is provided.")}
        return {"ok": False, "kind": "noexe", "message": (
            "compile.sh did NOT produce a runnable ./executable from a FRESH copy (the exam builds this "
            "way and will score 0). Use paths relative to compile.sh's own location (no absolute "
            "/workspace/...); leave an executable file named ./executable next to compile.sh."

            + ("\nBUILD OUTPUT (last lines):\n" + tok.split("SMOKE_NOEXE", 1)[-1].strip()[:900]
               if tok.split("SMOKE_NOEXE", 1)[-1].strip() else ""))}

    def _delivery_smoke_deep(self) -> dict:

        base = self._delivery_smoke_base()
        if not base["ok"]:
            return base
        tar_cmd = getattr(self, "_submission_tar_cmd", None)
        if not tar_cmd:
            return base

        subs = list(dict.fromkeys([s for s in (self.subcommands or []) if s and not s.startswith("-")]
                                  + self._corpus_regions()))[:20]
        import shlex

        probe = "; ".join(
            f"timeout 15 ./executable {shlex.quote(s)} --help </dev/null 2>&1 | head -c 4000" for s in ([""] + subs))
        script = (
            "rm -rf /tmp/_pb_deep && mkdir -p /tmp/_pb_deep && "
            f"( {tar_cmd} ) | tar xf - -C /tmp/_pb_deep 2>/dev/null && "
            "cd /tmp/_pb_deep && rm -f executable && chmod +x compile.sh 2>/dev/null; "
            "./compile.sh >/dev/null 2>&1; "
            "if [ ! -x executable ] && [ ! -e executable ]; then echo PB_DEEP_NOEXE; exit 0; fi; "
            f"OUT=$( {probe} ); echo \"$OUT\"; echo PB_DEEP_END")
        r = self.room.sh(script, timeout=600)
        tok = (r.stdout or b"").decode("utf-8", "replace")
        if "PB_DEEP_NOEXE" in tok:
            return {"ok": False, "kind": "noexe", "message": (
                "The REAL submission tar (what the exam extracts) does NOT build a runnable ./executable, "
                "even though the live /workspace does. Almost always a CAPTURE/packaging bug: a shipped "
                "non-utf8 binary or __pycache__/*.pyc dropped a source file from the tar. Remove stray "
                "binaries/build artefacts from your candidate dir so only source ships.")}
        m = _DEEP_DELIVERY_IMPORT_SIGNAL.search(tok)
        if m:
            return {"ok": False, "kind": "import", "message": (
                f"A subcommand crashed on an import/module error in the DELIVERED tar ({m.group(0)}). The "
                "entrypoint likely imports a package/module that did NOT ship (dropped by the tar — a "
                "stray binary or __pycache__ artefact corrupts packaging; or the module simply isn't in "
                "your candidate dir). Ensure every module your entrypoint imports is a committed source "
                "file under the candidate dir. First failing probe output:\n" + tok[-600:])}
        return {"ok": True, "kind": "ok", "message": ""}

    def _corpus_regions(self) -> list[str]:

        seen = []
        for c in (self.corpus or []):
            args = c.get("args") or []
            for tok in args:
                if isinstance(tok, str) and tok and not tok.startswith("-"):

                    if tok not in seen and re.fullmatch(r"[a-z][a-z0-9_-]{0,29}", tok):
                        seen.append(tok)
                    break
        return seen[:15]

    def _t_write_file(self, a):

        dst = a["path"] if a["path"].startswith("/") else CAND + "/" + a["path"]

        new = _b(a["content"])
        if len(new) < 50 and not a.get("force"):
            old = self.room.read_file(dst)
            if old and len(old) > 200:
                return {"write_refused": True, "path": a["path"],
                        "existing_bytes": len(old), "new_bytes": len(new),
                        "note": (f"Refusing to overwrite {a['path']} ({len(old)} bytes) with "
                                 f"only {len(new)} bytes — this would destroy your candidate. "
                                 "To EDIT, write the FULL new file contents (not an empty or "
                                 "partial body). If you truly want to reset it, pass force=true.")}
        self.room.write_file(dst, new)
        out = {"written": a["path"]}
        out.update(self._after_edit())
        return out

    def _after_edit(self) -> dict:

        self._reg_dirty = True
        self.writes += 1
        if not getattr(self, "_wrote_real_candidate", False) and self._candidate_src_bytes() >= 200:
            self._wrote_real_candidate = True
        build = self.room.sh("cd /workspace && sh compile.sh 2>&1 || true")

        self.room.sh(f"touch {_SRC_STAMP}")
        out = {"build": build.stdout.decode("utf-8", "replace")[-1500:]}

        smoke = self._delivery_smoke()
        if not smoke["ok"]:
            out["EXAM_DELIVERY_WARNING"] = smoke["message"]

        if getattr(self, "regression_guard_feedback", True) and any(c.get("anchor") for c in self.corpus):
            passing, _failing = self.anchor_pass_set()
            prev = getattr(self, "_anchor_pass_prev", None)
            if prev is not None:
                broke = prev - passing
                if broke:
                    shown = "; ".join(f"args={list(k[0])}" + (f" stdin={k[1]!r}" if k[1] else "")
                                      for k in list(broke)[:5])
                    out["TRUNK_REGRESSION_WARNING"] = (
                        f"This edit BROKE {len(broke)} documented/confirmed feature(s) that PASSED "
                        f"before — fix this before moving on (a trunk regression costs far more than "
                        f"any edge case): {shown}. Re-check with run_regression; if the change wasn't "
                        "meant to touch these, revert that part.")
            self._anchor_pass_prev = passing
        return out

    def _t_read_file(self, a):
        p = a["path"]
        if not p.startswith("/"):
            p = CAND + "/" + p
        data = self.room.read_file(p)
        return {"path": p, "content": data.decode("utf-8", "replace")[:8000]}

    def _t_shell(self, a):
        r = self.room.sh(a["cmd"], timeout=120)
        return {"stdout": r.stdout.decode("utf-8", "replace")[:6000],
                "stderr": r.stderr.decode("utf-8", "replace")[:2000], "exit": r.code}

    def _candidate_src_bytes(self) -> int:

        names = " -o ".join(f"-name '*.{e}'" for e in _SRC_EXTS)
        prunes = " ".join(f"-path '*/{d}/*' -prune -o" for d in _SRC_SKIP_DIRS)
        r = self.room.sh(
            f"find {CAND} {prunes} -type f \\( {names} \\) -print0 2>/dev/null | "
            "xargs -0 cat 2>/dev/null | wc -c")
        try:
            return int((r.stdout or b"0").decode().strip() or "0")
        except ValueError:
            return 0

    def _t_bash(self, a):

        command = a["command"]

        probe = f"\nprintf '\\n{_SRC_PROBE_MARK}'; {_src_find_expr(_SRC_STAMP)}"
        r = self.room.bash_login(command + probe, cwd=CAND, timeout=120)
        full = r.stdout.decode("utf-8", "replace")

        body, _, tail = full.rpartition(_SRC_PROBE_MARK)
        if not body and tail == full:
            body, tail = full, ""
        out = {"stdout": body[:6000], "exit": r.code}
        if tail.strip():
            out.update(self._after_edit())
        return out

    def _t_bash_probe(self, a):

        r = self.room.bash_login(a["command"], cwd=CAND, timeout=120)
        return {"stdout": r.stdout.decode("utf-8", "replace")[:6000], "exit": r.code}

    def _serve_and_probe(self, args: list[str], requests: list[dict], *, oracle: bool,
                         env: dict, root: str, timeout: float) -> dict:
        exe = ORACLE if oracle else CAND_EXE

        pick = self.room.exec(["python3", "-c",
                               "import socket;s=socket.socket();s.bind(('127.0.0.1',0));"
                               "print(s.getsockname()[1]);s.close()"], timeout=20)
        port = (pick.stdout or b"").decode().strip()
        if not port.isdigit():
            return {"error": "could_not_allocate_port"}
        pid_file = f"{root.rstrip('/')}/.pb_serve.pid"
        argv = [self._scenario_expand(x, {"root": root, "primary": root, "url": ""}).replace(
            "{port}", port) for x in args]
        quoted = " ".join(shlex.quote(x) for x in argv)
        envs = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in (env or {}).items())
        try:
            self.room.exec(["sh", "-c",
                            f"cd {shlex.quote(root)} 2>/dev/null || cd /tmp/work; "
                            f"{envs} setsid {shlex.quote(exe)} {quoted} "
                            f">/tmp/.pb_serve.out 2>&1 & echo $! > {shlex.quote(pid_file)}"],
                           timeout=30)
            ready = f"import socket,sys;s=socket.create_connection(('127.0.0.1',{port}),0.3);s.close()"
            for _ in range(60):
                if self.room.exec(["python3", "-c", ready], timeout=10).code == 0:
                    break
                time.sleep(0.1)
            else:
                return {"error": "server_never_listened"}
            probe = self.room.exec(["python3", "-c", self._SERVE_PROBE, port,
                                    json.dumps(requests)], timeout=max(10.0, timeout))
            return {"responses": (probe.stdout or b"").decode("utf-8", "replace")[:self._CORPUS_CAP],
                    "probe_exit": probe.code}
        finally:
            self.room.exec(["sh", "-c",
                            f"p={shlex.quote(pid_file)}; if test -s \"$p\"; then "
                            "pid=$(cat \"$p\"); kill -- -\"$pid\" 2>/dev/null || kill \"$pid\" "
                            "2>/dev/null || true; i=0; "
                            "while kill -0 \"$pid\" 2>/dev/null && test $i -lt 30; do "
                            "sleep 0.05; i=$((i+1)); done; "
                            "kill -9 -- -\"$pid\" 2>/dev/null || kill -9 \"$pid\" 2>/dev/null || "
                            "true; fi; rm -f \"$p\""], timeout=30)

    def _t_run_regression(self, a):

        self._reg_dirty = True

        reg = self.count_regression_failures(
            limit=max(1, int(os.environ.get("PB_FAILURE_LIMIT", "6"))), detail=3)
        total = len(self.corpus)
        failed = reg["failed"]
        out = {"total": total, "passed": total - failed, "failed": failed,
               "first_failures": reg["examples"]}
        out.update({"required_total": reg.get("required_scanned", total),
                    "required_failed": reg.get("required_failed", failed),
                    "diagnostic_total": reg.get("diagnostic_scanned", 0),
                    "diagnostic_failed": reg.get("diagnostic_failed", 0),
                    "required_completion_rate": reg.get("required_completion_rate", 1.0)})

        rp = reg.get("region_pass") or {}
        if len(rp) > 1:
            rows = sorted(((r, p, t) for r, (p, t) in rp.items()), key=lambda x: (x[1] / x[2], -x[2]))
            weak = [(r, p, t) for r, p, t in rows if p < t]
            if weak:
                lines = [f"  • {r}: {p}/{t} ({100*p//t}%)" for r, p, t in weak[:8]]
                out["weakest_regions"] = (
                    "CORPUS PASS RATE BY SUBCOMMAND/REGION (weakest first — the candidate loses the "
                    "most by leaving a whole region mis-implemented; fix the lowest-% region's cases "
                    "before polishing regions already at 100%):\n" + "\n".join(lines))
        if reg.get("clusters") and failed > 0:

            cl = reg["clusters"]
            lines = [f"  • {c['count']} cases share: {c['signature']}   e.g. args={c['example']['args']}"
                     f" stdin={c['example']['stdin']!r}" for c in cl[:6]]
            out["failure_clusters"] = (
                "FAILING CASES GROUPED BY ROOT CAUSE (fix the LARGEST cluster first — one fix clears "
                "all N cases in it; don't grind case-by-case):\n" + "\n".join(lines))
        if reg["diffs"]:
            shown = []
            for d in reg["diffs"]:
                block = (f"  • args={d['args']} stdin={d['stdin']!r} field={d['field']}\n"
                         f"      ORACLE   : {d['oracle']!r}\n"
                         f"      CANDIDATE: {d['candidate']!r}")
                if d.get("fix_hint"):
                    block += f"\n      → {d['fix_hint']}"
                shown.append(block)
            out["exact_divergences"] = (
                "EXACT output divergences to fix (the oracle's OBSERVED output vs YOUR "
                "candidate's output — find where your code produces the CANDIDATE string and "
                "change it to match the ORACLE byte-for-byte, then edit the source with `bash` + re-run):\n"
                + "\n".join(shown))
        return out

    @staticmethod
    def _reg_sig(c: dict) -> tuple:
        import re
        flags = tuple(sorted({str(a).split("=", 1)[0] for a in (c.get("args") or [])
                              if str(a).startswith("-")}))
        ecls = " ".join(re.findall(r"[A-Za-z]+", c.get("oracle_stderr") or "")[:5]).lower()
        return (flags, c.get("oracle_exit"), ecls)

    def _diverse_scan_sample(self, max_scan: int) -> list:
        corpus = self.corpus
        if len(corpus) <= max_scan:
            return corpus
        keep = set()

        for i, c in enumerate(corpus):
            if c.get("anchor"):
                keep.add(i)

        from collections import OrderedDict
        groups: "OrderedDict[tuple, list]" = OrderedDict()
        for i in range(len(corpus) - 1, -1, -1):
            if i in keep:
                continue
            groups.setdefault(self._reg_sig(corpus[i]), []).append(i)

        for sig, idxs in groups.items():
            if len(keep) >= max_scan:
                break
            keep.add(idxs[0])

        rr = [list(idxs[1:]) for idxs in groups.values()]
        while len(keep) < max_scan and any(rr):
            for bucket in rr:
                if not bucket:
                    continue
                keep.add(bucket.pop(0))
                if len(keep) >= max_scan:
                    break
        return [corpus[i] for i in sorted(keep)]

    def count_regression_failures(self, limit: int = 6, max_scan: int = 400,
                                  detail: int = 2) -> dict:
        if (not self._reg_dirty and self._reg_cache is not None
                and self._reg_cache["_limit"] >= limit and self._reg_cache["_detail"] >= detail):
            return self._reg_cache
        res = self._compute_regression_failures(limit=max(limit, 6), max_scan=max_scan,
                                                detail=max(detail, 2))
        res["_limit"], res["_detail"] = max(limit, 6), max(detail, 2)
        self._reg_cache = res
        self._reg_dirty = False
        return res

    def _region(self, case: dict) -> str:
        subs = set(self.subcommands or [])
        if subs:
            for a in (case.get("args") or []):
                if str(a) in subs:
                    return str(a)
        return "(top)"

    def _obligation_key(self, case: dict) -> str:
        args = [str(a) for a in (case.get("args") or [])]
        flag = next((a for a in args if a.startswith("-")), "(positional)")
        return f"{self._region(case)}:{flag}"

    def _tally_region(self, region_pass: dict, case: dict, passed: bool) -> None:
        slot = region_pass.setdefault(self._region(case), [0, 0])
        slot[0] += int(passed)
        slot[1] += 1

    def _compute_regression_failures(self, limit: int = 6, max_scan: int = 400,
                                     detail: int = 2) -> dict:
        from concurrent.futures import ThreadPoolExecutor
        scan = self._diverse_scan_sample(max_scan) if len(self.corpus) > max_scan else self.corpus

        meta_cases = [c for c in scan if c.get("metamorphic")]
        live = [c for c in scan if not c.get("metamorphic")
                and (c.get("crash_matchable")
                or not _is_crash(c.get("oracle_exit", 0), c.get("oracle_stderr", "")))]

        for c in live:
            if not c.get("env_endpoint"):
                ep = _loopback_endpoint(c.get("args"))
                if ep:
                    c["needs_env"], c["env_endpoint"] = True, ep
        needs_env = [c for c in live if c.get("needs_env") and c.get("env_endpoint")]
        if needs_env:
            down = set()
            for ep in {c["env_endpoint"] for c in needs_env}:
                if not _port_listening(self.room, ep.rsplit(":", 1)[-1]):
                    down.add(ep)
            if down:
                live = [c for c in live if not (c.get("needs_env") and c.get("env_endpoint") in down)]

        def run_one(case):
            to = max(self.reg_timeout, int(case.get("oracle_secs", 0) * 3) + 1)
            if case.get("executor") == "command_sequence":
                if case.get("tree"):
                    self._restore_tree(case)
                opts = dict(case.get("executor_options") or {})
                sequence_case = {**case,
                                 "setup_kind": case.get("setup_kind") or opts.get("setup_kind"),
                                 "tree_root": case.get("tree_root") or opts.get("root")}
                blob = self._run_scenario_sequence(
                    sequence_case, list(opts.get("steps") or []), oracle=False)[:self._CORPUS_CAP]
                class R: pass
                r = R(); r.stdout = blob; r.stderr = b""; r.code = 0
                try:
                    observation = json.loads(blob.decode("utf-8", "replace"))
                    r.timed_out = any(bool(step.get("timed_out"))
                                      for step in observation.get("steps", []))
                except Exception:
                    r.timed_out = False
                return case, r, None, None, None
            if case.get("executor") == "pty_screen":
                self._restore_absent(case)
                if case.get("tree"): self._restore_tree(case)
                elif case.get("files"): self._restore_files(case)
                opts = dict(case.get("executor_options") or {})
                masks = opts.pop("volatile_masks", [])
                obs = run_pty_in_room(self.room, [CAND_EXE] + case["args"], executable=CAND_EXE, **opts)

                class R: pass
                r=R(); r.stdout=obs.output; r.stderr=b""; r.code=obs.returncode
                return case, r, None, None, None
            self._restore_absent(case)
            if case.get("tree"):
                self._restore_tree(case)
            elif case.get("struct_pre"):
                self._restore_struct_str(case["struct_pre"])
            elif case.get("files"):
                self._restore_files(case)
            r = self.room.run_candidate(case["args"], _case_stdin(case), timeout=to, env=case.get("env"))

            cfiles = self._read_files_b64(list(case["oracle_files"].keys())) if case.get("oracle_files") else None

            cnew = None
            if case.get("oracle_newfiles"):
                cnew = self._read_files_b64(list(case["oracle_newfiles"].keys()))
                for p in case["oracle_newfiles"]:
                    self.room.exec(["sh", "-c", f"rm -f {shlex.quote(p)}"])

            cstruct = self._file_struct_str(case["args"]) if case.get("oracle_struct") else None
            return case, r, cfiles, cstruct, cnew

        with_files = [c for c in live if c.get("files") or c.get("tree") or c.get("struct_pre")]
        no_files = [c for c in live if not (c.get("files") or c.get("tree") or c.get("struct_pre"))]
        results = []
        for case in with_files:
            results.append(run_one(case))
        if no_files:
            with ThreadPoolExecutor(max_workers=self.reg_parallel) as ex:
                results.extend(ex.map(run_one, no_files))

        required_scanned = sum(c.get("required", True) is not False for c in meta_cases + live)
        diagnostic_scanned = len(meta_cases) + len(live) - required_scanned
        failed = 0; required_failed = 0; diagnostic_failed = 0
        examples = []; diffs = []; timeouts = 0

        balance = os.environ.get("PB_FAILURE_BALANCE", "")
        if balance not in ("", "0", "1", "cause", "obligation", "hybrid"):
            raise ValueError(
                f"PB_FAILURE_BALANCE={balance!r} invalid; use 1|cause|obligation|hybrid")
        balance_by = ("cause" if balance in ("1", "cause")
                      else balance if balance in ("obligation", "hybrid") else "")
        defer_help = os.environ.get("PB_DETAIL_DEFER_HELP") == "1"

        help_first = os.environ.get("PB_DETAIL_HELP_FIRST") == "1"
        if help_first:
            defer_help = False
        pool: dict = {}
        help_diffs: list = []
        other_diffs: list = []
        required_timeouts = 0; diagnostic_timeouts = 0
        required_evaluated = 0; diagnostic_evaluated = 0
        case_outcomes = []

        def record_outcome(case: dict, passed: bool | None, reason: str = "") -> None:
            nonlocal required_evaluated, diagnostic_evaluated
            required = case.get("required", True) is not False
            if passed is not None:
                if required:
                    required_evaluated += 1
                else:
                    diagnostic_evaluated += 1
            case_outcomes.append({
                "identity": repr(case_identity(case)),
                "evaluated": passed is not None,
                "passed": passed,
                "required": required,
                "reason": reason,
            })

        for case in meta_cases:
            m = case["metamorphic"]; relation = m["relation"]
            out = self._t_metamorphic({"relation": relation, "base": m["base"],
                                       "followups": m.get("followups", []), "_no_record": True})
            if out.get("candidate_holds") is False:
                record_outcome(case, False, "metamorphic_relation_failed")
                failed += 1
                required = case.get("required", True) is not False
                required_failed += int(required); diagnostic_failed += int(not required)
                if len(examples) < limit: examples.append({"args": case.get("args", []), "stdin": case.get("stdin", ""), "relation": relation, "required": required})
                if len(diffs) < detail: diffs.append({"args": case.get("args", []), "stdin": case.get("stdin", ""), "field": "metamorphic", "oracle": "relation holds", "candidate": "relation violated", "fix_hint": relation, "required": required})
            elif out.get("candidate_holds") is True:
                record_outcome(case, True, "metamorphic_relation_holds")
            else:
                record_outcome(case, None, "metamorphic_not_evaluated")
        clusters: dict = {}
        region_pass: dict = {}
        required_region_pass: dict = {}
        diagnostic_region_pass: dict = {}
        def _cluster_sig(field, cso, cse, ccode, oexit):

            txt = (cse or "").strip()
            lines = txt.splitlines() if txt else []
            line = ""
            for ln in lines:
                if "panicked at" in ln and len(lines) > lines.index(ln) + 1:
                    line = lines[lines.index(ln) + 1].strip()
                    break
            if not line and lines:
                line = lines[0].strip()
            line = re.sub(r":\d+(:\d+)?", ":N", line)
            line = re.sub(r"0x[0-9a-fA-F]+", "0xADDR", line)
            if line:
                return f"stderr~{line[:90]}"
            if ccode != oexit:
                return f"exit {ccode} (want {oexit})"
            return f"{field} divergence"
        for case, c, cfiles, cstruct, cnew in results:
            required = case.get("required", True) is not False
            if bool(getattr(c, "timed_out", False)) or c.code == 124:
                timeouts += 1
                required_timeouts += int(required)
                diagnostic_timeouts += int(not required)

            oracle_crashed = _is_crash(case.get("oracle_exit", 0), case.get("oracle_stderr", "") or "")
            if (not case.get("crash_matchable") and oracle_crashed
                    and _is_crash(c.code, c.stderr.decode("utf-8", "replace"))):
                record_outcome(case, None, "unstable_crash_skipped")
                continue
            cso = c.stdout.decode("utf-8", "replace")[:self._CORPUS_CAP]
            cse = c.stderr.decode("utf-8", "replace")[:self._CORPUS_CAP]

            def _eq(a, b, field):
                na, nb = _normalize_volatile(a), _normalize_volatile(b)
                if case.get("comparator") == "semantic_cli" and field in ("stdout", "stderr"):
                    return compare_stream(case, field, b, a).equal
                if case.get("comparator") == "crash_skeleton" and field in ("stdout", "stderr"):
                    return typed_compare("crash_skeleton", b.encode("utf-8", "replace"),
                                         a.encode("utf-8", "replace")).equal
                if case.get("comparator") == "terminal_screen" and field == "stdout":
                    opts = case.get("executor_options") or {}
                    return typed_compare("terminal_screen", b.encode("utf-8", "replace"),
                                         a.encode("utf-8", "replace"), rows=opts.get("rows",24),
                                         cols=opts.get("cols",80),
                                         volatile_masks=opts.get("volatile_masks",())).equal
                if case.get("comparator") == "http_sequence" and field == "stdout":
                    opts = case.get("executor_options") or {}
                    return typed_compare("http_sequence", b.encode("utf-8", "replace"),
                                         a.encode("utf-8", "replace"),
                                         steps=opts.get("steps", ())).equal
                if (case.get("comparator") in ("json", "json_multiset", "xml", "xml_multiset",
                                               "text_record_multiset") and field == "stdout"):
                    return typed_compare(case["comparator"], b.encode("utf-8", "replace"),
                                         a.encode("utf-8", "replace")).equal
                return na == nb

            files_match = (not case.get("oracle_files")
                           or all(_artifact_b64_equiv(case["oracle_files"][p], (cfiles or {}).get(p))
                                  for p in case["oracle_files"]))

            struct_match = (not case.get("oracle_struct") or cstruct == case["oracle_struct"])

            newfiles_match = (not case.get("oracle_newfiles")
                              or all(_artifact_b64_equiv(case["oracle_newfiles"][p], (cnew or {}).get(p))
                                     for p in case["oracle_newfiles"]))

            def _exact_stream_ok(key, raw):
                return exact_streams_match(case, {key: raw})
            bytes_match = exact_streams_match(
                case, {"oracle_stdout_b64": c.stdout, "oracle_stderr_b64": c.stderr})
            if (_eq(cso, case["oracle_stdout"], "stdout")
                    and _eq(cse, case["oracle_stderr"], "stderr")
                    and c.code == case["oracle_exit"] and files_match
                    and struct_match and newfiles_match and bytes_match):
                self._tally_region(region_pass, case, passed=True)
                self._tally_region(required_region_pass if required else diagnostic_region_pass,
                                   case, passed=True)
                record_outcome(case, True, "matched")
                continue

            if failed < detail and case.get("executor") not in {"pty_screen", "command_sequence"}:
                if _oracle_is_nondeterministic(self.room, case["args"], _case_stdin(case), env=case.get("env")):
                    self.record_flaky(case, reason="oracle non-deterministic (regression re-check)")
                    record_outcome(case, None, "oracle_nondeterministic")
                    continue

                fresh = self.room.run_oracle(case["args"], _case_stdin(case), env=case.get("env"))
                fso = fresh.stdout.decode("utf-8", "replace")[:self._CORPUS_CAP]
                fse = fresh.stderr.decode("utf-8", "replace")[:self._CORPUS_CAP]

                fresh_b64: dict = {}
                attach_exact_streams(fresh_b64, fresh.stdout, fresh.stderr, self._CORPUS_CAP)

                fresh_case = _neutralize_home_in_expectation(
                    {"args": case.get("args"), "stdin": case.get("stdin"),
                     "oracle_stdout": fso, "oracle_stderr": fse, **fresh_b64})
                fso, fse = fresh_case["oracle_stdout"], fresh_case["oracle_stderr"]
                fresh_b64 = {k: fresh_case[k] for k in ("oracle_stdout_b64", "oracle_stderr_b64")
                             if fresh_case.get(k)}

                b64_stale = any(
                    case.get(k) and not exact_streams_match({"comparator": "exact", k: case[k]},
                                                            {k: base64.b64decode(fresh_b64[k])})
                    for k in ("oracle_stdout_b64", "oracle_stderr_b64") if fresh_b64.get(k))
                b64_appeared = any(bool(fresh_b64.get(k)) != bool(case.get(k))
                                   for k in ("oracle_stdout_b64", "oracle_stderr_b64"))
                if ((fso, fse, fresh.code) != (case["oracle_stdout"], case["oracle_stderr"], case["oracle_exit"])
                        or b64_stale or b64_appeared):
                    case["oracle_stdout"], case["oracle_stderr"], case["oracle_exit"] = fso, fse, fresh.code
                    for k in ("oracle_stdout_b64", "oracle_stderr_b64"):
                        case.pop(k, None)
                    case.update(fresh_b64)
                    if self.corpus_path:
                        self.corpus_path.write_text(json.dumps(self.corpus))

                if _eq(cso, case["oracle_stdout"], "stdout") and _eq(cse, case["oracle_stderr"], "stderr") and c.code == case["oracle_exit"] and files_match and struct_match and newfiles_match                        and _exact_stream_ok("oracle_stdout_b64", c.stdout) and _exact_stream_ok("oracle_stderr_b64", c.stderr):
                    self._tally_region(region_pass, case, passed=True)
                    self._tally_region(required_region_pass if required else diagnostic_region_pass,
                                       case, passed=True)
                    record_outcome(case, True, "matched_refreshed_oracle")
                    continue
            failed += 1
            required_failed += int(required)
            diagnostic_failed += int(not required)
            self._tally_region(region_pass, case, passed=False)
            self._tally_region(required_region_pass if required else diagnostic_region_pass,
                               case, passed=False)
            record_outcome(case, False, "candidate_divergence")

            _fld = ("stdout" if cso != case["oracle_stdout"] else
                    "stderr" if cse != case["oracle_stderr"] else
                    "exit" if c.code != case["oracle_exit"] else
                    "file" if not files_match else
                    "newfile" if not newfiles_match else "struct")
            _sig = _cluster_sig(_fld, cso, cse, c.code, case["oracle_exit"])
            if required:
                cl = clusters.setdefault(_sig, {"count": 0, "example": None})
                cl["count"] += 1
                if cl["example"] is None:
                    cl["example"] = {"args": case["args"], "stdin": (case.get("stdin", "") or "")[:60]}
            if len(examples) < limit:
                examples.append({"args": case["args"], "stdin": (case.get("stdin", "") or "")[:60],
                                 "required": required})

            if balance_by:
                row = {"args": case["args"], "stdin": (case.get("stdin", "") or "")[:60],
                       "required": required}
                if balance_by == "cause":
                    pool.setdefault(_sig, []).append(row)
                elif balance_by == "obligation":
                    pool.setdefault(self._obligation_key(case), []).append(row)
                else:
                    pool.setdefault(_sig, {}).setdefault(
                        self._obligation_key(case), []).append(row)

            if help_first:
                _slot = (len(diffs) < detail if _is_help_probe(case)
                         else len(other_diffs) < detail)
            elif defer_help:
                _slot = (len(diffs) < detail if not _is_help_probe(case)
                         else len(help_diffs) < detail)
            else:
                _slot = len(diffs) < detail
            if _slot:
                if cso != case["oracle_stdout"]:
                    f, ov, cv = "stdout", case["oracle_stdout"], cso
                elif cse != case["oracle_stderr"]:
                    f, ov, cv = "stderr", case["oracle_stderr"], cse
                elif c.code != case["oracle_exit"]:
                    f, ov, cv = "exit", str(case["oracle_exit"]), str(c.code)
                elif not files_match:

                    import base64 as _b64
                    p = next((p for p in (case.get("oracle_files") or {})
                              if (cfiles or {}).get(p) != case["oracle_files"][p]), None)
                    def _d(x): return _b64.b64decode(x).decode("utf-8", "replace") if x else ""
                    f = f"file:{p}"
                    ov = _d(case["oracle_files"][p]) if p else ""
                    cv = _d((cfiles or {}).get(p)) if p else ""
                elif not newfiles_match:

                    import base64 as _b64
                    p = next((p for p in (case.get("oracle_newfiles") or {})
                              if (cnew or {}).get(p) != case["oracle_newfiles"][p]), None)
                    def _d(x): return _b64.b64decode(x).decode("utf-8", "replace") if x else ""
                    f = f"newfile:{p}"
                    ov = _d(case["oracle_newfiles"][p]) if p else ""
                    cv = _d((cnew or {}).get(p)) if p else ""
                else:

                    f = "struct (symlink preservation)"
                    ov = case.get("oracle_struct", "")
                    cv = cstruct or ""
                diffs.append({"args": case["args"], "stdin": (case.get("stdin", "") or "")[:80],
                              "field": f, "oracle": ov[:1500], "candidate": cv[:1500],
                              "fix_hint": _localized_fix_hint(f, ov, cv),
                              "required": required})
                if defer_help and _is_help_probe(case):
                    help_diffs.append(diffs.pop())
                elif help_first and not _is_help_probe(case):
                    other_diffs.append(diffs.pop())
        if defer_help and len(diffs) < detail:
            diffs.extend(help_diffs[:detail - len(diffs)])
        if help_first and len(diffs) < detail:
            diffs.extend(other_diffs[:detail - len(diffs)])
        if balance_by and pool:
            examples = _balanced_pick(pool, limit)
        top_clusters = sorted(clusters.items(), key=lambda kv: -kv[1]["count"])
        return {"failed": failed, "timeouts": timeouts, "examples": examples,
                "scanned": len(scan), "diffs": diffs,
                "required_failed": required_failed,
                "required_scanned": required_scanned,
                "required_timeouts": required_timeouts,
                "required_evaluated": required_evaluated,
                "required_completion_rate": (required_evaluated / required_scanned
                                             if required_scanned else 1.0),
                "diagnostic_failed": diagnostic_failed,
                "diagnostic_scanned": diagnostic_scanned,
                "diagnostic_timeouts": diagnostic_timeouts,
                "diagnostic_evaluated": diagnostic_evaluated,
                "case_outcomes": case_outcomes,
                "region_pass": region_pass,
                "required_region_pass": required_region_pass,
                "diagnostic_region_pass": diagnostic_region_pass,
                "clusters": [{"signature": s, "count": v["count"], "example": v["example"]}
                             for s, v in top_clusters if v["count"] >= 2]}

    def anchor_pass_count(self) -> tuple[int, int]:

        passing, failing = self.anchor_pass_set()
        return len(passing), len(passing) + len(failing)

    def demote_anchors(self, keys) -> int:

        wanted, n = set(keys), 0
        for case in self.corpus:
            if case.get("anchor") and repr(case_identity(case)) in wanted:
                case.pop("anchor", None)
                case["anchor_demoted"] = "blocked_checkpoints_repeatedly"
                n += 1
        if n:
            self._reg_dirty = True
            if self.corpus_path:
                self.corpus_path.write_text(json.dumps(self.corpus))
        return n

    def anchor_pass_set(self, *, anchors_only: bool = True) -> tuple[set, list[dict]]:
        passing: set = set()
        failing: list[dict] = []
        for case in (c for c in self.corpus if c.get("anchor") or not anchors_only):
            key = case_identity(case)
            if case.get("executor") == "command_sequence":
                try:
                    opts = dict(case.get("executor_options") or {})
                    sequence_case = {**case,
                                     "setup_kind": case.get("setup_kind") or opts.get("setup_kind"),
                                     "tree_root": case.get("tree_root") or opts.get("root")}
                    blob = self._run_scenario_sequence(
                        sequence_case, list(opts.get("steps") or []), oracle=False)
                    oracle_blob = str(case.get("oracle_stdout", "")).encode("utf-8", "replace")
                    if case.get("comparator") == "http_sequence":
                        equal = typed_compare("http_sequence", oracle_blob, blob,
                                              steps=opts.get("steps", ())).equal
                    else:
                        equal = (_norm_str(blob.decode("utf-8", "replace"))
                                 == _norm_str(case.get("oracle_stdout", "")))
                    if equal:
                        passing.add(key)
                    else:
                        failing.append(case)
                except Exception:
                    failing.append(case)
                continue
            to = max(self.reg_timeout, int(case.get("oracle_secs", 0) * 3) + 1)
            self._restore_absent(case)
            if case.get("tree"):
                self._restore_tree(case)
            elif case.get("files"):
                self._restore_files(case)
            r = self.room.run_candidate(case["args"], _case_stdin(case), timeout=to, env=case.get("env"))
            cfiles = self._read_files_b64(list(case["oracle_files"].keys())) if case.get("oracle_files") else None
            cnew = None
            if case.get("oracle_newfiles"):
                cnew = self._read_files_b64(list(case["oracle_newfiles"].keys()))
                for p in case["oracle_newfiles"]:
                    self.room.exec(["sh", "-c", f"rm -f {shlex.quote(p)}"])
            cstruct = self._file_struct_str(case["args"]) if case.get("oracle_struct") else None
            so = r.stdout.decode("utf-8", "replace") if isinstance(r.stdout, bytes) else (r.stdout or "")
            se = r.stderr.decode("utf-8", "replace") if isinstance(r.stderr, bytes) else (r.stderr or "")
            files_match = (not case.get("oracle_files")
                           or all(_artifact_b64_equiv(case["oracle_files"][p], (cfiles or {}).get(p)) for p in case["oracle_files"]))
            newfiles_match = (not case.get("oracle_newfiles")
                              or all(_artifact_b64_equiv(case["oracle_newfiles"][p], (cnew or {}).get(p)) for p in case["oracle_newfiles"]))
            struct_match = (not case.get("oracle_struct") or cstruct == case["oracle_struct"])

            bytes_match = exact_streams_match(
                case, {"oracle_stdout_b64": _b(r.stdout), "oracle_stderr_b64": _b(r.stderr)})
            if bytes_match and _norm_str(so) == _norm_str(case["oracle_stdout"]) and _norm_str(se) == _norm_str(case["oracle_stderr"])                    and r.code == case["oracle_exit"] and files_match and newfiles_match and struct_match:
                passing.add(key)
            else:
                failing.append(case)
        return passing, failing

    def _t_submit(self, a):
        if len(self.submissions) >= self.max_submissions:
            return {"error": f"already submitted — your {self.max_submissions} counted "
                    f"submission(s) are spent; the run is finalized",
                    "done": True, "best_pass_rate": self._best_rate()}

        built = False
        for _attempt in range(3):
            self.room.sh("cd /workspace && sh compile.sh >/dev/null 2>&1 || true", timeout=300)
            if self.room.exec(["test", "-x", "/workspace/executable"]).code == 0:
                built = True
                break
            time.sleep(1.0)
        if not built:

            ls = self.room.exec(["sh", "-c",
                                 "find /workspace -name '*.py' -o -name '*.c' -o -name '*.cpp' "
                                 "-o -name '*.cc' -o -name '*.go' -o -name '*.rs' -o -name '*.js' "
                                 "2>/dev/null | head -1"])
            if not (ls.stdout or b"").strip():
                return {"submit_blocked": True, "reason": "candidate does not build (no source in /workspace)",
                        "note": "/workspace has no source file and produces no executable. Write your "
                                "implementation and a working compile.sh, then submit. Your submission was NOT consumed."}

        if self.corpus:
            reg = self.count_regression_failures(limit=3, detail=3)
            required_total = int(reg.get("required_scanned", len(self.corpus)) or 0)
            required_failed = int(reg.get("required_failed", reg["failed"]) or 0)
            if required_total and required_failed == required_total:
                return {"submit_blocked": True, "reason": "candidate fails its ENTIRE local corpus",
                        "failed": required_failed, "of": required_total,
                        "note": "Your candidate fails every recorded corpus case — it is almost "
                                "certainly broken/empty or mis-built, and would score ~0 on the "
                                "hidden suite. Fix it before submitting. Your submission was NOT consumed.",
                        "diffs": reg.get("diffs", [])[:3]}

        if getattr(self, "regression_guard_feedback", True):

            smoke = self._delivery_smoke(deep=True)
            if smoke["kind"] in ("symlink", "hang", "import"):
                return {"submit_blocked": True, "reason": "candidate is undeliverable to the exam",
                        "note": smoke["message"] + " Your submission was NOT consumed — fix and submit."}
            if smoke["kind"] == "noexe":
                self.last_delivery_warning = smoke["message"]
        score = self.exam_fn()
        self.last_exam = score
        n = len(self.submissions) + 1
        rate = score.get("pass_rate")
        rec = {"submission": n, "passed": score.get("passed"),
               "total": score.get("total"), "pass_rate": rate,
               "solved": bool(score.get("solved")), "rationale": a.get("rationale", "")}
        self.submissions.append(rec)
        if score.get("solved"):
            self.solved = True
            self.done = True
        elif len(self.submissions) >= self.max_submissions:
            self.done = True
        return {"submission": n, "of_max": self.max_submissions,
                "passed": rec["passed"], "total": rec["total"], "pass_rate": rate,
                "solved": rec["solved"], "remaining_submissions": self.max_submissions - n,
                "done": self.done,
                "note": ("All hidden tests passed — task solved." if rec["solved"]

                         else "Not 100%. You may reverse-engineer further FROM THE SCORE "
                              "(never the tests) and submit again." if not self.done
                         else "This was your single counted submission — finalized.")}
