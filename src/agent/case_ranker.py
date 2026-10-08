from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from .case_model import merge_signature, is_trivial_surface_case
except ImportError:
    from case_model import merge_signature, is_trivial_surface_case


def _surface_penalty() -> int:

    return int(os.environ.get("PB_SURFACE_RANK_PENALTY", "45"))


@dataclass(frozen=True)
class RankedCase:
    priority: int
    reason: tuple[str, ...]
    case: dict


def case_source(case: dict) -> str:
    found = str(case.get("found_by") or "direct")
    return found.split(":", 1)[0]


def case_modality(case: dict) -> str:
    if (case.get("needs_env") or case.get("services") or case.get("setup_kind")
            or case.get("executor") == "command_sequence"):
        return "stateful"
    if case.get("oracle_newfiles"):
        return "artifact"
    if case.get("files") or case.get("tree") or case.get("oracle_files") or case.get("struct_pre"):
        return "file"
    if case.get("stdin") or case.get("stdin_b64"):
        return "stdin"
    return "argv"


def rank_case(case: dict, *, region: str, existing_signatures: set,
              uncovered_flags: set[str] | None = None) -> RankedCase:
    score = 0; why = []
    modality = case_modality(case)
    if modality in ("artifact", "stateful"):
        score += 80; why.append(modality)
    elif modality == "file":
        score += 55; why.append("file-fixture")
    elif modality == "stdin":
        score += 25; why.append("stdin")
    sig = merge_signature(case, region)
    if sig not in existing_signatures:
        score += 45; why.append("new-semantic-cluster")
    args = [str(a) for a in case.get("args", [])]
    used = {a.split("=", 1)[0] for a in args if a.startswith("-")}
    if uncovered_flags and used & uncovered_flags:
        score += 65; why.append("previously-uncovered-flag")
    is_error = bool(case.get("oracle_stderr")) or int(case.get("oracle_exit", 0) or 0) != 0
    if not is_error:
        score += 35; why.append("successful-path")
    elif modality == "argv" and case_source(case) == "fuzz":
        score -= 25; why.append("argv-error-fuzz")
    if case.get("struct_divergence"):
        score += 35; why.append("structural")
    if case.get("crash_matchable"):
        score -= 35; why.append("diagnostic-crash")
    if is_trivial_surface_case(case):
        penalty = _surface_penalty()
        if penalty:
            score -= penalty; why.append("help-version-surface")
        else:
            why.append("help-version-surface-penalty-disabled")
    return RankedCase(score, tuple(why), case)


def allocate_source_quotas(total: int, available: set[str]) -> dict[str, int]:
    base = {"hunt": .40, "critic": .20, "fuzz": .25}
    quotas = {s: int(total * frac) for s, frac in base.items() if s in available}

    for s in available & base.keys():
        quotas[s] = max(1, quotas.get(s, 0))
    return quotas
