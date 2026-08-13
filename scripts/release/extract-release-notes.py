#!/usr/bin/env python3
"""Extract GitHub release notes for a version.

Sources, in priority order:
  1. ``.github/release-notes/v<version>.md``  (project convention)
  2. CHANGELOG.md section ``## [<version>]``

Guards (exit 1 with a clear error when violated, never an empty release):
  - CHANGELOG.md must contain the ``## [<version>]`` section (release-notes guard)
  - the selected notes source must be non-empty

Usage:
    extract-release-notes.py <tag> [--repo <root>]
        [--output <file>] [--title-output <file>] [--check-only]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

TAG_RE = re.compile(r"^v(?P<version>\d+\.\d+\.\d+)$")
SECTION_RE = re.compile(r"^## \[(?P<version>\d+\.\d+\.\d+)\](?: - .*)?$")


def version_from_tag(tag: str) -> str:
    m = TAG_RE.match(tag)
    if not m:
        raise ValueError(f"tag '{tag}' is not a valid release tag (expected vX.Y.Z)")
    return m.group("version")


def parse_changelog_section(changelog_text: str, version: str) -> str | None:
    """Return the body of the ``## [<version>]`` section, or None when absent/empty."""
    lines = changelog_text.splitlines()
    start: int | None = None
    for i, line in enumerate(lines):
        m = SECTION_RE.match(line.strip())
        if m and m.group("version") == version:
            start = i
            break
    if start is None:
        return None
    body: list[str] = []
    for line in lines[start + 1 :]:
        if SECTION_RE.match(line.strip()):
            break
        body.append(line)
    text = "\n".join(body).strip()
    return text or None


def load_release_notes_file(repo_root: Path, version: str) -> str | None:
    path = repo_root / ".github" / "release-notes" / f"v{version}.md"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


def build_release_notes(repo_root: Path, version: str) -> tuple[str, str]:
    """Return (notes, title).

    Raises ValueError when the CHANGELOG section is missing or when no
    non-empty notes source exists, so a release is never published blank.
    """
    changelog_path = repo_root / "CHANGELOG.md"
    changelog_section: str | None = None
    if changelog_path.is_file():
        changelog_section = parse_changelog_section(
            changelog_path.read_text(encoding="utf-8"), version
        )
    if changelog_section is None:
        raise ValueError(f"CHANGELOG.md has no non-empty section '## [{version}]'")

    notes = load_release_notes_file(repo_root, version) or changelog_section
    if not notes:
        raise ValueError(f"no non-empty release notes found for version {version}")

    title = f"LarkLedger v{version}"
    first_line = notes.splitlines()[0].strip()
    m = re.match(r"^#\s+(.+)$", first_line)
    if m:
        title = m.group(1).strip()
    return notes, title


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="release tag, e.g. v0.11.0")
    parser.add_argument(
        "--repo", type=Path, default=Path.cwd(), help="repository root (default: cwd)"
    )
    parser.add_argument("--output", type=Path, help="write notes to this file instead of stdout")
    parser.add_argument("--title-output", type=Path, help="write the release title to this file")
    parser.add_argument(
        "--check-only", action="store_true", help="only verify guards; write nothing"
    )
    args = parser.parse_args(argv)

    try:
        version = version_from_tag(args.tag)
    except ValueError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    try:
        notes, title = build_release_notes(args.repo, version)
    except ValueError as exc:
        print(f"::error::release notes guard failed: {exc}", file=sys.stderr)
        return 1

    if not args.check_only:
        if args.output is not None:
            args.output.write_text(notes + "\n", encoding="utf-8")
        else:
            print(notes)
        if args.title_output is not None:
            args.title_output.write_text(title + "\n", encoding="utf-8")
    print(f"release notes for v{version} OK (title: {title})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
