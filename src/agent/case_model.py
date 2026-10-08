from __future__ import annotations

import hashlib
import base64
import json
import os
import re
from pathlib import PurePosixPath
from typing import Any


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, bytes):
        return {"__bytes_sha256__": hashlib.sha256(value).hexdigest()}
    return value


def stable_digest(value: Any) -> str:
    raw = json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8", "surrogatepass")
    return hashlib.sha256(raw).hexdigest()


def case_identity(case: dict) -> tuple:
    return (
        tuple(str(a) for a in (case.get("args") or case.get("argv") or [])),
        stable_digest({"stdin": case.get("stdin", ""), "stdin_b64": case.get("stdin_b64", "")}),
        str(case.get("cwd") or "/tmp/work"),
        stable_digest(case.get("env") or {}),
        stable_digest({"files": case.get("files") or {}, "tree": case.get("tree") or []}),
        str(case.get("struct_pre") or ""),
        stable_digest(case.get("services") or []),
        stable_digest(case.get("metamorphic") or {}),
        str(case.get("executor") or "oneshot"),
        stable_digest(case.get("executor_options") or {}),
    )


_HELP_FLAGS = {"-h", "--help", "-?", "--usage"}
_VERSION_FLAGS = {"-V", "--version"}


_VERSION_AMBIGUOUS = {"-v"}


_SURFACE_SUBCOMMANDS = {"help", "version"}


def error_signature(case: dict, region: str = "(top)") -> tuple:
    args = [str(a) for a in (case.get("args") or [])]
    flags = tuple(sorted({a.split("=", 1)[0] for a in args if a.startswith("-")}))
    err = case.get("oracle_stderr") or ""

    skeleton = re.sub(r"0x[0-9a-fA-F]+", "0xADDR", err)

    for i, arg in enumerate(args):
        if i and not arg.startswith("-") and args[i - 1].startswith("-") and arg:
            skeleton = skeleton.replace(arg, "VALUE")
    skeleton = re.sub(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?", "N", skeleton)
    skeleton = re.sub(r"/(?:[^\s:'\"]+/)*[^\s:'\"]+", "/PATH", skeleton)
    words = " ".join(re.findall(r"[A-Za-z_]+", skeleton)[:10]).lower()
    return ("error", region, flags, case.get("oracle_exit"), words)


def _stdin_shape(case: dict) -> tuple:
    if case.get("stdin_b64"):
        return ("binary", len(case.get("stdin_b64") or ""))
    text = case.get("stdin") or ""
    if not text:
        return ("empty", 0)
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return ("json-object", tuple(sorted(map(str, obj.keys()))))
        if isinstance(obj, list):
            return ("json-array", len(obj))
        return ("json-scalar", type(obj).__name__)
    except Exception:
        lines = text.count("\n") + 1
        return ("text", min(lines, 8), min(len(text), 4096))


def _positional_shape(args: list[str]) -> tuple:
    vals = []
    skip_value = False
    for a in args:
        if skip_value:
            skip_value = False
            continue
        if a.startswith("-"):
            if "=" not in a:
                skip_value = True
            continue
        suffix = PurePosixPath(a).suffix.lower() if ("/" in a or "." in a) else ""
        vals.append(("path" if "/" in a or suffix else "value", suffix))
    return tuple(vals[:6])


def _output_kind(case: dict) -> str:
    if case.get("oracle_newfiles"):
        return "artifact-tree"
    if case.get("oracle_files"):
        return "inplace-file"
    if case.get("oracle_struct"):
        return "filesystem-structure"
    out = case.get("oracle_stdout") or ""
    stripped = out.lstrip()
    if stripped.startswith(("{", "[")):
        return "structured-text"
    if out:
        return "text"
    return "exit-only"


def success_signature(case: dict, region: str = "(top)") -> tuple:
    args = [str(a) for a in (case.get("args") or [])]
    flags = tuple(sorted({a.split("=", 1)[0] for a in args if a.startswith("-")}))
    fixture = _fixture_shape(case)
    return ("success", region, flags, _positional_shape(args), _stdin_shape(case),
            fixture, _output_kind(case))


def merge_signature(case: dict, region: str = "(top)") -> tuple:
    is_error = bool(case.get("oracle_stderr")) or int(case.get("oracle_exit", 0) or 0) != 0
    return error_signature(case, region) if is_error else success_signature(case, region)


def signature_cap(signature: tuple, *, error_cap: int = 3, success_cap: int = 12) -> int:
    return error_cap if signature and signature[0] == "error" else success_cap
