from __future__ import annotations

import os
import re
from pathlib import Path


PROMPTS_DIR = Path(os.environ.get("PB_PROMPTS_DIR")
                   or Path(__file__).resolve().parent.parent / "prompts")


_LIST_SEP = "\n\n---\n\n"
_PROVENANCE = re.compile(r"\n*<!--\s*provenance:.*?-->", re.S)


def strip_provenance(text: str) -> str:
    return _PROVENANCE.sub("", text).strip()


def load(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text()


def load_list(name: str) -> list[str]:
    return [strip_provenance(item) for item in
            (PROMPTS_DIR / f"{name}.md").read_text().rstrip("\n").split(_LIST_SEP)]


def load_list_raw(name: str) -> list[str]:
    return (PROMPTS_DIR / f"{name}.md").read_text().rstrip("\n").split(_LIST_SEP)
