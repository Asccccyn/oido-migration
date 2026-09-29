# oído 实施报告（WP00 + WP01 + WP03 的 P0 基础层）

日期：2026-09-29 · 实施：GLM（程知行）· 基线：OIDO-ARCH-1.1 / OIDO-EXEC-1.1 / D01–D10
状态定名（评审修正）：**基础层完成**，非"首期功能完成"——韵律谱/渲染/MCP/真实语音验收未落地。

## 1. 真实完成

### WP00 只读盘点（完成）

- 工程包四文件 SHA-256 与 MANIFEST 一致，入库 `docs/pack/`。
- 一方现场重录：Siren `03e45f2`（65 条未提交，与包内一致）；**Mariposa HEAD 已前进至 `6df6dfe`**（包内 `b299273` 已过时）；**estómago 实际存在于 `~/Projects/estomago`**（`3ceec9e`，v0.2.2，claude-agent-sdk 0.3.283），包内 L07 的"不可达"系工具限制。
- SSRF 审计（P0）：唯一 URL 入口 `voice_transcribe → downloadWithLimits`，已有 F03/F21 加固（scheme 白名单、v4/v6+NAT64/mapped 阻断、全地址 DNS 检查、逐跳重定向复验、流式大小上限）。**遗留 S-GAP-1（DNS rebinding TOCTOU：校验与 fetch 双解析未钉 IP，P1）、S-GAP-2（无整体 deadline，P3）**——修复归属 Siren，WP10 接入前处理；oído 侧已用"工具不收 URL"合同封死自身面。
- `voice_list` 真实合同已核（conversation_id/message_id/from/to/limit → serializeAsset），D07 拉取路径无需新造 Siren 工具。
- 详见 `docs/WP00_BASELINE.md`。

### WP01 合同与骨架（完成，mock 等级：合成夹具 + 内存/临时目录 SQLite）

- 仓库 `~/Projects/oido`；Python 3.12.14 + venv + `requirements.lock`（freeze 全量；未装 uv，选择已记录）。
- 合同：`contracts/`（audio/analysis/evidence/stream/mcp-tools v1 JSON Schema；证据 schema 内嵌情绪字段封禁）。
- 迁移：`migrations/0001_init.sql`（13 表 + tombstone + outbox + 幂等表 + 审计表）。
- 领域层：半开样本区间/有理数时间映射（`domain/timebase.py`）、半音换算与三参照域（`domain/pitch.py`）、状态机+原因码（`domain/states.py`）、证据合同与 provider 边界净化（`domain/evidence.py`）。
- 存储层：SQLite WAL 迁移器、对象原子写（tmp→sha256→fsync→rename；失败路径零残留）。
- 服务层：资产登记/owner 删除（先取消任务→阻断读取→清对象→tombstone，幂等）、分析任务（幂等 409、租约/fencing、迟到提交拒绝、终态不复活）、事务 outbox。
- API：`/health/live` `/health/ready` `/v1/capabilities`（未装 provider 如实报 `not_configured`）；feature flags 默认全关。**服务未对外监听**（bind_port=0）。

### WP03 P0 采样率正确性（完成）

- `dsp/f0.py`：pYIN 封装**不传 sr 即抛错**（类型层面封死 R02 类缺陷）；元数据携带 librosa 版本与参数。
- A01：440 Hz burst 夹具在 16/24/44.1/48 kHz 下，中位 F0 误差 ≤2% **且** onset 时间定位同时校验；反向对照证明"错 sr 必被检出"（错标 22050 时 onset 落 2.14s、频率报 ~202 Hz）。
- A02：幅度减半 = −6.0206 dB（≤0.01 dB）；A03：无声 → `value=null, below_floor=true`，绝不 0 dBFS。
- `dsp/rms.py` + EXEC §3.5 fixture 值 −9.0308998699 dBFS 复核通过。

### 监督器（完成）

`scripts/supervise_tests.py`：进程树（pgid）RSS+超时监督，SIGTERM→宽限→SIGKILL；自测四项（退出码透传/超时杀/RSS 杀/孙进程树杀）全过。

## 2. 测试（全部受监督、前台、小批）

全量 90 passed（1.38s，numba 已热）——明细见 `acceptance.json.test_runs`。**证据等级：全部合成信号/内存夹具；无任何真实音频、无任何生产系统接入。**

## 3. 未完成（不装完成）

- WP02 媒体探测/解码子进程：**未实现**；`ffmpeg` 未安装（blocker，需安装批准或限定 wav/flac/ogg）。
- WP03 其余（波形包络/STFT/Mel/MFCC/频谱描述量/长文件分块）：未实现。
- WP04–WP09、WP11、WP12：未开始（声质、ASR/对齐/韵律谱、事件、embedding/codec、渲染/查看器、MCP 服务器、实时、Mariposa 关联）。
- Phase 0 四入口图片运输测试：未开始；**需要宿主用户批准后由char在四个真实客户端读码**，涉及 Claude 账号操作，依"涉 Claude 须二次确认"纪律等待明确授权。
- Siren S-GAP-1/2 修复：在 Siren 仓，须遵守其未提交工作保护流程。
- MCP 服务器本体：未实现（六工具合同已冻结于 `contracts/mcp-tools.v1.json`）。

## 4. 与基线的偏离

无语义偏离。两项现场修正均属"重录工作树"预期内：Mariposa HEAD 前进、estómago 可达。

## 5. 下一步（按 EXEC 依赖序）

1. WP02 媒体接入（先解决 ffmpeg 决策）＋ WP03 剩余 DSP（A05–A14 夹具）。
2. Phase 0 四入口随机码测试（等批准）与 Siren S-GAP 修复（WP10 前）。
3. WP05 普通话韵律层 + `qiaosheng` 基线（B16–B18）。

## 6. 回滚

新仓自包含：`rm -rf ~/Projects/oido` 即完全移除；未创建 `~/Data/oido` 生产数据（测试全部用临时目录）；未动 Siren/Mariposa/estómago 任何文件；无网络/隧道/VPN 变更。
