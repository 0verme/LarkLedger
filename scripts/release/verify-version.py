#!/usr/bin/env python3
"""Verify that a release tag matches every in-repo version source.

Usage:
    verify-version.py <tag> [--repo <root>]

Checks (all must hold, otherwise exit 1):
  - tag is strict semver ``vX.Y.Z`` (no pre-release / build metadata)
  - pyproject.toml ``[project].version == X.Y.Z``
  - src/lark_ledger/__init__.py ``__version__ == X.Y.Z``
  - web/package.json ``version == X.Y.Z``
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

TAG_RE = re.compile(r"^v(?P<version>\d+\.\d+\.\d+)$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def load_version_sources(repo_root: Path) -> dict[str, str]:
    """Return {source_name: version} for every in-repo version source."""
    sources: dict[str, str] = {}

    pyproject = repo_root / "pyproject.toml"
    if not pyproject.is_file():
        raise FileNotFoundError(f"missing {pyproject}")
    with pyproject.open("rb") as fh:
        data = tomllib.load(fh)
    sources["pyproject.toml"] = data["project"]["version"]

    init_py = repo_root / "src" / "lark_ledger" / "__init__.py"
    if not init_py.is_file():
        raise FileNotFoundError(f"missing {init_py}")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', init_py.read_text(encoding="utf-8"))
    if not m:
        raise ValueError(f"cannot parse __version__ in {init_py}")
    sources["src/lark_ledger/__init__.py"] = m.group(1)

    package_json = repo_root / "web" / "package.json"
    if not package_json.is_file():
        raise FileNotFoundError(f"missing {package_json}")
    sources["web/package.json"] = json.loads(package_json.read_text(encoding="utf-8"))["version"]
    return sources


def verify(tag: str, repo_root: Path) -> list[str]:
    """Return a list of errors; an empty list means the tag matches every source."""
    errors: list[str] = []
    m = TAG_RE.match(tag)
    if not m:
        errors.append(
            f"tag '{tag}' is not a strict semantic version tag (expected vX.Y.Z, no suffix)"
        )
        return errors
    expected = m.group("version")
    for source, version in load_version_sources(repo_root).items():
        if not VERSION_RE.match(version):
            errors.append(f"{source} declares non-semver version '{version}'")
            continue
        if version != expected:
            errors.append(f"version mismatch: tag says {expected}, but {source} declares {version}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="release tag, e.g. v0.11.0")
    parser.add_argument(
        "--repo", type=Path, default=Path.cwd(), help="repository root (default: cwd)"
    )
    args = parser.parse_args(argv)
    try:
        errors = verify(args.tag, args.repo)
    except (
        FileNotFoundError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        tomllib.TOMLDecodeError,
    ) as exc:
        print(f"::error::version verification failed: {exc}", file=sys.stderr)
        return 1
    if errors:
        for err in errors:
            print(f"::error::{err}", file=sys.stderr)
        return 1
    version = TAG_RE.match(args.tag).group("version")
    print(f"tag {args.tag} matches project version {version} (all version sources agree)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
