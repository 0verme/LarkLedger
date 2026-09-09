#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/ops/lib.sh
source "$SCRIPT_DIR/ops/lib.sh"

APP_DIR="${APP_DIR:-/vol2/1000/LarkLedger}"
ENV_FILE="$(ll_resolve_env_file "$APP_DIR")"
COMPOSE_FILE="$APP_DIR/compose.image.yaml"
STATE_DIR="${LARK_LEDGER_STATE_DIR:-$APP_DIR/.deploy-state}"
STATE_FILE="$STATE_DIR/current.env"
LOCK_DIR="${APP_DIR}.deploy.lock"
IMAGE_REPOSITORY='ghcr.io/0verme/larkledger'

log() {
  printf '[rollback] %s\n' "$*"
}

fail() {
  log "错误：$*" >&2
  exit 1
}

cleanup() {
  rmdir -- "$LOCK_DIR" 2>/dev/null || true
}

on_error() {
  local line="$1"
  log "rollback 在第 ${line} 行失败；deployment state 未更新" >&2
  exit 1
}

schema_changing_stop() {
  cat >&2 <<'EOF'
SCHEMA-CHANGING ROLLBACK

自动镜像回滚已停止。

请按照 docs/backup-restore.md /
docs/release-sop.md 执行数据库 restore / rollback。
EOF
  exit 1
}

