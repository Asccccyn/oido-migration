"""AUD-3 修复的确定性测试：CAS 竞争发生时 claim() 必须拒绝授予租约。

用连接代理在 UPDATE 到达前插入一次"竞争写"，单线程内确定性触发竞态，
不依赖线程时序。
"""

import pytest

from oido.service.analyses import AnalysisService
from oido.service.assets import AssetService
from oido.storage.db import migrate, open_db
from oido.storage.objects import ObjectStore


class RaceConnection:
    """第一次租约 UPDATE 到达前，让"另一个 worker"抢先递增 token。"""

    def __init__(self, real):
        self._real = real
        self._armed = False

    def execute(self, sql, params=()):
        if (
            not self._armed
            and sql.startswith("UPDATE analysis_runs")
            and "lease_owner = ?" in sql
        ):
            self._armed = True
            # 真实竞态里赢家是独立事务：先提交，再让输家的 UPDATE 落空
            self._real.execute(
                """UPDATE analysis_runs
                   SET fencing_token = fencing_token + 1, lease_owner = 'winner',
                       lease_expires_at = '2999-01-01T00:00:00.000Z'
                   WHERE analysis_id = ?""",
                (params[5],),
            )
            self._real.commit()
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture()
def env(tmp_path):
    conn = open_db(tmp_path / "meta" / "oido.sqlite3")
    migrate(conn)
    assets = AssetService(conn, ObjectStore(tmp_path / "objects"))
    analyses = AnalysisService(conn)
    audio_id = assets.register_upload(owner="qiaosheng", data=b"\x00" * 64,
                                      source_kind="user_upload")["audio_id"]
    run = analyses.create(audio_id=audio_id, profile="conversation_fast",
                          requested_by="qiaosheng")["analysis_id"]
    return conn, analyses, run


class TestClaimCasRace:
    def test_racy_claim_returns_none_and_rolls_back(self, env):
        conn, analyses, run = env
        racy = AnalysisService(RaceConnection(conn))
        lease = racy.claim(run, worker="loser")
        assert lease is None, "CAS 落败必须返回 None，不得假授予"
        row = conn.execute(
            "SELECT lease_owner, fencing_token FROM analysis_runs WHERE analysis_id = ?",
            (run,),
        ).fetchone()
        assert row["lease_owner"] == "winner" and row["fencing_token"] == 1
        # 落败者没留下任何半写状态
        assert conn.in_transaction is False

    def test_normal_claim_still_works(self, env):
        _, analyses, run = env
        lease = analyses.claim(run, worker="w1")
        assert lease is not None and lease["fencing_token"] == 1
