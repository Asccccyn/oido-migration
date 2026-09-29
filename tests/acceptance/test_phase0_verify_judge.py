"""AUD-5 修复的测试钉：verify 的判定逻辑（MATCH/CONTAINS/两态失败）。"""

import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "phase0_verify", Path(__file__).resolve().parents[2] / "scripts" / "phase0_verify.py"
)
verify = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("phase0_verify", verify)
_spec.loader.exec_module(verify)

judge, normalize = verify.judge, verify.normalize


class TestJudge:
    def test_exact(self):
        assert judge("AB3D9K", "AB3D9K") == "MATCH"

    def test_exact_with_separators_and_case(self):
        # 模型复述常带分隔符：归一化后仍是逐字复述
        assert judge("AB3D9K", normalize("a b 3 d - 9 k")) == "MATCH"

    def test_contains(self):
        # "随机码是：AB3D9K" —— 码完整在内，运输成功但非逐字复述
        assert judge("AB3D9K", normalize("随机码是：AB3D9K")) == "CONTAINS"
        assert judge("AB3D9K", normalize("The code reads AB3D9K in the image.")) == "CONTAINS"

    def test_wrong_code_same_length(self):
        assert judge("AB3D9K", normalize("AB3D9M")) == "MISMATCH"

    def test_length_mismatch(self):
        assert judge("AB3D9K", normalize("AB3D")) == "LENGTH_MISMATCH"
        assert judge("AB3D9K", normalize("完全不对")) == "LENGTH_MISMATCH"
