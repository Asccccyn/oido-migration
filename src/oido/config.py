"""配置与 feature flags（OIDO-EXEC §9.1：所有生产接入必须可退）。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# 上线开关全集（EXEC §9.1）；默认全关——任何生产接入都须显式打开。
# B6（联合审计 2026-10-27）删除五个仅声明无消费者的开关（est_evidence/siren_sidecar/
# realtime/memory_links/personal_baseline）：只出现在本声明与文档，无 enabled() 行为
# 消费者——声明不等于能力；真接入对应功能时再加回（带真实消费者）。
FEATURE_FLAGS = (
    "ingest",
    "offline_analysis",
    "mcp_tools",
)


@dataclass
class OidoConfig:
    data_root: Path = field(default_factory=lambda: Path(os.environ.get("OIDO_DATA_ROOT", "~/Data/oido")).expanduser())
    # 服务绑定 loopback（ARCH §13），端口待现场查冲突后定；0 = 不监听
    bind_host: str = "127.0.0.1"
    bind_port: int = 0
    # R8：ready 按迁移目录的版本全集核对
    migrations_dir: Path = field(
        default_factory=lambda: Path(__file__).resolve().parents[2] / "migrations"
    )
    flags: dict[str, bool] = field(default_factory=lambda: {f: False for f in FEATURE_FLAGS})

    def enabled(self, flag: str) -> bool:
        if flag not in FEATURE_FLAGS:
            raise ValueError(f"unknown feature flag: {flag!r}")
        return self.flags[flag]

    def ensure_dirs(self) -> None:
        for sub in ("metadata", "originals", "derivatives", "spool", "audit", "backups"):
            (self.data_root / sub).mkdir(parents=True, exist_ok=True)

    @staticmethod
    def from_env() -> "OidoConfig":
        cfg = OidoConfig()
        for flag in FEATURE_FLAGS:
            env = os.environ.get(f"OIDO_FLAG_{flag.upper()}")
            if env is not None:
                cfg.flags[flag] = env.strip().lower() in ("1", "true", "yes", "on")
        return cfg
