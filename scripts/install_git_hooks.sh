#!/usr/bin/env bash
# 安装仓库内 git 钩子（提交前门禁）。用法：
#     bash scripts/install_git_hooks.sh          # 安装
#     bash scripts/install_git_hooks.sh --remove # 卸载
#
# 说明：钩子脚本随仓库分发（scripts/hooks/），但 .git/hooks 不入版本库，
# 因此每个工作副本需执行一次；CI 侧不依赖钩子（流水线会独立再跑一遍门禁）。
set -uo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo .)"
cd "$ROOT"
HOOK_DIR=".git/hooks"
SRC="$ROOT/scripts/hooks"
DST="$HOOK_DIR/pre-commit"

if [ ! -d "$HOOK_DIR" ]; then
  echo "当前目录不是 git 仓库（缺少 $HOOK_DIR）"; exit 1
fi

if [ "${1:-}" = "--remove" ]; then
  rm -f "$DST" && echo "已卸载 pre-commit 钩子"
  exit 0
fi

chmod +x "$SRC"/* 2>/dev/null || true
ln -sf "$SRC/pre-commit" "$DST"
echo "已安装 pre-commit → $SRC/pre-commit"
echo "验证： git commit 时会先跑接口契约检查 + 快速门禁"
echo "临时跳过： SKIP_GATE=1 git commit ...（CI 仍会拦截）"