if [[ $# -ne 1 ]]; then
  printf '用法：%s X.Y.Z|vX.Y.Z\n' "$0" >&2
  exit 2
fi
VERSION="$(ll_normalize_version "$1" 2>/dev/null || true)"
[[ -n "$VERSION" ]] || fail 'rollback 目标必须是明确的 X.Y.Z 或 vX.Y.Z；禁止 latest/main/master/HEAD/develop'

[[ -d "$APP_DIR" ]] || fail "应用目录不存在：$APP_DIR"
[[ -f "$ENV_FILE" ]] || fail "缺少环境文件：$ENV_FILE"
[[ -f "$COMPOSE_FILE" ]] || fail "缺少生产 Compose 文件：$COMPOSE_FILE"
[[ -x "$APP_DIR/scripts/ops/backup-postgres.sh" ]] || fail '缺少可执行的 scripts/ops/backup-postgres.sh'
[[ -x "$APP_DIR/scripts/ops/verify-deployment.sh" ]] || fail '缺少可执行的 scripts/ops/verify-deployment.sh'
ll_require_command docker || fail '未找到 docker，请先安装并启动 Docker'
ll_require_command curl || fail '未找到 curl，无法完成 rollback 验收'
docker compose version >/dev/null 2>&1 || fail '当前 Docker 未提供 docker compose 命令'

mkdir -- "$LOCK_DIR" 2>/dev/null || fail "已有部署或 rollback 正在运行：$LOCK_DIR"
trap cleanup EXIT
trap 'on_error "$LINENO"' ERR

cd -- "$APP_DIR"
COMPOSE_ARGS=(--env-file "$ENV_FILE" -f "$COMPOSE_FILE")
export LARK_LEDGER_IMAGE_TAG="$VERSION"
BASE_URL="$(ll_env_or_file LARK_LEDGER_BASE_URL "$ENV_FILE" 'http://127.0.0.1:8000')"
export LARK_LEDGER_BASE_URL="$BASE_URL"

log "校验 rollback 目标 Compose 配置：$IMAGE_REPOSITORY:$VERSION"
docker compose "${COMPOSE_ARGS[@]}" config --quiet

state_current_version="$(ll_state_value "$STATE_FILE" current_version 2>/dev/null || true)"
current_version_payload="$(curl -sS --max-time 5 "$BASE_URL/version" 2>/dev/null || true)"
current_version="$(ll_json_string_field version "$current_version_payload")"
[[ -n "$current_version" ]] || current_version="$state_current_version"
[[ -n "$current_version" ]] || current_version='unknown'

# Read the database revision from readiness even when readiness is HTTP 503;
# the migration check remains useful during a worker or receiver outage. A
# previously verified state file is the conservative fallback for a crashed
# current container, but absence of both proofs stops rollback.
current_ready_payload="$(curl -sS --max-time 5 "$BASE_URL/readyz" 2>/dev/null || true)"
current_revision="$(ll_json_string_field current "$current_ready_payload")"
[[ -n "$current_revision" ]] || current_revision="$(ll_state_value "$STATE_FILE" alembic_revision 2>/dev/null || true)"
[[ -n "$current_revision" ]] || schema_changing_stop

log "拉取待回滚 GHCR 镜像：$IMAGE_REPOSITORY:$VERSION"
docker compose "${COMPOSE_ARGS[@]}" pull app
TARGET_IMAGE="$IMAGE_REPOSITORY:$VERSION"
TARGET_IMAGE_DIGEST="$(docker image inspect "$TARGET_IMAGE" --format '{{join .RepoDigests ","}}' 2>/dev/null || true)"
TARGET_IMAGE_DIGEST="${TARGET_IMAGE_DIGEST%%,*}"
[[ -n "$TARGET_IMAGE_DIGEST" ]] || fail "无法解析目标镜像 digest：$TARGET_IMAGE"

# Resolve the old image's Alembic head without connecting to the database. A
# matching single head with the verified current DB revision is the narrow
# code-only proof; any uncertainty is deliberately treated as schema-changing.
HEAD_OUTPUT=''
if ! HEAD_OUTPUT="$(docker compose "${COMPOSE_ARGS[@]}" run --rm --no-deps app alembic heads 2>/dev/null)"; then
  schema_changing_stop
fi
if ! ll_code_only_compatible "$HEAD_OUTPUT" "$current_revision"; then
  schema_changing_stop
fi
TARGET_HEAD="$(ll_single_alembic_head "$HEAD_OUTPUT" | sed -n '1p')"

log "已证明 code-only rollback：target_head=$TARGET_HEAD current_revision=$current_revision"

# Preserve the current database before switching the image, even though this
# narrow path does not alter schema. The backup is still the recovery point if
# the old application fails verification.
log '执行 rollback 前 PostgreSQL backup'
backup_output=''
if ! backup_output="$APP_DIR/scripts/ops/backup-postgres.sh"; then
  fail 'rollback 前 PostgreSQL backup 失败；未切换镜像'
fi
BACKUP_FILE="$(printf '%s\n' "$backup_output" | sed -n 's/^backup_file=//p' | tail -n 1)"
[[ -n "$BACKUP_FILE" ]] || fail 'backup 脚本未返回 backup_file；未切换镜像'

log "启动 rollback 镜像：$VERSION"
docker compose "${COMPOSE_ARGS[@]}" up -d --remove-orphans

log "执行 rollback deployment verification：$VERSION"
"$APP_DIR/scripts/ops/verify-deployment.sh" "$VERSION"

verified_version_payload="$(curl -fsS --max-time 5 "$BASE_URL/version")"
verified_ready_payload="$(curl -fsS --max-time 5 "$BASE_URL/readyz")"
VERIFIED_VERSION="$(ll_json_string_field version "$verified_version_payload")"
VERIFIED_GIT_SHA="$(ll_json_string_field git_sha "$verified_version_payload")"
VERIFIED_BUILD_TIME="$(ll_json_string_field build_time "$verified_version_payload")"
VERIFIED_REVISION="$(ll_json_string_field current "$verified_ready_payload")"
[[ "$VERIFIED_VERSION" == "$VERSION" ]] || fail 'rollback 验收后 version 读取异常；deployment state 未更新'
[[ "$VERIFIED_REVISION" == "$current_revision" ]] || fail 'rollback 验收后 migration revision 发生变化；deployment state 未更新'

mkdir -p -- "$STATE_DIR"
STATE_TMP="$STATE_FILE.tmp.$$"
{
  printf '# Generated by rollback-fnos.sh; contains no secrets.\n'
  printf 'current_version=%s\n' "$VERSION"
  printf 'previous_version=%s\n' "$current_version"
  printf 'git_sha=%s\n' "${VERIFIED_GIT_SHA:-unknown}"
  printf 'build_time=%s\n' "${VERIFIED_BUILD_TIME:-unknown}"
  printf 'image=%s\n' "$TARGET_IMAGE"
  printf 'image_digest=%s\n' "$TARGET_IMAGE_DIGEST"
  printf 'alembic_revision=%s\n' "$VERIFIED_REVISION"
  printf 'deployed_at=%s\n' "$(ll_timestamp)"
  printf 'backup_file=%s\n' "$BACKUP_FILE"
} >"$STATE_TMP"
mv -- "$STATE_TMP" "$STATE_FILE"

log 'rollback 完成，deployment state 已原子更新'
