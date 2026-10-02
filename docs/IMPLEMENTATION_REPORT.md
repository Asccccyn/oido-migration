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

### WP01 合同与骨架（**部分**——2026-10-01 林石见独立审计后校准，原"完成"声明撤回）

审计确认的缺口（除已修 bug 外仍未做的）：宿主认证适配与 scope、access_audit 实际写入、
HTTP/MCP 共用业务入口、心跳/重启恢复、outbox 与业务事务接线及可靠消费、备份恢复演练。
已落地部分见下，但整体为**部分交付**。

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

全量 147 passed（审计 20 钉 + 复审 R01–R08 的 17 钉）——明细见 `acceptance.json.test_runs`。**证据等级：全部合成信号/内存夹具；无任何真实音频、无任何生产系统接入。**

## 1.5 2026-10-01 独立审计响应（林石见，基线 2bb86fb）

审计结论"当前只能算基础原型，不能按完整插件验收"——**接受**。初轮按 12 项全修申报（aad4c84/76d74dd）。

## 1.6 2026-10-01 复审响应（基线 76d74dd：7 项关闭、5 项部分、新增 R01–R08）

复审纠正了"12 项全部修复"的过度声明——按逐项状态接受，并修复全部 8 项新问题：

| 项 | 修复 | 回归测试 |
|---|---|---|
| R01 | artifact 删除失败**保留数据库行**作重试索引；确认文件删除成功才删行；未清完不 tombstone | TestR01（失败→行在→重试→行删+文件删+终态） |
| R02 | 净化递归**任意深度容器**（list 套 list 套 dict） | TestR02（含 stage.detail 深嵌套全库扫描） |
| R03 | validate_evidence 升级完整类型合同（schema_version/数值/非空串/标志位数组）；持久层用**数据库真实 variant** 校验归属/timebase/sample_count 边界 | TestR03（7 类类型反例 + 4 类归属/边界反例） |
| R04 | 迁移改**显式 BEGIN/COMMIT/ROLLBACK**（根因：sqlite3 默认隔离模式 DDL 不自动开事务，隐式 rollback 罩不住 ALTER）；历史半迁移状态可验证恢复 | TestR04（中断→DDL 回滚→重试成功；遗留半迁移→恢复补登版本） |
| R05 | 空/白字串幂等键统一归一为"无键" | TestR05 |
| R06 | 取消竞态落败后**回读真实终态**如实返回（race_lost 标记），不再谎报 cancelled | TestR06（两连接注入竞争提交） |
| R07 | TTS 音色绑定与 partial 可解释性进入**服务持久化入口**（非仅 schema）；schema 补 origin 非 null、stage 降级必带 reason | TestR07（服务 4 反例 + schema 2 反例） |
| R08 | ready 按**迁移版本全集**（expected_versions 与目录对照）+ 12 张业务表核对 | TestR08（半迁移/缺表/版本集） |
| 低优先 | 探针 SDK 版本从解析目录读 package.json（包不导出 ./package.json 子路径） | TestProbeSdkVersion |

复审确认关闭的旧项：F01、F02（删除意图）、F05、F08（新请求）、F09、F10、F11（10ms）；
旧 F03/F04/F06/F07/F12 的残余部分由 R01/R02/R03/R05/R08 补齐。

| 项 | 修复 | 回归测试 |
|---|---|---|
| F01 复活竞态 | commit/claim 改条件 UPDATE（token+非终态+资产 active 原子校验）；删除递增 fence | TestF01Resurrection×3 |
| F02 删除意图丢失 | 意图先持久化（deletion_requested+墓碑+取消），对象清理失败保持可重试态 | TestF02DeleteIntent×2 |
| F03 证据残留 | 删除清 evidence/stages/artifacts 行与对象；分析读取检查资产生命周期 | TestF03EvidenceCleanup |
| F04 情绪绕过 | 净化递归 list；stage.detail 同净化；持久前 validate_evidence | TestF04F06×2 |
| F05 半开事务 | 全部多语句写库 try/except 回滚；先验证后副作用 | TestF05 |
| F06 非法证据入库 | 持久层完整校验；区间整数/非负/有序；JSON allow_nan=False | TestF04F06 |
| F07 并发幂等裸异常 | IntegrityError→回读比对→去重或 409 | TestF07×2 |
| F08 请求参数丢失 | request_json 持久化，claim 返回 | TestF08 |
| F09 探针不发图 | AsyncIterable\<SDKUserMessage\>+image content block（按 SDK d.ts 实测合同） | TestF09F10（静态钉+node --check；真跑待登录环境） |
| F10 作用域崩溃 | 解析器提升模块作用域 | 同上 |
| F11 短音频伪 F0 | 80ms 不足信号门，拒答不产数 | TestF11×4 采样率 |
| F12 空 ready | 迁移非空+关键表齐全才算 ready | TestF12 |

审计指出的**仍然成立、未在本轮修复**的缺口：宿主认证/scope/access_audit 写入、
transcript_revisions/asset_links/pins 表、temporary 到期清理、feature flags 未被业务调用、
监督器无磁盘预算、打包未含 migrations/contracts、探针未真实运行。逐项列于
`acceptance.json.open_gaps`，不装完成。

## 3. 未完成（不装完成）

- WP02 媒体探测/解码子进程：**未实现**。`ffmpeg` 9.0.2 已装并固定（REVIEW_20260929 裁定 2；文档漂移勘误：本行此前仍写"未安装"），但媒体探测/隔离解码/限额子进程代码未写。
- WP03 其余（波形包络/STFT/Mel/MFCC/频谱描述量/长文件分块）：未实现。
- WP04–WP09、WP11、WP12：未开始（声质、ASR/对齐/韵律谱、事件、embedding/codec、渲染/查看器、MCP 服务器、实时、Mariposa 关联）。
- Phase 0 四入口图片运输测试：**已批准并武装完毕（夹具/探针/校验器/协议矩阵），零次真实读码执行**——Web/iOS 待宿主用户手动发送，Code 入口待 CLI 安装批准，estómago 探针待已登录终端运行（零凭据搬运）。
- Siren S-GAP-1/2 修复：在 Siren 仓，须遵守其未提交工作保护流程。
- MCP 服务器本体：未实现（六工具合同已冻结于 `contracts/mcp-tools.v1.json`）。

## 4. 与基线的偏离

无语义偏离。两项现场修正均属"重录工作树"预期内：Mariposa HEAD 前进、estómago 可达。

## 5. 下一步（按 EXEC 依赖序）

1. 补完 WP01 真实基础（宿主认证/scope/审计写入、outbox 与业务事务接线、心跳与恢复）→ WP02 媒体接入（ffmpeg 已就位）＋ WP03 剩余 DSP（A05–A14 夹具）。
2. Phase 0 四入口随机码测试（等批准）与 Siren S-GAP 修复（WP10 前）。
3. WP05 普通话韵律层 + `qiaosheng` 基线（B16–B18）。

## 6. 回滚

新仓自包含：`rm -rf ~/Projects/oido` 即完全移除；未创建 `~/Data/oido` 生产数据（测试全部用临时目录）；未动 Siren/Mariposa/estómago 任何文件；无网络/隧道/VPN 变更。
