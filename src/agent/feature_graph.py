from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, asdict
from enum import IntEnum


class CoverageLevel(IntEnum):
    UNSEEN = 0
    PARSE_ERROR_ONLY = 1
    ACCEPTED = 2
    HAPPY_PATH = 3
    BOUNDARY = 4
    INTERACTION = 5
    STATEFUL_OR_ARTIFACT = 6


@dataclass
class FeatureObligation:
    feature_path: str
    flag: str | None = None
    value: str | None = None
    value_class: str = "presence"
    target_level: int = int(CoverageLevel.HAPPY_PATH)
    priority: int = 50


@dataclass
class FeatureNode:
    path: str
    flags: list[str] = field(default_factory=list)
    enum_values: dict[str, list[str]] = field(default_factory=dict)
    coverage_level: int = int(CoverageLevel.UNSEEN)
    case_count: int = 0


@dataclass
class FeatureGraph:
    nodes: dict[str, FeatureNode] = field(default_factory=dict)
    obligations: list[FeatureObligation] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"nodes": {k: asdict(v) for k, v in self.nodes.items()},
                "obligations": [asdict(x) for x in self.obligations]}


def _enum_values(line: str) -> list[str]:
    vals = []
    for body in re.findall(r"[\[<{]([^\]}>]{1,120})[\]}>]", line):
        parts = re.split(r"\s*[|,/]\s*", body)

        if 2 <= len(parts) <= 16 and all(re.fullmatch(r'[A-Za-z0-9_.+-]+', p or '')
                                         and not p.startswith('-') for p in parts):
            vals.extend(parts)
    return list(dict.fromkeys(vals))


def parse_help_node(path: str, text: str) -> FeatureNode:
    node = FeatureNode(path=path)
    for line in text.splitlines():
        flags = re.findall(r"(?<![\w-])(-{1,2}[A-Za-z][\w-]*)", line)
        for flag in flags:
            if flag not in node.flags:
                node.flags.append(flag)
            vals = _enum_values(line)
            if vals:
                node.enum_values.setdefault(flag, [])
                node.enum_values[flag] = list(dict.fromkeys(node.enum_values[flag] + vals))
    return node


def build_feature_graph(root_help: str, sub_help: dict[str, str], documented_subcommands: list[str],
                        hard_feature_debt: bool = False) -> FeatureGraph:
    g = FeatureGraph(); g.nodes["(root)"] = parse_help_node("(root)", root_help)
    for sub in documented_subcommands:
        g.nodes[sub] = parse_help_node(sub, sub_help.get(sub, ""))
    for path, node in g.nodes.items():
        for flag in node.flags:
            if flag in {"-h", "--help", "--version", "-V"}: continue
            values = node.enum_values.get(flag) or []
            if values:
                for val in values:
                    g.obligations.append(FeatureObligation(path, flag, val, "documented-enum", 3, 75))
            else:
                g.obligations.append(FeatureObligation(path, flag, None, "presence",
                                                       3 if hard_feature_debt else 2, 45))
            if hard_feature_debt:
                g.obligations.append(FeatureObligation(path, flag, None, "boundary", 4, 55))
        if path != "(root)":
            g.obligations.append(FeatureObligation(path, None, None, "subcommand-happy-path", 3, 90))
            if hard_feature_debt:
                g.obligations.append(FeatureObligation(path, None, None, "boundary", 4, 60))
    return g

def coverage_debt(graph: FeatureGraph, corpus: list[dict], limit: int = 20) -> list[FeatureObligation]:

    if os.environ.get("PB_ABSORB_DISCOVERIES") == "1":
        absorb_discoveries(graph, corpus,
                           os.environ.get("PB_HARD_FEATURE_DEBT") == "1")
    update_coverage(graph, corpus)

    if os.environ.get("PB_DEBT_GUIDANCE") == "0":
        return []
    debt = []
    for ob in graph.obligations:
        node = graph.nodes[ob.feature_path]
        cases = [c for c in corpus if (ob.feature_path == "(root)" or ob.feature_path in c.get("args", []))]
        if ob.value_class == "boundary":

            if not any((int(c.get("oracle_exit", 0) or 0) != 0 or c.get("oracle_stderr"))
                       and (not ob.flag or any(str(a).split('=', 1)[0] == ob.flag
                                               for a in c.get("args", []))) for c in cases):
                debt.append(ob)
            continue
        if ob.flag and not any(any(str(a).split('=',1)[0] == ob.flag for a in c.get('args', [])) for c in cases):
            debt.append(ob); continue
        if ob.value and not any(ob.value in [str(a) for a in c.get('args', [])] for c in cases):
            debt.append(ob); continue
        if node.coverage_level < ob.target_level:
            debt.append(ob)
    return sorted(debt, key=lambda x: -x.priority)[:limit]


