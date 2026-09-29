#!/usr/bin/env python3
"""Phase 0 答案校验器：只输出 MATCH/MISMATCH，绝不回显期望码。

用法：
  .venv/bin/python scripts/phase0_verify.py --entry claude_web --answer "AB3D9K"
  .venv/bin/python scripts/phase0_verify.py --entry estomago --answer-file evals/phase0/answers/estomago.raw.txt

answer 允许含空格/小写/换行（模型复述常带分隔符），做归一化后精确比较。
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRETS = ROOT / "evals" / "phase0" / "secrets"


def normalize(s: str) -> str:
    # 只剥分隔/标点，保留任何文字（含中文说明）——"随机码是：AB3D9K"
    # 必须判 CONTAINS 而非误判逐字复述 MATCH
    return re.sub(r"[^\w]", "", s, flags=re.UNICODE).upper()


def judge(expected: str, got: str) -> str:
    """AUD-5：区分 MATCH（逐字复述）/ CONTAINS（码在说明文字内）/ 失败两态。"""
    if len(got) == len(expected):
        diff = [i for i, (a, b) in enumerate(zip(got, expected)) if a != b]
        return "MISMATCH" if diff else "MATCH"
    if expected in got:
        return "CONTAINS"
    return "LENGTH_MISMATCH"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--entry", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--answer")
    g.add_argument("--answer-file", type=Path)
    args = ap.parse_args()

    raw = args.answer if args.answer is not None else Path(args.answer_file).read_text()
    expected = normalize((SECRETS / f"{args.entry}.txt").read_text())
    result = judge(expected, normalize(raw))
    if result == "MATCH":
        print(f"RESULT=MATCH entry={args.entry}")
    elif result == "CONTAINS":
        print(f"RESULT=CONTAINS entry={args.entry}")
    else:
        print(f"RESULT={result} entry={args.entry}")


if __name__ == "__main__":
    main()
