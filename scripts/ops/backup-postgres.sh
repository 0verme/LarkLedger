#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

APP_DIR="${APP_DIR:-$(cd -- "$SCRIPT_DIR/../.." && pwd)}"
ENV_FILE="$(ll_resolve_env_file "$APP_DIR")"
BASE_URL="$(ll_env_or_file LARK_LEDGER_BASE_URL "$ENV_FILE" 'http://127.0.0.1:8000')"
BACKUP_DIR="$(ll_env_or_file LARK_LEDGER_BACKUP_DIR "$ENV_FILE" "$APP_DIR/backups")"
DATABASE_URL="$(ll_env_or_file LARK_LEDGER_DATABASE_URL "$ENV_FILE" '')"

fail() {
  printf '[backup] 错误：%s\n' "$*" >&2
  exit 1
}

normalize_database_url() {
  local url="$1"
  case "$url" in
    postgresql://*)
      printf '%s' "$url"
      ;;
    postgresql+*://*)
      printf 'postgresql://%s' "${url#*://}"
      ;;
    *)
      return 1
      ;;
  esac
}

if [[ ! -f "$ENV_FILE" && -z "${LARK_LEDGER_DATABASE_URL:-}" ]]; then
  fail "缺少环境文件：$ENV_FILE，且未通过环境变量提供 LARK_LEDGER_DATABASE_URL"
fi
[[ -n "$DATABASE_URL" ]] || fail "未配置 LARK_LEDGER_DATABASE_URL；拒绝创建未知目标的备份"
ll_require_command pg_dump || fail '请在 NAS 或数据库工具容器中安装 pg_dump'
ll_require_command sha256sum || fail '请安装 sha256sum 以生成备份校验和'

PG_DATABASE_URL="$(normalize_database_url "$DATABASE_URL" 2>/dev/null || true)"
[[ -n "$PG_DATABASE_URL" ]] || fail 'LARK_LEDGER_DATABASE_URL 不是支持的 PostgreSQL URL'

mkdir -p -- "$BACKUP_DIR"

TIMESTAMP="$(date -u '+%Y%m%d_%H%M%S')"
DUMP_FILE="$BACKUP_DIR/larkledger_${TIMESTAMP}.dump"
CHECKSUM_FILE="$DUMP_FILE.sha256"
META_FILE="$DUMP_FILE.meta"
DUMP_TMP="$DUMP_FILE.tmp.$$"
CHECKSUM_TMP="$CHECKSUM_FILE.tmp.$$"
META_TMP="$META_FILE.tmp.$$"
ERROR_TMP="$BACKUP_DIR/.pg_dump_error.$$"

cleanup() {
  rm -f -- "$DUMP_TMP" "$CHECKSUM_TMP" "$META_TMP" "$ERROR_TMP"
}
trap cleanup EXIT

[[ ! -e "$DUMP_FILE" && ! -e "$CHECKSUM_FILE" && ! -e "$META_FILE" ]] || \
  fail "备份文件已存在，拒绝覆盖：$DUMP_FILE"

# Capture metadata before the dump while the currently running application is
# still available. Metadata is best-effort; backup validity depends on pg_dump
# and checksum, not on an HTTP probe.
APP_VERSION='unknown'
if command -v curl >/dev/null 2>&1; then
  VERSION_PAYLOAD="$(curl -sS --max-time 5 "$BASE_URL/version" 2>/dev/null || true)"
  APP_VERSION="$(ll_json_string_field version "$VERSION_PAYLOAD")"
  [[ -n "$APP_VERSION" ]] || APP_VERSION='unknown'
fi

ALEMBIC_REVISION='unknown'
if command -v psql >/dev/null 2>&1; then
  ALEMBIC_REVISION="$(psql "$PG_DATABASE_URL" -Atqc \
    'SELECT version_num FROM alembic_version ORDER BY version_num LIMIT 1' \
    2>/dev/null | head -n 1 | tr -d '\r' || true)"
fi
if [[ -z "$ALEMBIC_REVISION" || "$ALEMBIC_REVISION" == 'unknown' ]] && command -v curl >/dev/null 2>&1; then
  READY_PAYLOAD="$(curl -sS --max-time 5 "$BASE_URL/readyz" 2>/dev/null || true)"
  ALEMBIC_REVISION="$(ll_json_string_field current "$READY_PAYLOAD")"
fi
[[ -n "$ALEMBIC_REVISION" ]] || ALEMBIC_REVISION='unknown'

printf '[backup] 开始 PostgreSQL custom-format backup：%s\n' "$DUMP_FILE"
if ! pg_dump \
  --dbname="$PG_DATABASE_URL" \
  -Fc \
  -Z 6 \
  --file="$DUMP_TMP" \
  2>"$ERROR_TMP"; then
  fail 'pg_dump 失败；未生成可用备份，已停止部署'
fi
[[ -s "$DUMP_TMP" ]] || fail 'pg_dump 生成了空文件；已停止部署'

mv -- "$DUMP_TMP" "$DUMP_FILE"
sha256sum -- "$DUMP_FILE" >"$CHECKSUM_TMP"
mv -- "$CHECKSUM_TMP" "$CHECKSUM_FILE"

{
  printf 'timestamp=%s\n' "$(ll_timestamp)"
  printf 'app_version=%s\n' "$APP_VERSION"
  printf 'alembic_revision=%s\n' "$ALEMBIC_REVISION"
} >"$META_TMP"
mv -- "$META_TMP" "$META_FILE"

printf 'backup_file=%s\n' "$DUMP_FILE"
printf 'checksum_file=%s\n' "$CHECKSUM_FILE"
printf 'meta_file=%s\n' "$META_FILE"
printf 'app_version=%s\n' "$APP_VERSION"
printf 'alembic_revision=%s\n' "$ALEMBIC_REVISION"
