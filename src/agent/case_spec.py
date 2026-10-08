from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal


class BlobStore:
    def __init__(self, root: Path):
        self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True)
    def put(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest(); p = self.root / digest
        if not p.exists(): p.write_bytes(data)
        return digest
    def get(self, digest: str) -> bytes: return (self.root / digest).read_bytes()


@dataclass(frozen=True)
class TreeEntry:
    path: str
    kind: Literal['file', 'dir', 'symlink']
    blob: str | None = None
    target: str | None = None
    mode: int | None = None


@dataclass
class CaseSpec:
    argv: list[str]
    stdin_blob: str | None = None
    cwd: str = '/tmp/work'
    env: dict[str, str] = field(default_factory=dict)
    tree: list[TreeEntry] = field(default_factory=list)
    executor: str = 'oneshot'
    executor_options: dict = field(default_factory=dict)
    comparator: str = 'exact'
    metadata: dict = field(default_factory=dict)

    def to_dict(self):
        d = asdict(self); d['tree'] = [asdict(x) for x in self.tree]; return d


@dataclass(frozen=True)
class ManifestEntry:
    path: str
    kind: str
    mode: int
    size: int
    sha256: str | None = None
    target: str | None = None


def capture_manifest(root: Path, blobs: BlobStore | None = None) -> dict[str, ManifestEntry]:
    root = Path(root); result = {}
    if not root.exists(): return result
    for p in sorted([root] + list(root.rglob('*'))):
        rel = '.' if p == root else p.relative_to(root).as_posix()
        st = p.lstat(); mode = stat.S_IMODE(st.st_mode)
        if p.is_symlink(): ent = ManifestEntry(rel, 'symlink', mode, 0, target=os.readlink(p))
        elif p.is_dir(): ent = ManifestEntry(rel, 'dir', mode, 0)
        elif p.is_file():
            data = p.read_bytes(); digest = hashlib.sha256(data).hexdigest()
            if blobs: blobs.put(data)
            ent = ManifestEntry(rel, 'file', mode, len(data), sha256=digest)
        else: continue
        result[rel] = ent
    return result


def diff_manifests(before: dict[str, ManifestEntry], after: dict[str, ManifestEntry]) -> dict:
    bk, ak = set(before), set(after)
    created, deleted = sorted(ak-bk), sorted(bk-ak)
    modified = sorted(k for k in bk & ak if before[k] != after[k])
    return {'created': created, 'deleted': deleted, 'modified': modified}


def materialize_tree(root: Path, entries: list[TreeEntry], blobs: BlobStore) -> None:
    root = Path(root); root.mkdir(parents=True, exist_ok=True)
    for e in sorted(entries, key=lambda x: (x.path.count('/'), x.kind != 'dir')):
        p = root / e.path
        if e.kind == 'dir': p.mkdir(parents=True, exist_ok=True)
        elif e.kind == 'file':
            p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(blobs.get(e.blob or ''))
        elif e.kind == 'symlink':
            p.parent.mkdir(parents=True, exist_ok=True); p.symlink_to(e.target or '')
        if e.mode is not None and e.kind != 'symlink': p.chmod(e.mode)
