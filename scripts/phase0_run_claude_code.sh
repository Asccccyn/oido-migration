#!/bin/bash
# Phase 0 · claude_code 入口硬化运行器（AUD-4：把防作弊边界做成可执行约束）
#
# 边界：CLI 主体与 secrets 同机同用户，0600 挡不住 owner——
# 本脚本强制：临时目录 + 禁全部工具 + 只喂图，使其只能经图片通道获知随机码。
#
# 前置：claude CLI 已安装且已登录（安装/登录需宿主用户批准并本人操作）。
# 用法：cd ~/Projects/oido && bash scripts/phase0_run_claude_code.sh
set -euo pipefail

cd "$(dirname "$0")/.."
command -v claude >/dev/null 2>&1 || { echo "BLOCKED：claude CLI 未安装/不在 PATH"; exit 2; }
[ -f evals/phase0/fixtures/claude_code.png ] || { echo "BLOCKED：夹具缺失（先跑 phase0_make_fixture.py）"; exit 2; }

TMP=$(mktemp -d /tmp/oido-phase0-code.XXXXXX)
cp evals/phase0/fixtures/claude_code.png "$TMP/"
mkdir -p evals/phase0/answers
cd "$TMP"

echo "[run:claude_code] sandbox=$TMP（无 secrets、禁工具，仅图片通道）"
claude -p "这是一张随机码测试图。请只输出图中显示的随机码，逐字符原样复述，不要添加其他内容。" \
  --allowedTools "" claude_code.png \
  > "$OLDPWD/evals/phase0/answers/claude_code.raw.txt"

echo "[run:claude_code] 回答已写入 evals/phase0/answers/claude_code.raw.txt（不回显）"
cd "$OLDPWD"
.venv/bin/python scripts/phase0_verify.py --entry claude_code \
  --answer-file evals/phase0/answers/claude_code.raw.txt
