"""Phase 0 夹具物理完整性（自审计加固：像素统计而非人眼）。

四张夹具必须有真实墨迹、集中在中央带、边缘干净——
不证明可读性（那是四次真实读码的事），但排除"渲染出白图/偏移图"。"""

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

FIXTURES = Path(__file__).resolve().parents[2] / "evals" / "phase0" / "fixtures"
ENTRIES = ("claude_web", "claude_ios", "claude_code", "estomago")


@pytest.mark.parametrize("entry", ENTRIES)
def test_fixture_has_centered_ink(entry):
    img = Image.open(FIXTURES / f"{entry}.png")
    assert img.size == (800, 300)
    a = np.asarray(img.convert("L"))
    ink = a < 128
    total = int(ink.sum())
    assert total > 3000, f"{entry}: ink too sparse ({total}px) — 可能渲染失败"
    assert total < 80000, f"{entry}: ink suspiciously dense ({total}px)"
    H, W = ink.shape
    central = ink[int(H * 0.20):int(H * 0.80), int(W * 0.10):int(W * 0.90)]
    assert central.sum() == total, f"{entry}: 墨迹超出中央安全区（居中计算漂移）"
