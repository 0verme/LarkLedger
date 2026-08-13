"""Tests for scripts/release/ — the tag/version/notes guards used by release.yml.

Full YAML/expression validation of the workflows themselves is done by
actionlint in CI (workflow job); these tests pin the guard behaviour so a
regression is caught without needing a real tag push.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts" / "release"


def load_script(name: str):
    path = SCRIPTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def fake_repo(tmp_path: Path) -> Path:
    """A minimal repo tree whose version sources all agree on 0.11.0."""
    (tmp_path / "src" / "lark_ledger").mkdir(parents=True)
    (tmp_path / "web").mkdir()
    (tmp_path / ".github" / "release-notes").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "lark-ledger"\nversion = "0.11.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "src" / "lark_ledger" / "__init__.py").write_text(
        '"""LarkLedger."""\n__version__ = "0.11.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "web" / "package.json").write_text(
        json.dumps({"name": "lark-ledger-web", "version": "0.11.0"}),
        encoding="utf-8",
    )
    return tmp_path


class TestVerifyVersion:
    def test_matching_tag_passes(self, fake_repo: Path) -> None:
        verify = load_script("verify-version")
        assert verify.verify("v0.11.0", fake_repo) == []

    def test_mismatched_version_fails(self, fake_repo: Path) -> None:
        verify = load_script("verify-version")
        errors = verify.verify("v0.10.0", fake_repo)
        assert errors
        assert "0.11.0" in errors[0]

    @pytest.mark.parametrize("bad", ["0.11.0", "v11", "v1.2", "v1.2.3.4", "v1.2.3-rc1", ""])
    def test_malformed_tag_fails(self, fake_repo: Path, bad: str) -> None:
        verify = load_script("verify-version")
        assert verify.verify(bad, fake_repo)

    def test_version_source_mismatch_fails(self, fake_repo: Path) -> None:
        verify = load_script("verify-version")
        (fake_repo / "web" / "package.json").write_text(
            json.dumps({"name": "lark-ledger-web", "version": "0.10.0"}),
            encoding="utf-8",
        )
        errors = verify.verify("v0.11.0", fake_repo)
        assert any("web/package.json" in error for error in errors)

    def test_missing_version_source_fails(self, fake_repo: Path) -> None:
        verify = load_script("verify-version")
        (fake_repo / "web" / "package.json").unlink()
        with pytest.raises(FileNotFoundError):
            verify.verify("v0.11.0", fake_repo)

    def test_non_semver_source_version_fails(self, fake_repo: Path) -> None:
        verify = load_script("verify-version")
        (fake_repo / "web" / "package.json").write_text(
            json.dumps({"name": "lark-ledger-web", "version": "latest"}),
            encoding="utf-8",
        )
        assert verify.verify("v0.11.0", fake_repo)


class TestExtractReleaseNotes:
    def test_changelog_section_extracted_and_does_not_bleed(self, fake_repo: Path) -> None:
        extract = load_script("extract-release-notes")
        changelog = (
            "## [0.11.0] - 2026-08-20\n\n### Added\n\n- feature of 0.11\n\n"
            "## [0.10.0] - 2026-08-14\n\n### Added\n\n- feature of 0.10\n"
        )
        section = extract.parse_changelog_section(changelog, "0.11.0")
        assert section is not None
        assert "feature of 0.11" in section
        assert "feature of 0.10" not in section

    def test_missing_version_section_fails(self, fake_repo: Path) -> None:
        extract = load_script("extract-release-notes")
        (fake_repo / "CHANGELOG.md").write_text(
            "## [0.10.0] - 2026-08-14\n\n### Added\n\n- x\n", encoding="utf-8"
        )
        assert extract.parse_changelog_section(
            (fake_repo / "CHANGELOG.md").read_text(encoding="utf-8"), "0.9.0"
        ) is None
        with pytest.raises(ValueError, match="CHANGELOG.md has no non-empty section"):
            extract.build_release_notes(fake_repo, "0.9.0")

    def test_empty_section_fails(self, fake_repo: Path) -> None:
        extract = load_script("extract-release-notes")
        (fake_repo / "CHANGELOG.md").write_text(
            "## [0.11.0] - 2026-08-20\n\n## [0.10.0] - 2026-08-14\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="CHANGELOG.md has no non-empty section"):
            extract.build_release_notes(fake_repo, "0.11.0")

    def test_release_notes_file_preferred_with_title(self, fake_repo: Path) -> None:
        extract = load_script("extract-release-notes")
        (fake_repo / "CHANGELOG.md").write_text(
            "## [0.11.0] - 2026-08-20\n\n### Added\n\n- changelog content\n",
            encoding="utf-8",
        )
        (fake_repo / ".github" / "release-notes" / "v0.11.0.md").write_text(
            "# LarkLedger v0.11.0 — Custom\n\nHand-written notes.\n", encoding="utf-8"
        )
        notes, title = extract.build_release_notes(fake_repo, "0.11.0")
        assert "Hand-written notes" in notes
        assert "changelog content" not in notes
        assert title == "LarkLedger v0.11.0 — Custom"

    def test_default_title_from_changelog(self, fake_repo: Path) -> None:
        extract = load_script("extract-release-notes")
        (fake_repo / "CHANGELOG.md").write_text(
            "## [0.11.0] - 2026-08-20\n\n### Added\n\n- x\n", encoding="utf-8"
        )
        notes, title = extract.build_release_notes(fake_repo, "0.11.0")
        assert title == "LarkLedger v0.11.0"

    def test_check_only_cli_passes(
        self, fake_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        extract = load_script("extract-release-notes")
        (fake_repo / "CHANGELOG.md").write_text(
            "## [0.11.0] - 2026-08-20\n\n### Added\n\n- x\n", encoding="utf-8"
        )
        rc = extract.main(["--check-only", "--repo", str(fake_repo), "v0.11.0"])
        assert rc == 0
        assert "OK" in capsys.readouterr().out

    def test_missing_notes_cli_fails(
        self, fake_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        extract = load_script("extract-release-notes")
        rc = extract.main(["--check-only", "--repo", str(fake_repo), "v0.11.0"])
        assert rc == 1
        assert "no non-empty section" in capsys.readouterr().err


class TestVerifyAnnotatedTagScript:
    """The guard script is bash; it is exercised natively in CI (ubuntu).

    On Windows the subprocess/bash path handling is not meaningful, so the
    suite is skipped there (behaviour was verified in the WSL shell directly).
    """

    @staticmethod
    def _git(repo: Path, *args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, encoding="utf-8"
        )

    @pytest.mark.skipif(sys.platform == "win32", reason="bash guard tested on Linux CI")
    def test_annotated_accepted_lightweight_rejected(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        self._git(repo, "init", "-q", "-b", "main")
        self._git(repo, "config", "user.email", "t@example.com")
        self._git(repo, "config", "user.name", "Test")
        (repo / "f").write_text("x", encoding="utf-8")
        self._git(repo, "add", "f")
        self._git(repo, "commit", "-q", "-m", "init")
        self._git(repo, "tag", "v-light")
        self._git(repo, "tag", "-a", "v-annot", "-m", "release")

        script = SCRIPTS_DIR / "verify-annotated-tag.sh"
        ok = subprocess.run(
            ["bash", str(script), "v-annot"], cwd=repo, capture_output=True, encoding="utf-8"
        )
        assert ok.returncode == 0, ok.stderr
        assert "annotated tag" in ok.stdout

        bad = subprocess.run(
            ["bash", str(script), "v-light"], cwd=repo, capture_output=True, encoding="utf-8"
        )
        assert bad.returncode != 0
        assert "not an annotated tag" in bad.stderr

        missing = subprocess.run(
            ["bash", str(script), "v-nope"], cwd=repo, capture_output=True, encoding="utf-8"
        )
        assert missing.returncode != 0

    @pytest.mark.skipif(sys.platform == "win32", reason="bash guard tested on Linux CI")
    def test_missing_argument_fails(self) -> None:
        script = SCRIPTS_DIR / "verify-annotated-tag.sh"
        result = subprocess.run(["bash", str(script)], capture_output=True, encoding="utf-8")
        assert result.returncode != 0
