"""Deployment closure tests for the FNOS shell entrypoints."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts" / "ops" / "lib.sh"
VERIFY = ROOT / "scripts" / "ops" / "verify-deployment.sh"
DEPLOY = ROOT / "scripts" / "deploy-fnos.sh"
COMPOSE_IMAGE = ROOT / "compose.image.yaml"
BASH = shutil.which("bash")
CURL = shutil.which("curl")
DOCKER = shutil.which("docker")


def shell_path(path: Path) -> str:
    """Make a Windows worktree path usable by the WSL bash shim when present."""
    value = str(path)
    if os.name == "nt" and len(value) >= 2 and value[1] == ":":
        return f"/mnt/{value[0].lower()}{value[2:].replace(chr(92), '/')}"
    return value


@contextmanager
def materialize_shell_script(script: Path) -> Iterator[Path]:
    """Copy scripts to an ASCII temp path for the Windows WSL bash shim."""
    if os.name != "nt":
        yield script
        return

    with TemporaryDirectory() as temporary_directory:
        directory = Path(temporary_directory)
        relative = script.relative_to(ROOT)
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(script, target)
        if script.name in {"deploy-fnos.sh", "rollback-fnos.sh", "verify-deployment.sh"}:
            ops_directory = directory / "scripts" / "ops"
            ops_directory.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(LIB, ops_directory / "lib.sh")
        yield target


def run_bash(
    script: Path,
    *args: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    if BASH is None:
        pytest.skip("bash is required for deployment script tests")
    with materialize_shell_script(script) as materialized:
        return subprocess.run(
            [BASH, shell_path(materialized), *args],
            cwd=None if os.name == "nt" else ROOT,
            env=env,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )


def run_lib(command: str, *args: str) -> subprocess.CompletedProcess[str]:
    if BASH is None:
        pytest.skip("bash is required for deployment script tests")
    with TemporaryDirectory() as temporary_directory:
        directory = Path(temporary_directory)
        wrapper = directory / "wrapper.sh"
        with wrapper.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(LIB.read_text(encoding="utf-8") + f"\n{command}\n")
        return subprocess.run(
            [BASH, shell_path(wrapper), *args],
            cwd=None if os.name == "nt" else ROOT,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [("0.14.0", "0.14.0"), ("v0.14.0", "0.14.0")],
)
def test_deploy_version_cli_contract_accepts_full_release_versions(
    raw: str, normalized: str
) -> None:
    result = run_lib('ll_normalize_version "$1"', raw)
    assert result.returncode == 0
    assert result.stdout == normalized


@pytest.mark.parametrize("raw", ["latest", "main", "master", "HEAD", "develop", "0.14", "", "v"])
def test_deploy_version_cli_contract_rejects_floating_or_invalid_versions(raw: str) -> None:
    result = run_lib('ll_normalize_version "$1"', raw)
    assert result.returncode != 0


def test_deploy_cli_rejects_missing_version_before_host_preflight() -> None:
    result = run_bash(DEPLOY)
    assert result.returncode == 2
    assert "用法" in result.stderr


class _DeploymentHandler(BaseHTTPRequestHandler):
    responses: dict[str, tuple[int, dict[str, object]]] = {}

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        status, body = self.responses.get(self.path, (404, {"status": "not found"}))
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextmanager
def deployment_server(
    responses: dict[str, tuple[int, dict[str, object]]],
) -> Iterator[str]:
    _DeploymentHandler.responses = responses
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DeploymentHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def run_verify(base_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("LARK_LEDGER_ENV_FILE", None)
    environment["LARK_LEDGER_BASE_URL"] = base_url
    environment["LARK_LEDGER_VERIFY_RETRIES"] = "1"
    environment["LARK_LEDGER_VERIFY_DELAY_SECONDS"] = "0"
    environment["LARK_LEDGER_VERIFY_TIMEOUT_SECONDS"] = "2"
    return run_bash(VERIFY, *args, env=environment)


def _success_responses(version: str = "0.14.0") -> dict[str, tuple[int, dict[str, object]]]:
    return {
        "/healthz": (200, {"status": "ok"}),
        "/readyz": (
            200,
            {
                "status": "ready",
                "checks": {"migration": {"status": "ok", "current": "20260828_0029"}},
            },
        ),
        "/version": (
            200,
            {"version": version, "git_sha": "abc123", "build_time": "2026-08-31T00:00:00Z"},
        ),
        "/ops/status": (200, {"status": "ok"}),
    }


@pytest.mark.skipif(
    os.name == "nt" or CURL is None,
    reason="Linux curl is required for HTTP verification tests",
)
def test_verify_deployment_reports_health_failure() -> None:
    responses = _success_responses()
    responses["/healthz"] = (503, {"status": "down"})
    with deployment_server(responses) as base_url:
        result = run_verify(base_url, "0.14.0")
    assert result.returncode != 0
    assert "/healthz" in result.stderr


@pytest.mark.skipif(
    os.name == "nt" or CURL is None,
    reason="Linux curl is required for HTTP verification tests",
)
def test_verify_deployment_reports_readiness_failure() -> None:
    responses = _success_responses()
    responses["/readyz"] = (503, {"status": "not_ready"})
    with deployment_server(responses) as base_url:
        result = run_verify(base_url, "0.14.0")
    assert result.returncode != 0
    assert "/readyz" in result.stderr


@pytest.mark.skipif(
    os.name == "nt" or CURL is None,
    reason="Linux curl is required for HTTP verification tests",
)
def test_verify_deployment_reports_version_mismatch() -> None:
    with deployment_server(_success_responses("0.13.0")) as base_url:
        result = run_verify(base_url, "0.14.0")
    assert result.returncode != 0
    assert "version" in result.stderr
    assert "0.14.0" in result.stderr


@pytest.mark.skipif(
    os.name == "nt" or CURL is None,
    reason="Linux curl is required for HTTP verification tests",
)
def test_verify_deployment_reports_all_successful_checks() -> None:
    with deployment_server(_success_responses()) as base_url:
        result = run_verify(base_url, "v0.14.0")
    assert result.returncode == 0, result.stderr
    assert "PASS /healthz" in result.stdout
    assert "PASS /readyz" in result.stdout
    assert "version=0.14.0" in result.stdout
    assert "git_sha=abc123" in result.stdout
    assert "build_time=2026-08-31T00:00:00Z" in result.stdout
    assert "PASS /ops/status" in result.stdout


@pytest.mark.skipif(DOCKER is None, reason="Docker Compose is required for compose config tests")
def test_image_compose_requires_explicit_image_tag() -> None:
    with TemporaryDirectory() as temporary_directory:
        directory = Path(temporary_directory)
        compose_file = directory / "compose.image.yaml"
        env_file = directory / ".env"
        shutil.copyfile(COMPOSE_IMAGE, compose_file)
        env_file.write_text(
            "LARK_LEDGER_DATABASE_URL=postgresql+asyncpg://example\n",
            encoding="utf-8",
        )

        missing = subprocess.run(
            [
                DOCKER,
                "compose",
                "--env-file",
                str(env_file),
                "-f",
                str(compose_file),
                "config",
                "--quiet",
            ],
            cwd=directory,
            text=True,
            capture_output=True,
            check=False,
        )
        assert missing.returncode != 0

        env_file.write_text(
            "LARK_LEDGER_IMAGE_TAG=0.14.0\nLARK_LEDGER_DATABASE_URL=postgresql+asyncpg://example\n",
            encoding="utf-8",
        )
        explicit = subprocess.run(
            [
                DOCKER,
                "compose",
                "--env-file",
                str(env_file),
                "-f",
                str(compose_file),
                "config",
                "--quiet",
            ],
            cwd=directory,
            text=True,
            capture_output=True,
            check=False,
        )
        assert explicit.returncode == 0, explicit.stderr


def test_rollback_code_only_proof_and_uncertain_schema_path() -> None:
    matching = run_lib(
        'll_code_only_compatible "$1" "$2"',
        "20260828_0029 (head)",
        "20260828_0029",
    )
    assert matching.returncode == 0

    mismatch = run_lib(
        'll_code_only_compatible "$1" "$2"',
        "20260828_0030 (head)",
        "20260828_0029",
    )
    assert mismatch.returncode != 0

    multiple_heads = run_lib(
        'll_code_only_compatible "$1" "$2"',
        "20260828_0029 (head)\n20260828_0030 (head)",
        "20260828_0029",
    )
    assert multiple_heads.returncode != 0


def test_deployment_scripts_do_not_contain_local_production_build_or_git_pull() -> None:
    source = DEPLOY.read_text(encoding="utf-8")
    assert "git pull" not in source
    assert "--build" not in source
    assert "alembic upgrade head" in source
    assert 'backup_output="$("$APP_DIR/scripts/ops/backup-postgres.sh")"' in source
