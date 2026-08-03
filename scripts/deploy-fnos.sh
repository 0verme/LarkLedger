#!/usr/bin/env bash

set -Eeuo pipefail

readonly APP_DIR="${APP_DIR:-/vol2/1000/LarkLedger}"
readonly REPO_URL="${REPO_URL:-https://github.com/0verme/LarkLedger.git}"
readonly BRANCH="${BRANCH:-main}"
readonly LOCK_DIR="${APP_DIR}.deploy.lock"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

fail() {
  log "错误：$*" >&2
  exit 1
}

cleanup() {
  rmdir "$LOCK_DIR" 2>/dev/null || true
}

trap 'fail "部署在第 ${LINENO} 行失败"' ERR

command -v git >/dev/null 2>&1 || fail "未找到 git，请先在飞牛 NAS 上安装 Git"
command -v docker >/dev/null 2>&1 || fail "未找到 docker，请先安装并启动 Docker"
docker compose version >/dev/null 2>&1 || fail "当前 Docker 未提供 docker compose 命令"

mkdir "$LOCK_DIR" 2>/dev/null || fail "已有部署任务正在运行；如果确认没有，请删除 $LOCK_DIR"
trap cleanup EXIT

if [[ ! -d "$APP_DIR/.git" ]]; then
  if [[ -e "$APP_DIR" ]] && [[ -n "$(find "$APP_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
    fail "$APP_DIR 已存在且不是空目录，无法自动克隆"
  fi

  log "首次部署，正在从 $REPO_URL 克隆 $BRANCH 分支"
  mkdir -p "$(dirname "$APP_DIR")"
  git clone --branch "$BRANCH" --single-branch "$REPO_URL" "$APP_DIR"
fi

cd "$APP_DIR"

origin_url="$(git remote get-url origin 2>/dev/null || true)"
[[ "$origin_url" =~ (^|[:/])0verme/LarkLedger(\.git)?$ ]] || \
  fail "origin 不是 0verme/LarkLedger（当前为：${origin_url:-未配置}）"

current_branch="$(git branch --show-current)"
[[ "$current_branch" == "$BRANCH" ]] || \
  fail "当前分支是 $current_branch，请先切换到 $BRANCH"

[[ -f .env ]] || \
  fail "缺少 $APP_DIR/.env；请复制 .env.example 并填写真实配置后重试"

log "拉取 $BRANCH 分支的最新代码"
git pull --ff-only origin "$BRANCH"

log "校验 Docker Compose 配置"
docker compose config --quiet

log "重新构建并启动容器"
docker compose up -d --build --remove-orphans

log "当前容器状态"
docker compose ps

log "部署完成"
