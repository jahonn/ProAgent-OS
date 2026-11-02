#!/usr/bin/env python3
"""Check dist/ sdist and wheel layout. Stdlib only. Does not upload.

Run after ``python -m build``. Expects one wheel and one sdist in ``dist/``.
"""
from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"

WHEEL_REQUIRED = (
    "proagent_os/__init__.py",
    "proagent_os/main.py",
    "proagent_os/web/templates/index.html",
    "proagent_os/web/static/app.js",
    "proagent_os/web/static/app.css",
)
# Cwd-relative repo data. The sdist keeps a source tree; the wheel must not
# install these as top-level paths. See docs/PYPI.md.
SDIST_REQUIRED_PREFIXES = (
    "config/",
    "skills/",
    "verticals/",
    "fixtures/",
)
WHEEL_FORBIDDEN_TOP = frozenset(
    {"config", "skills", "verticals", "fixtures", "tests", "docs", "scripts"}
)
METADATA_NEEDLES = (
    "Name: proagent-os",
    "Version: 0.1.1",
    "License-Expression: MIT",
    "Metadata-Version: 2.",
    "Description-Content-Type: text/markdown",
    "Project-URL: Homepage, https://github.com/jahonn/ProAgent-OS",
    "Project-URL: Repository, https://github.com/jahonn/ProAgent-OS",
    "Project-URL: Issues, https://github.com/jahonn/ProAgent-OS/issues",
    "Classifier: Programming Language :: Python :: 3.11",
    "Classifier: Programming Language :: Python :: 3.12",
)


def _fail(msg: str) -> None:
    print(f"package layout: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _one(pattern: str) -> Path:
    matches = sorted(DIST.glob(pattern))
    if len(matches) != 1:
        _fail(f"expected one {pattern} under {DIST}, found {[p.name for p in matches]}")
    return matches[0]


def _wheel_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        return archive.namelist()


def _sdist_names(path: Path) -> list[str]:
    names: list[str] = []
    with tarfile.open(path) as archive:
        for member in archive.getmembers():
            parts = member.name.split("/", 1)
            names.append(parts[1] if len(parts) == 2 else member.name)
    return names


def _metadata(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        meta_names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(meta_names) != 1:
            _fail(f"{path.name} METADATA entries: {meta_names}")
        return archive.read(meta_names[0]).decode()


def main() -> None:
    if not DIST.is_dir():
        _fail("dist/ is missing; run python -m build first")
    wheel = _one("*.whl")
    sdist = _one("*.tar.gz")
    wheel_names = _wheel_names(wheel)
    missing = [name for name in WHEEL_REQUIRED if name not in wheel_names]
    if missing:
        _fail(f"{wheel.name} missing {missing}")
    leaked = [name for name in wheel_names if name.split("/", 1)[0] in WHEEL_FORBIDDEN_TOP]
    if leaked:
        _fail(f"{wheel.name} contains non-package paths: {leaked[:12]}")
    license_hits = [
        name
        for name in wheel_names
        if name.endswith("/LICENSE") or name.endswith(".dist-info/licenses/LICENSE")
    ]
    if not license_hits:
        _fail(f"{wheel.name} missing LICENSE")
    metadata = _metadata(wheel)
    absent = [needle for needle in METADATA_NEEDLES if needle not in metadata]
    if absent:
        _fail(f"{wheel.name} METADATA missing {absent}")
    # License classifiers are deprecated next to License-Expression (PEP 639).
    if "License :: OSI Approved" in metadata:
        _fail(f"{wheel.name} still carries a License :: classifier")
    sdist_names = _sdist_names(sdist)
    for prefix in SDIST_REQUIRED_PREFIXES:
        if not any(name.startswith(prefix) for name in sdist_names):
            _fail(f"{sdist.name} missing {prefix}*")
    for required in ("pyproject.toml", "README.md", "LICENSE", "proagent_os/__init__.py"):
        if required not in sdist_names:
            _fail(f"{sdist.name} missing {required}")
    print(f"package layout ok: {wheel.name} ({len(wheel_names)} files), {sdist.name}")


if __name__ == "__main__":
    main()
