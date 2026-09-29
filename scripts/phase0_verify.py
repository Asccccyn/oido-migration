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
    return re.sub(r"[^0-9A-Za-z]", "", s).upper()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--entry", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--answer")
    g.add_argument("--answer-file", type=Path)
    args = ap.parse_args()

    raw = args.answer if args.answer is not None else Path(args.answer_file).read_text()
    expected = normalize((SECRETS / f"{args.entry}.txt").read_text())
    got = normalize(raw)
    if len(got) == len(expected):
        diff = [i for i, (a, b) in enumerate(zip(got, expected)) if a != b]
        if diff:
            print(f"RESULT=MISMATCH entry={args.entry} wrong_positions={diff}")
        else:
            print(f"RESULT=MATCH entry={args.entry}")
    elif expected in got:
        # 模型常带说明文字（"随机码是：XXXXXX"）——码完整在内即运输成功，
        # 记为 CONTAINS 以区别于逐字复述（协议矩阵两列分别记录）
        print(f"RESULT=CONTAINS entry={args.entry} extra_chars={len(got) - len(expected)}")
    else:
        print(f"RESULT=LENGTH_MISMATCH entry={args.entry} got_len={len(got)} expected_len={len(expected)}")


if __name__ == "__main__":
    main()