_JUNK_PROBE = re.compile(r"^-{1,3}(unknown|bogus|foo|bar|baz|xxx+|nope|invalid|nonexistent|"
                         r"badflag|notaflag|noexist|fake|dummy|garbage)$|"
                         r"^-{3,}|^-$|^-\d+$", re.I)


def _derivable_from_known(flag: str, known: set[str]) -> bool:
    if flag.startswith("--") or len(flag) < 3:
        return False
    body = flag[1:]
    if "--" + body in known:
        return True
    stem = body.rstrip("0123456789")
    if stem != body and "-" + stem in known:
        return True
    return (body.isalpha()
            and all("-" + ch in known for ch in body))


def undocumented_flags(graph: FeatureGraph, corpus: list[dict]) -> dict[str, list[str]]:
    subs = {x for x in graph.nodes if x != "(root)"}
    known = {p: set(n.flags) for p, n in graph.nodes.items()}
    all_known = {f for s in known.values() for f in s}
    found: dict[str, list[str]] = {}
    for case in corpus:
        args = [str(x) for x in case.get("args", [])]
        path = next((x for x in args if x in subs), "(root)")
        for arg in args:
            flag = arg.split("=", 1)[0]
            if not re.fullmatch(r"-{1,2}[A-Za-z][\w-]*", flag) or _JUNK_PROBE.match(flag):
                continue
            if flag in known.get(path, set()) or flag in {"-h", "--help", "--version", "-V"}:
                continue
            if _derivable_from_known(flag, all_known):
                continue
            found.setdefault(path, [])
            if flag not in found[path]:
                found[path].append(flag)
    return found


def absorb_discoveries(graph: FeatureGraph, corpus: list[dict],
                       hard_feature_debt: bool = False) -> list[FeatureObligation]:
    added: list[FeatureObligation] = []
    existing = {(o.feature_path, o.flag, o.value, o.value_class) for o in graph.obligations}
    for path, flags in undocumented_flags(graph, corpus).items():
        node = graph.nodes.setdefault(path, FeatureNode(path=path))
        for flag in flags:
            if flag not in node.flags:
                node.flags.append(flag)
            for value_class, target, priority in (
                    ("undocumented-presence", 3 if hard_feature_debt else 2, 40),
                    *((("boundary", 4, 50),) if hard_feature_debt else ()),
            ):
                key = (path, flag, None, value_class)
                if key not in existing:
                    existing.add(key)
                    ob = FeatureObligation(path, flag, None, value_class, target, priority)
                    graph.obligations.append(ob)
                    added.append(ob)
    return added


_REJECTION = re.compile(r"unknown (option|flag|argument|switch)|unrecogni[sz]ed (option|argument)|"
                        r"invalid (option|switch)|no such option|not a valid (option|flag)|"
                        r"illegal option|unexpected argument", re.I)

def augment_behavioral_obligations(graph: FeatureGraph, profile) -> FeatureGraph:
    specs = []
    inputs, outputs, state = set(profile.inputs), set(profile.outputs), set(profile.state)
    if "source_code" in inputs:
        specs += [("(behavior)/source-code/structure", "code-structure", 5, 100),
                  ("(behavior)/source-code/lexical-lookalike", "string-comment-separation", 5, 100),
                  ("(behavior)/source-code/invalid-incomplete", "syntax-boundary", 4, 90)]
    if "directory_tree" in inputs or "filesystem" in state:
        specs += [("(behavior)/filesystem/tree", "nested-tree", 5, 90),
                  ("(behavior)/filesystem/symlink", "symlink-state", 6, 95)]
    if "structured_document" in inputs:
        specs += [("(behavior)/document/structure", "nested-structure", 5, 90),
                  ("(behavior)/document/link", "relative-link", 5, 85)]
    if "artifact_tree" in outputs:
        specs += [("(behavior)/artifact/tree", "artifact-manifest", 6, 100),
                  ("(behavior)/artifact/semantic", "typed-artifact", 6, 100)]
    if "terminal" in state:
        specs += [("(behavior)/terminal/screen", "pty-screen", 6, 100),
                  ("(behavior)/terminal/lifecycle", "key-resize-signal", 6, 100)]
    if "long_running" in state:
        specs.append(("(behavior)/process/lifecycle", "start-stop-signal", 6, 95))
    existing = {(o.feature_path, o.value_class) for o in graph.obligations}
    for path, value_class, target, priority in specs:
        graph.nodes.setdefault(path, FeatureNode(path=path))
        if (path, value_class) not in existing:
            graph.obligations.append(FeatureObligation(path, None, None, value_class, target, priority))
    return graph
