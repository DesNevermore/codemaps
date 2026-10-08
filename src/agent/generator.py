from __future__ import annotations

import re
from dataclasses import dataclass, field

from container import Cleanroom
from feature_graph import FeatureGraph, build_feature_graph


_FLAG_RE = re.compile(r"(?<![\w-])(-{1,2}[A-Za-z][\w-]*)")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


@dataclass
class FeatureMap:
    flags: list[str] = field(default_factory=list)
    subcommands: list[str] = field(default_factory=list)
    help_text: str = ""
    version: str = ""
    readme: str = ""

    def summary(self) -> str:
        return (f"flags({len(self.flags)}): {' '.join(self.flags[:60])}\n"
                f"subcommands: {' '.join(self.subcommands) or '(none)'}\n"
                f"version: {self.version.strip()[:80]}")


def mine_docs(room: Cleanroom) -> FeatureMap:
    fm = FeatureMap()

    def _looks_like_help(t: str) -> bool:
        tl = t.lower()
        return ("usage" in tl or "options" in tl or "commands" in tl) and bool(_FLAG_RE.search(t))
    help_txt, fallback = "", ""
    for h in (["--help"], ["-h"], []):
        r = room.run_oracle(h)
        txt = _ANSI_RE.sub("", (r.stdout + r.stderr).decode("utf-8", "replace"))
        if h and _looks_like_help(txt) and not help_txt:
            help_txt = txt
        if len(txt) > len(fallback):
            fallback = txt
    fm.help_text = help_txt or fallback
    fm.version = (room.run_oracle(["--version"]).stdout
                  + room.run_oracle(["-v"]).stdout).decode("utf-8", "replace")

    seen = []
    for m in _FLAG_RE.finditer(fm.help_text):
        f = m.group(1)
        if f not in seen and len(f) <= 30:
            seen.append(f)
    fm.flags = seen

    sub, in_cmds, blanks = [], False, 0

    _HDR = re.compile(r"(?i)^\s*(?:[\w /&-]*\bcommands?\s*:\s*(\S.*)?"
                      r"|the\s+commands?\b[\w ,'()-]*\bare\s*:\s*$"
                      r"|where\s+<?command>?\s+is\s+one\s+of\s*:)\s*$")
    _ROW = re.compile(r"^\s*(?:[-*]\s+)?([a-z][a-z0-9_-]{1,29})\b")
    for line in fm.help_text.splitlines():
        mh = _HDR.match(line)
        if mh:
            in_cmds, blanks = True, 0
            tail = (mh.group(1) or "").strip()
            mt = re.match(r"([a-z][a-z0-9_-]{1,29})\b", tail)
            if mt:
                sub.append(mt.group(1))
            continue
        if in_cmds:
            if not line.strip():

                blanks += 1
                if blanks > 2:
                    in_cmds = False
                continue
            stripped = line.strip()
            group_heading = bool(re.match(r"^\s*(?:--+\s|\[|<)", line)) or stripped.endswith(":")
            if not line[:1].isspace() and not group_heading and not _ROW.match(line):
                in_cmds = False
                continue
            blanks = 0
            m = _ROW.match(line)

            if m and not re.match(r"^\s*-{1,2}[A-Za-z]", line):
                sub.append(m.group(1))
    fm.subcommands = list(dict.fromkeys(sub))

    rd = room.sh("cat /workspace/README* /workspace/readme* 2>/dev/null | head -200")
    fm.readme = rd.stdout.decode("utf-8", "replace")
    return fm


def mine_strings(room: Cleanroom, probes: list[tuple[list, bytes]] | None = None) -> list[str]:
    out: set[str] = set()
    for args, stdin in (probes or []):
        r = room.run_oracle(args, stdin)
        for blob in (r.stdout, r.stderr):
            for tok in re.findall(rb"[A-Za-z][A-Za-z0-9_./ +:-]{2,40}", blob):
                out.add(tok.decode("utf-8", "replace"))
    return sorted(out)


def candidate_coverage(room: Cleanroom, run_args: list[tuple[list, bytes]]) -> dict:
    has = room.sh("python3 -c 'import coverage' 2>/dev/null && echo yes || echo no")
    if b"yes" not in has.stdout:
        return {"available": False}
    room.sh("rm -f /workspace/.coverage")
    for args, stdin in run_args:
        a = " ".join(args)
        room.exec(["sh", "-c",
                   f"cd /tmp/work && python3 -m coverage run -a --source=/workspace "
                   f"/workspace/main.py {a} >/dev/null 2>&1 || true"], stdin=stdin)
    rep = room.sh("cd /workspace && python3 -m coverage report 2>/dev/null | tail -25")
    return {"available": True, "report": rep.stdout.decode("utf-8", "replace")}


def mine_feature_graph(room: Cleanroom, fm: FeatureMap) -> FeatureGraph:
    sub_help = {}
    for sub in fm.subcommands:
        r = room.run_oracle([sub, "--help"])
        text = _ANSI_RE.sub("", (r.stdout + r.stderr).decode("utf-8", "replace"))
        if not text.strip():
            r = room.run_oracle(["help", sub])
            text = _ANSI_RE.sub("", (r.stdout + r.stderr).decode("utf-8", "replace"))
        sub_help[sub] = text
    import os
    return build_feature_graph(fm.help_text, sub_help, fm.subcommands,
                               hard_feature_debt=os.environ.get("PB_HARD_FEATURE_DEBT") == "1")
