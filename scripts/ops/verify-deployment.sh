#!/usr/bin/env bash

set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

APP_DIR="${APP_DIR:-$(cd -- "$SCRIPT_DIR/../.." && pwd)}"
ENV_FILE="$(ll_resolve_env_file "$APP_DIR")"
BASE_URL="$(ll_env_or_file LARK_LEDGER_BASE_URL "$ENV_FILE" 'http://127.0.0.1:8000')"
EXPECTED_VERSION=''
if [[ $# -gt 1 ]]; then
  printf '用法：%s [X.Y.Z|vX.Y.Z]\n' "$0" >&2
  exit 2
fi
if [[ $# -eq 1 ]]; then
  EXPECTED_VERSION="$(ll_normalize_version "$1" 2>/dev/null || true)"
  [[ -n "$EXPECTED_VERSION" ]] || {
    printf '[verify] 错误：expected version 必须是 X.Y.Z 或 vX.Y.Z\n' >&2
    exit 2
  }
fi

fail() {
  printf '[verify] 错误：%s\n' "$*" >&2
  exit 1
}

ll_require_command curl || fail '未找到 curl'
[[ "$BASE_URL" =~ ^https?:// ]] || fail 'LARK_LEDGER_BASE_URL 必须是 http:// 或 https:// URL'
BASE_URL="${BASE_URL%/}"

RETRIES="${LARK_LEDGER_VERIFY_RETRIES:-12}"
DELAY_SECONDS="${LARK_LEDGER_VERIFY_DELAY_SECONDS:-2}"
TIMEOUT_SECONDS="${LARK_LEDGER_VERIFY_TIMEOUT_SECONDS:-5}"
[[ "$RETRIES" =~ ^[1-9][0-9]*$ ]] || fail 'LARK_LEDGER_VERIFY_RETRIES 必须是正整数'
[[ "$DELAY_SECONDS" =~ ^[0-9]+([.][0-9]+)?$ ]] || fail 'LARK_LEDGER_VERIFY_DELAY_SECONDS 必须是非负数字'
[[ "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail 'LARK_LEDGER_VERIFY_TIMEOUT_SECONDS 必须是正整数'

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/larkledger-verify.XXXXXX")"
trap 'rm -rf -- "$TMP_DIR"' EXIT

request_json() {
  local endpoint="$1"
  local expected_status="$2"
  local output_file="$TMP_DIR/response.json"
  local attempt status=''

  for ((attempt = 1; attempt <= RETRIES; attempt++)); do
    status="$(curl -sS --max-time "$TIMEOUT_SECONDS" \
      -o "$output_file" -w '%{http_code}' "$BASE_URL$endpoint" 2>/dev/null || true)"
    if [[ "$status" == "$expected_status" ]]; then
      cat -- "$output_file"
      return 0
    fi
    if (( attempt < RETRIES )); then
      sleep "$DELAY_SECONDS"
    fi
  done

  fail "$endpoint 未在 ${RETRIES} 次尝试内返回 HTTP $expected_status（最后状态：${status:-network-error}）"
}

printf 'Deployment verification\n'
printf '%s\n' '----------------------'

request_json '/healthz' '200' >/dev/null
printf 'PASS /healthz      HTTP 200\n'

READY_PAYLOAD="$(request_json '/readyz' '200')"
printf 'PASS /readyz       HTTP 200（migration current）\n'

VERSION_PAYLOAD="$(request_json '/version' '200')"
VERSION="$(ll_json_string_field version "$VERSION_PAYLOAD")"
GIT_SHA="$(ll_json_string_field git_sha "$VERSION_PAYLOAD")"
BUILD_TIME="$(ll_json_string_field build_time "$VERSION_PAYLOAD")"
[[ -n "$VERSION" ]] || fail '/version 缺少 version 字段'
if [[ -n "$EXPECTED_VERSION" && "$VERSION" != "$EXPECTED_VERSION" ]]; then
  fail "/version.version=$VERSION，与目标版本 $EXPECTED_VERSION 不一致"
fi
printf 'PASS /version       version=%s\n' "$VERSION"
printf '      git_sha=%s\n' "${GIT_SHA:-unknown}"
printf '      build_time=%s\n' "${BUILD_TIME:-unknown}"

request_json '/ops/status' '200' >/dev/null
printf 'PASS /ops/status    HTTP 200\n'

# Keep this output machine-readable for the deploy script without exposing any
# environment values or response bodies.
MIGRATION_REVISION="$(ll_json_string_field current "$READY_PAYLOAD")"
printf '      alembic_revision=%s\n' "${MIGRATION_REVISION:-unknown}"
printf 'RESULT: deployment verification passed\n'
