"""Policy assertions for the CI / release workflows.

Full YAML parsing and expression checks are performed by actionlint in the CI
``workflow`` job. These tests pin the security- and ordering-relevant
invariants so a regression is caught in plain Python (no YAML dependency).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
RELEASE_YML = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
CI_YML = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")


def _permissions_block(yml: str) -> str:
    return yml.split("permissions:", 1)[1].split("concurrency:", 1)[0].split("jobs:", 1)[0]


def _job_block(yml: str, name: str) -> str:
    marker = f"  {name}:"
    start = yml.index(marker) + len(marker)
    rest = yml[start:]
    m = re.search(r"\n  [a-z_]+:\n", rest)
    return rest if m is None else rest[: m.start()]


class TestReleaseWorkflowPermissions:
    def test_minimal_scope(self) -> None:
        perms = _permissions_block(RELEASE_YML)
        assert "contents: write" in perms
        assert "packages: write" in perms
        for forbidden in ("actions:", "issues:", "pull-requests:", "id-token:"):
            assert forbidden not in perms, f"unexpected permission {forbidden}"


class TestReleaseWorkflowJobOrdering:
    def test_validate_then_image_then_release(self) -> None:
        assert "validate:" in RELEASE_YML
        image = _job_block(RELEASE_YML, "image")
        release = _job_block(RELEASE_YML, "release")
        assert "needs: validate" in image
        assert "needs: image" in release

    def test_guards_run_in_validate_before_build(self) -> None:
        validate = _job_block(RELEASE_YML, "validate")
        assert "verify-annotated-tag.sh" in validate
        assert "verify-version.py" in validate
        assert "extract-release-notes.py" in validate
        assert "--check-only" in validate


class TestReleaseWorkflowReleaseJob:
    def test_creates_release_idempotently(self) -> None:
        release = _job_block(RELEASE_YML, "release")
        assert "gh release create" in release
        assert "--verify-tag" in release
        assert "--title" in release
        assert "--notes-file" in release
        # idempotency: never blindly re-create an existing release
        assert "gh release view" in release

    def test_existing_release_verification(self) -> None:
        release = _job_block(RELEASE_YML, "release")
        assert ".tagName" in release
        assert ".isDraft" in release
        assert ".isPrerelease" in release

    def test_no_shell_trace_or_secret_leak(self) -> None:
        release = _job_block(RELEASE_YML, "release")
        assert "set -x" not in release
        assert "GH_TOKEN: ${{ github.token }}" in release

    def test_release_summary_uses_step_summary(self) -> None:
        release = _job_block(RELEASE_YML, "release")
        assert "GITHUB_STEP_SUMMARY" in release


class TestReleaseWorkflowConcurrency:
    def test_same_tag_serialized_without_cancel(self) -> None:
        assert "group: release-${{ github.ref }}" in RELEASE_YML
        assert "cancel-in-progress: false" in RELEASE_YML


class TestReleaseWorkflowBuildIdentity:
    def test_version_arg_is_bare_semver_not_tag_name(self) -> None:
        # github.ref_name keeps the v prefix (v0.11.0) but the runtime build
        # identity contract expects a bare semver (0.11.0). Regression guard:
        # the image job must resolve a stripped version and must not pass the
        # raw ref_name through to the Docker build arg.
        assert "LARK_LEDGER_VERSION=${{ steps.identity.outputs.version }}" in RELEASE_YML
        assert "LARK_LEDGER_VERSION=${{ github.ref_name }}" not in RELEASE_YML
        assert "${GITHUB_REF_NAME#v}" in RELEASE_YML

    def test_build_time_does_not_depend_on_nullable_event_field(self) -> None:
        # github.event.head_commit is null on tag pushes, so the build time
        # must be derived in a step instead of read from the event payload.
        assert "LARK_LEDGER_BUILD_TIME=${{ steps.identity.outputs.build_time }}" in RELEASE_YML
        assert "github.event.head_commit.timestamp" not in RELEASE_YML

    def test_git_sha_baked_from_github_sha(self) -> None:
        assert "LARK_LEDGER_GIT_SHA=${{ github.sha }}" in RELEASE_YML


class TestCIWorkflow:
    def test_actionlint_job_present_and_pinned(self) -> None:
        assert "workflow:" in CI_YML
        workflow = _job_block(CI_YML, "workflow")
        assert "actionlint" in workflow
        assert "1.7.12" in workflow
        assert "actionlint_1.7.12_linux_amd64.tar.gz" in workflow

    def test_no_workflow_dispatch_in_release(self) -> None:
        # workflow_dispatch was deliberately not added: a release must come
        # from a real annotated tag push (see docs/release-sop.md).
        assert "workflow_dispatch" not in RELEASE_YML
