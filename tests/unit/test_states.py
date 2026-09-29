"""状态机：非法迁移与终态不可复活（EXEC §3.2）。"""

import pytest

from oido.domain.states import (
    IllegalTransition, asset_transition, is_terminal_job, job_transition,
    require_reason, stage_transition,
)


class TestJobTransitions:
    @pytest.mark.parametrize(
        "frm,to",
        [
            ("queued", "running"), ("queued", "cancelled"), ("queued", "failed"),
            ("running", "succeeded"), ("running", "partial"),
            ("running", "failed"), ("running", "cancelled"),
        ],
    )
    def test_legal(self, frm, to):
        assert job_transition(frm, to) == to

    @pytest.mark.parametrize(
        "frm,to",
        [
            ("cancelled", "running"),   # 终态复活
            ("succeeded", "running"),
            ("failed", "queued"),
            ("partial", "running"),
            ("queued", "succeeded"),    # 跳过 running
            ("running", "queued"),      # 回退
            ("bogus", "running"),
        ],
    )
    def test_illegal(self, frm, to):
        with pytest.raises(IllegalTransition):
            job_transition(frm, to)

    def test_terminal(self):
        assert is_terminal_job("cancelled")
        assert is_terminal_job("partial")
        assert not is_terminal_job("running")


class TestStageAndAsset:
    def test_stage_skip_is_not_negative(self):
        assert stage_transition("pending", "skipped") == "skipped"
        with pytest.raises(IllegalTransition):
            stage_transition("skipped", "running")

    def test_asset_lifecycle(self):
        assert asset_transition("active", "deletion_requested") == "deletion_requested"
        assert asset_transition("deletion_requested", "tombstoned") == "tombstoned"
        assert asset_transition("tombstoned", "purged") == "purged"
        with pytest.raises(IllegalTransition):
            asset_transition("purged", "active")           # 墓碑不可逆
        with pytest.raises(IllegalTransition):
            asset_transition("active", "tombstoned")       # 必须经 deletion_requested


class TestReasonCodes:
    def test_known(self):
        assert require_reason("provider_not_configured") == "provider_not_configured"

    def test_unknown(self):
        with pytest.raises(ValueError):
            require_reason("vibe_check_failed")
