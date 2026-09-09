#!/usr/bin/env bash

# Shared helpers for the FNOS deployment scripts. This file must not print or
# source arbitrary environment files: deployment .env files contain secrets.

ll_trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

ll_read_env_value() {
  local file="$1"
  local key="$2"
  local line name value

  [[ -f "$file" ]] || return 1
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    [[ -z "$line" || "${line:0:1}" == "#" ]] && continue
    if [[ "$line" == export\ * ]]; then
      line="${line#export }"
    fi
    [[ "$line" == *=* ]] || continue
    name="${line%%=*}"
    name="$(ll_trim "$name")"
    [[ "$name" == "$key" ]] || continue

    value="${line#*=}"
    value="$(ll_trim "$value")"
    if [[ ${#value} -ge 2 && "${value:0:1}" == "'" && "${value: -1}" == "'" ]]; then
      value="${value:1:${#value}-2}"
    elif [[ ${#value} -ge 2 && "${value:0:1}" == '"' && "${value: -1}" == '"' ]]; then
      value="${value:1:${#value}-2}"
    fi
    printf '%s' "$value"
    return 0
  done < "$file"
  return 1
}

ll_resolve_env_file() {
  local app_dir="$1"
  local candidate="${LARK_LEDGER_ENV_FILE:-$app_dir/.env}"
  if [[ "$candidate" != /* ]]; then
    candidate="$app_dir/$candidate"
  fi
  printf '%s' "$candidate"
}

ll_env_or_file() {
  local key="$1"
  local file="$2"
  local fallback="$3"
  local value

  if [[ -n "${!key-}" ]]; then
    printf '%s' "${!key}"
    return 0
  fi
  value="$(ll_read_env_value "$file" "$key" 2>/dev/null || true)"
  if [[ -n "$value" ]]; then
    printf '%s' "$value"
  else
    printf '%s' "$fallback"
  fi
}

ll_normalize_version() {
  local version="${1:-}"
  if [[ "$version" == v* ]]; then
    version="${version#v}"
  fi
  if [[ "$version" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
    printf '%s' "$version"
    return 0
  fi
  return 1
}

ll_json_string_field() {
  local key="$1"
  local payload="$2"
  printf '%s' "$payload" \
    | tr -d '\r\n' \
    | sed -nE 's/.*"'"$key"'"[[:space:]]*:[[:space:]]*"([^"\\]*)".*/\1/p'
}

ll_state_value() {
  local file="$1"
  local key="$2"
  local line
  [[ -f "$file" ]] || return 1
  line="$(grep -E "^${key}=" "$file" | tail -n 1 || true)"
  [[ -n "$line" ]] || return 1
  printf '%s' "${line#*=}"
}

ll_single_alembic_head() {
  local output="$1"
  printf '%s\n' "$output" \
    | grep -Eo '[0-9]{8}_[0-9]{4}' \
    | sort -u \
    | sed '/^$/d'
}

ll_code_only_compatible() {
  local heads_output="$1"
  local current_revision="$2"
  local heads head_count target_head
  heads="$(ll_single_alembic_head "$heads_output" || true)"
  head_count="$(printf '%s\n' "$heads" | sed '/^$/d' | wc -l | tr -d ' ')"
  [[ "$head_count" == '1' ]] || return 1
  target_head="$(printf '%s\n' "$heads" | sed -n '1p')"
  [[ -n "$current_revision" && "$target_head" == "$current_revision" ]]
}

ll_timestamp() {
  date -u '+%Y-%m-%dT%H:%M:%SZ'
}

ll_require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    printf '错误：未找到命令 %s\n' "$1" >&2
    return 1
  }
}
