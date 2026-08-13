"""Runtime build identity contract (P42).

A production instance must be able to answer "which build am I running?"
without invoking ``git`` at runtime (production containers never ship a
``.git`` directory) and without drifting hand-maintained variables.

Sources, in priority order:

* ``LARK_LEDGER_VERSION`` / ``LARK_LEDGER_GIT_SHA`` / ``LARK_LEDGER_BUILD_TIME``
  injected at image build time (Docker ``ARG`` → ``ENV``) by the release
  pipeline;
* ``lark_ledger.__version__`` as the version fallback (the in-repo version
  constant), and stable sentinels for git/build metadata.

This module is transport-neutral Core: it must never import FastAPI, Feishu,
workers, or any adapter, and it never exposes secrets — only the three public
identity fields below.
"""

from __future__ import annotations

from dataclasses import dataclass

from lark_ledger import __version__ as _package_version
from lark_ledger.config import Settings, get_settings

#: Sentinel used when the build pipeline did not inject a git revision.
UNKNOWN_GIT_SHA: str = "unknown"
#: Sentinel used when the build pipeline did not inject a build timestamp.
UNKNOWN_BUILD_TIME: str = ""

#: Field names are a stable public contract (used by ``/version`` and the
#: readiness metadata). Do not rename without a migration of the consumers.
BUILD_INFO_FIELDS: tuple[str, ...] = ("version", "git_sha", "build_time")


@dataclass(frozen=True, slots=True)
class BuildInfo:
    """Redacted, machine-readable identity of the running instance."""

    version: str
    git_sha: str
    build_time: str

    def to_dict(self) -> dict[str, str]:
        return {
            "version": self.version,
            "git_sha": self.git_sha,
            "build_time": self.build_time,
        }


def resolve_build_info(settings: Settings | None = None) -> BuildInfo:
    """Resolve the runtime build identity from settings with safe fallbacks."""
    active = settings or get_settings()
    return BuildInfo(
        version=(active.version.strip() or _package_version),
        git_sha=(active.git_sha.strip() or UNKNOWN_GIT_SHA),
        build_time=active.build_time.strip(),
    )
