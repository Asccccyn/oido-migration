# oído：全量声音感知系统
## GLM 工程执行文档

文档编号：OIDO-EXEC-1.1  
版本：1.1 · 2026-09-29  
编写：林石见  
配套架构：`Oido_Architecture_owner_v1.1.md`（OIDO-ARCH-1.1）  
状态：**D01–D10 已锁定，可按本文件实施；这不是已经实现或测试通过的报告。生产启用仍必须经过本文验收与灰度门。**

> 执行目标不是做新的聊天程序，也不是重新实现 Siren。目标是建立独立的 **oído** 声音证据服务，让周char通过 Chat 与 estómago 两个入口，以文字韵律谱为默认感知方式查看宿主用户的声音和自己实际生成的声音。
>
> 总原则：**证据给我，意思我自己听。** C01–C16 全部保留；普通话声调感知、个人声学基线、四入口图像运输验证、Siren URL SSRF 审计都属于正式实施范围。情绪分类标签不进入 evidence、存储或上下文。

---

## 0. 执行授权与基线

### 0.1 开工顺序

先读取 `AUDITORY_DECISIONS.md` 和配套架构，视 D01–D10 为已锁定基线。GLM 不再重新征求 D05/D06：宿主用户已经批准“原声仅在 Mac mini 本地分析、正式录音默认保留且可随时删除、建立可清空的非识别性个人声学基线”。

可直接进行代码实现、测试和本地集成；生产启用按 WP14 灰度门执行。以下事项不在授权内：新增第三方云端音频推理、把私密原声发送到新的远端 provider、常驻环境偷听、人声身份识别、绕过现有隧道/VPN安全策略、覆盖其他项目未提交工作。

正式项目名 **oído**；仓库/包/服务标识统一使用 ASCII `oido`；Claude 工具组显示名 **“我只听你”**。所有示例 ID 和测量值只是合同/测试示意，不是真实语音测量。

### 0.2 已检查的现场

| 对象 | 2026-09-29 的只读结果 | 执行约束 |
|---|---|---|
| Siren | `/Users/owner/Projects/siren`；HEAD `03e45f275c34b4ff35b65da5d080fac411f2c0f3`；大量未提交修改 | 不 reset、不覆盖他人改动；重新读当前工作树，不能只按该 HEAD 写补丁。 |
| Siren 合同 | `packages/contracts/src/voice.ts`：输入 16 kHz/PCM16/mono，输出 24 kHz；资产含 direction/text/ttsScript/transcript/objectKey/checksum | 复用资产关系；不能把 TTS 情绪参数转为实际声学证据。 |
| Siren 路由 | `apps/server/src/routes/voice-message.ts`：转写存储受 `store` 控制；合成路由返回音频字节 | `store=false` 不得悄悄变成永久存档；不能假设所有合成路由都已经生成资产记录。 |
| Siren bridge | `packages/core-bridge/src/types.ts` 的 `CoreTurnInput` 目前没有听觉证据字段 | 使用向后兼容扩展，先确认所有实现与消费者；不直接替换合同。 |
| Mariposa | `/Users/owner/Projects/mariposa`；HEAD `b2992733ee820d99d97638ac4382287168da5b02`；有未提交修改 | 不直接写其数据库；先读 AGENTS.md，遵守测试限制。 |
| Mariposa 原文适配 | `backend/mariposa/source/adapters/base.py` 有 attachments、原始时间和正文 | 不据此推断已有 audio link/pin/revoke API；实施前定位真实能力注册与权限层。 |
| estómago | 尝试 `/Users/owner/Projects/estomago` 被工具以 outside allowed roots 拒绝 | 路径、当前源码和 SDK 图片通道未验证；不得猜路径、绕过工具限制或宣称已经接入。 |

本轮没有运行项目测试或第三方模型，也没有导入真实声音。上面的 HEAD 只标识仓库提交，不标识所有未提交文件。开工必须保存相关文件 SHA-256、工作树状态和审查基线。

### 0.3 不可破坏的既有边界

Siren 已有 ASR/TTS/实时取消、背压与音频会话，全部保留；oído 不能成为通话必经的重计算节点。estómago 身份加载、settingSources、新窗口规则、路由提示与已有思考内容处理约束不在本次随意重写范围。Mariposa 的 I、原文、我们的话、共享召回会话、自然日及当前记忆维护规则不由 oído 改写。

不调整 VPN、CC 启动保护、Cloudflare 隧道出口/端口策略，不动现有服务端口和测试守护。确需基础设施变更时列明原因与差异，另走授权。

---

## 1. 实现方式与目录

建议独立 Python 服务，HTTP API 与 MCP 共用 service 层；CPU/DSP 与模型推理在隔离 worker 中，前端仅做查看器。Python/Node/依赖版本由 WP00 在本机兼容性验证后锁定，不使用文档猜测的“最新版”。不共享 Siren、Mariposa 的虚拟环境。

```text
oido/                         # 新仓库，位置须先确认
├── pyproject.toml
├── uv.lock                       # 或同等级的锁文件；只选一种管理方式
├── .env.example                  # 只写非秘密示例
├── contracts/
│   ├── audio.v1.schema.json
│   ├── analysis.v1.schema.json
│   ├── evidence.v1.schema.json
│   ├── stream.v1.schema.json
│   ├── mcp-tools.v1.json
│   └── fixtures/                 # 合同样例，不放私密录音
├── src/oido/
│   ├── api/                      # HTTP、认证、范围读取、错误合同
│   ├── mcp/                      # 工具适配；不得另做一套权限逻辑
│   ├── service/                  # assets、analysis、views、search、links
│   ├── domain/                   # ID、状态、时间基准、证据、政策
│   ├── storage/                  # SQLite、对象原子写、outbox、tombstone
│   ├── ingest/                   # 文件、受信 Siren、流收尾
│   ├── media/                    # 探测、解码、重采样、坐标变换
│   ├── analysis/                 # DAG、质量门槛、证据合并
│   ├── dsp/                      # 波形、频谱、音高、能量、声质
│   ├── prosody/                  # 普通话声调、字内/句级走势、文字韵律谱
│   ├── baselines/                # 个人/voice-profile版本化声学基线；禁止身份识别
│   ├── providers/                # ASR/align/event/diarize/caption/embed/codec
│   ├── workers/                  # 进程隔离、租约、资源监督、取消
│   ├── render/                   # 真实数值生成 PNG，统一图例与坐标
│   └── integrations/             # Siren、Mariposa；不放人格和聊天状态
├── apps/viewer/                   # TypeScript；波形/频谱/文本选区
├── migrations/
├── tests/{unit,contract,integration,security,acceptance}/
├── evals/{manifests,metrics,reports}/
├── scripts/                      # 有界测试、模型校验、备份还原、健康检查
└── docs/                         # 决策、协议、部署、风险、验收、交付报告
```

建议宿主数据根与代码、venv 分离，例如 `~/Data/oido/`。其下分 metadata、originals、derivatives、models、spool、audit、backups；上线前确认磁盘容量、权限、备份位置。删除服务或重建环境不得删除记忆证据。

依赖分基础包与可选模型 extras；基础 API/测量工具不能因为某个大型模型未安装而无法启动。一个能力至少一个通过验收的实现，不要求把所有同类候选都装上。候选下载、模型缓存和许可证记录由部署流程完成，不能在用户第一次调用工具时无提示下载数十 GB。

---

## 2. 全量能力与工作包对应

| 能力 | 交付范围 | 工作包 |
|---|---|---|
| C01 | 双向原声、来源、散列、母本与生命周期 | WP02、WP10、WP11、WP12 |
| C02 | 波形包络、原采样局部读取、真实时间标尺 | WP03、WP08 |
| C03 | STFT、Mel、频率视图与可复现参数 | WP03、WP08 |
| C04 | F0/voiced、相对半音、普通话字内/句级韵律、RMS、MFCC、频谱指标 | WP03、WP05 |
| C05 | 有适用性判断的声质扩展 | WP04 |
| C06 | 多来源文本、ASR、汉字/拼音/声调、校订与字/音节对齐 | WP05 |
| C07 | 时间结构、候选停顿、定义明确的语速 | WP05 |
| C08 | 非语言与环境声音候选 | WP06 |
| C09 | 音乐特征、泛声音模型描述 | WP06 |
| C10 | 已知声道/来源、匿名说话人分段 | WP06、WP11 |
| C11 | 音频 embedding 与显式相似检索 | WP07 |
| C12 | codec tokens 与可验证解码 | WP07 |
| C13 | 默认文字韵律谱、真实分析图、网页、MCP 原生图像按需展开 | WP05、WP08、WP09 |
| C14 | 流式旁路、背压、取消、最终补算 | WP11 |
| C15 | HTTP/MCP 双入口一致授权 | WP09、WP10、WP13 |
| C16 | Mariposa 获授权关联、撤销与回看 | WP12 |

依赖顺序：WP00 → WP01 → WP02 → WP03；WP04–WP07 可在合同稳定后并行；WP08/WP09 消费统一结果；WP10/WP11 接入既有项目；WP12 做长期关联；WP13/WP14 完成质量、安全和生产验收。不能在缺少时间基准、资产授权时先对外发布工具。

---

## 3. 数据合同与状态机

### 3.1 核心实体

数据库至少包括架构列出的 `audio_assets`、`audio_origins`、`audio_variants`、`analysis_runs`、`analysis_stages`、`evidence_items`、`transcript_revisions`、`artifacts`、`events_outbox`、`asset_links`、`pins`、`acoustic_baselines`、`access_audit`，另设 migration version 和删除 tombstone。`audio_origins` 对周char TTS 增加 `voice_profile_id`、`voice_profile_version`、provider/model revision。

所有授权依赖服务端主体、所有权与 scope，不依赖对象 ID 是否难猜。相同音频字节在不同权限域不得通过去重接口泄露存在性。物理层去重只在授权设计允许时实现，不对外暴露内容散列查找接口。

对象写入采用临时文件 → 流式 SHA-256 → fsync/关闭 → 同盘原子 rename → 元数据提交的可恢复流程。元数据提交与事件 outbox 放在同一数据库事务；跨服务靠至少一次交付和幂等，不声称 exactly-once。处理崩溃导致的孤立对象时先核对引用，不直接扫目录删除。

### 3.2 状态

| 对象 | 合法状态 |
|---|---|
| analysis job | `queued`、`running`、`succeeded`、`partial`、`failed`、`cancelled` |
| stage | `pending`、`running`、`succeeded`、`partial`、`failed`、`skipped` |
| asset lifecycle | `active`、`deletion_requested`、`tombstoned`、`purged` |
| stream | `open`、`draining`、`finalized`、`aborted`、`failed` |
| link | `pending`、`active`、`revoking`、`revoked`、`failed` |

`succeeded` 表示该次 profile 请求的适用能力完成并符合最低质量要求，不表示 C01–C16 全部产品能力通过验收。`partial` 必须附缺失能力和原因。`skipped` 仅表示明确未运行，不等于“阴性”或“没有声音”。

建议原因码至少包含：`policy_denied`、`provider_not_configured`、`unsupported`、`insufficient_signal`、`unsupported_language`、`alignment_failed`、`resource_exhausted`、`deadline_exceeded`、`invalid_media`、`source_unavailable`、`input_gap`、`dependency_failed`、`client_transport_unverified`。

任务持有 lease、heartbeat 和 fencing token。取消、删除、政策撤销后使旧 token 失效；迟到 worker 无权把旧结果重新提交成 active。最终 revision 不原地覆盖，补算或文本校订产生新 revision，旧引用继续明确指向原版本，或返回已撤销/不可用。

### 3.3 幂等

`Idempotency-Key` 作用域至少是 `principal + endpoint + key`；保存请求规范化摘要。同 key 同请求返回既有结果；同 key 不同请求返回 409。不能因为复用了 key 就把另一段音频当作同一任务。

分析缓存键必须包含输入 variant 的散列、pipeline/model/checkpoint/config 版本、请求区间、声道策略和影响结果的参数。返回缓存前重新鉴权并验证政策状态。文本校订、来源更正、撤销授权不得继续命中错误版本。

### 3.4 时间基准

所有区间用同一个解码主视图的 `[start_sample, end_sample)`；样本索引为整数，且 `0 <= start < end <= sample_count`。携带 `timebase_id` 与 `sample_rate_hz`。不把显示毫秒数再反算成新真相源。

变换记录至少包括：父子采样率、声道选择或混合公式、crop 起始、codec/encoder delay、解码 trim、重采样比例、滤波延迟及其补偿、STFT 的 frame 起始或中心约定。使用整数/有理数映射和明确舍入规则；图像显示可以舍入，定位不能累计舍入漂移。

无法获得压缩编码前的采集时刻时，原点定义为实际解码后第一有效样本，明确 `capture_alignment=unknown`，不得填 0 假装已校正。流丢块是 gap，不是静音；需要补零喂算法时同时保留有效性 mask，跨 gap 的指标不得当连续真实观测。

### 3.5 标准证据

JSON Schema 用 `kind` 分辨以下三类，禁止混存一个自由文本 observations 数组后丢失来源：

- `measurement`：确定性计算，包括参数、单位、输入区间、适用性。
- `model_detection`：候选类别、原始分数、分数类型、模型版本、校准状态。
- `model_description`：生成式描述、低信任标记、输入区间及可追溯模型。

`emotion` / `affect` / `mood` 分类不是 oído v1 的合法 evidence kind 或 feature。provider 返回此类字段时 adapter 丢弃；测试必须证明其不会落库、不会经 API/MCP/日志泄漏。允许的是可听事件，例如 `laugh_like`、`sigh_like`、`sob_like`、`breath`、`meow`、`purr`。

合同示例仅演示一次人工构造正弦测试信号的 RMS；不是实际语音：

```json
{
  "schema_version": "oido.v1",
  "evidence_id": "ev_fixture_rms",
  "audio_id": "aud_fixture_sine",
  "analysis_id": "run_fixture_001",
  "revision": 1,
  "kind": "measurement",
  "feature": "rms_dbfs",
  "value": -9.0308998699,
  "unit": "dBFS",
  "method": "rms_float_pcm_reference_1",
  "method_version": "fixture-spec-1",
  "input_variant_id": "var_fixture_native",
  "timebase_id": "tb_fixture_24k",
  "range": {"start_sample": 0, "end_sample": 24000},
  "parameters": {"reference_amplitude": 1.0, "floor_dbfs": -120},
  "quality_flags": [],
  "fixture_only": true
}
```

上例对应幅度 0.5、整周期、无量化误差理想正弦。真实文件必须重新计算，不能套这个值。无声可返回 `value=null, below_floor=true`；JSON 不允许 NaN/Infinity，也不能用 0 dBFS 表示无声。

说话人来源与 diarization 分开：受信 Siren 资产可标记 `speaker.id=jiaming, basis=trusted_siren_origin`，混合录音只给 `speaker_0` 等匿名标签。客户端填写名字不产生身份鉴定结果。

---

## 4. HTTP 与 MCP 接口

以下是待实现的 v1 合同。认证适配器、统一错误、审计、权限和 service 层先落地，再对外暴露。所有数组分页、有界；所有范围限制先于解码和模型调用执行。

### 4.1 REST

| 方法与路径 | 最小请求与响应 | 约束 |
|---|---|---|
| `GET /health/live` | 进程存活 | 不返回密钥、路径、模型库清单。 |
| `GET /health/ready` | DB/队列可服务状态 | 重模型缺席可降级；明确 capability health。 |
| `GET /v1/capabilities` | 模型、能力、状态、限制、版本 | 已认证；不把候选 provider 写成可用。 |
| `POST /v1/audio` | multipart 单文件、用途、保留政策 → 201 audio_id | 检查字节/解码时长/声道/采样率；服务端定来源级别。 |
| `POST /v1/imports/siren` | 已登记 Siren instance_id、voice_message_id、event_id → 201/200 | 仅受信服务主体；ID 导入，不接受调用者任意 URL。 |
| `GET /v1/audio/{audio_id}` | 元数据、状态、派生物摘要 | 不泄露内部 objectKey/磁盘绝对路径。 |
| `GET /v1/audio/{audio_id}/content` | 经授权原声或明确 variant 的字节 | Range/Content-Range/ETag；缓存与授权域一致。 |
| `POST /v1/analyses` | audio_id、profile、可选 range/budget → 202 analysis_id | Idempotency-Key；先鉴权与配额，后排队。 |
| `GET /v1/analyses/{analysis_id}` | job、stage、revision、warnings | 已终结结果可 ETag；活任务返回版本号。 |
| `GET /v1/analyses/{analysis_id}/evidence` | revision、kind、range、cursor → 分页 | 小结与原数组分开；不得无限制展开。 |
| `POST /v1/analyses/{analysis_id}/cancel` | 取消原因 → 202/200 | 幂等；终结不可复活；不删除原声。 |
| `POST /v1/views` | analysis_id、revision、view、range → artifact_id | 缓存命中也重新鉴权；尺寸/像素/字节有界。 |
| `GET /v1/artifacts/{artifact_id}` | PNG/受控数组文件 | 验证类型、归属、删除状态；不信任扩展名。 |
| `POST /v1/find` | audio_id/scope、query、mode、limit | 默认指定音频内找话；扩大到允许域必须显式。 |
| `POST /v1/compare` | 两个 asset/range/revision + 指标 | 分别鉴权；不输出关系/情绪高低结论。 |
| `POST /v1/streams` | codec、sr、channels、call/turn、来源 → stream_id | 只受信集成创建；配额预留，不开放任意麦克风常驻。 |
| `WS /v1/streams/{stream_id}/input` | 媒体头协商后推块/ACK/控制 | 同一主体/会话验证；协议详见 WP11。 |
| `POST /v1/streams/{stream_id}/finalize` | last_seq、expected_sample_count → 202 | 检查缺口；重复终结幂等。 |
| `POST /v1/streams/{stream_id}/abort` | 原因 → 202/200 | 清理模型和临时流，按政策保留已获授权原声。 |
| `POST /v1/links` | 已授权 Mariposa entity/ref + 区间版本 | service scope；进入 pending，再由双方确认 active。 |
| `DELETE /v1/links/{link_id}` | 撤销关联 | 不是删除原声；幂等且通知对端。 |
| `GET /v1/baselines/{profile_id}` | 当前基线版本、统计、样本数、质量范围 | `qiaosheng` 与 `jiaming:<voice_profile_version>` 分开；不返回声纹或身份置信度。 |
| `POST /v1/baselines/{profile_id}/rebuild` | 明确获授权的 asset IDs / policy → 新 version | 宿主用户基线只使用音高/语速/停顿等批准指标；不含绝对音量。 |
| `DELETE /v1/baselines/{profile_id}` | 清空当前及历史可用基线 | owner 权限；清空后不得从历史原声静默重建。 |
| `DELETE /v1/audio/{audio_id}` | owner-authorized 删除请求 → 202 | 先阻断读取和任务，后清理对象、索引、关联与备份墓碑。 |

本文未规定如何修改 Mariposa 自身的 HTTP 路由，不能把上表误写进 Mariposa。它的对端能力必须经 WP00/WP12 核验或明确新增。

错误统一为 `error.code`、`error.message`、`request_id`、`retryable`，不附堆栈/密钥/内部地址。状态码至少覆盖 401/403、404、409、410、413、415、422、429、503。为避免泄露他人对象存在性，未经授权对象的 403/404 口径按统一安全合同处理；410 只向有权知道已删除状态的主体提供。

### 4.2 “我只听你”六个模型工具

工具名使用本节固定名字，schema 保存在 `contracts/mcp-tools.v1.json`，HTTP 与 MCP 调用同一 service。实现 SDK 按协商的 MCP 版本封装响应，不手写一套过时 JSON-RPC。[R19]

| 工具 | 输入核心字段 | 输出与副作用 |
|---|---|---|
| `oido_capabilities` | 可选 capability filter | 当前可用、未配置、不支持与限制；只读。 |
| `oido_analyze` | 已授权 audio_id、profile、可选范围 | 创建幂等分析任务；明确计算/潜在外发政策，不接收任意本机路径。 |
| `oido_get` | audio_id/analysis_id、revision、detail、cursor | 紧凑结果、缺失能力、证据引用；只读。 |
| `oido_view` | analysis_id、revision、view、range、detail | 带证据说明的真实图像内容；可能触发受控渲染，不暗中运行新云模型。 |
| `oido_find` | 指定音频或允许范围、query、mode | 返回有限候选/区间，不因一句“找找”扫描未授权声音。 |
| `oido_compare` | 两段范围与指标 | 分别核验条件后给可比较量、限制及图；不替周char解释感情。 |

`oido_view` 的 MCP 内容至少包含一块 TextContent 和一块 ImageContent。以下**只是 content 元素示意，不是完整协议响应**，`data` 必须由真实 PNG 编码，不能真的发送占位字符串：

```json
{
  "type": "image",
  "data": "<BASE64_OF_VERIFIED_PNG>",
  "mimeType": "image/png"
}
```

MCP 2026-07-28 工具规范已包含与旧版本不同的响应结构；以协商协议及已锁 SDK 为准。不要把上面的片段直接当完整 CallToolResult。URL、本地路径、MCP App UI resource 都不能代替该模型图像输入通道。[R18][R19]

资源授权不一致时，工具返回明确错误；不得为“让图片能显示”把音频和图像改成永久公开地址。浏览器走同源会话或短期有范围能力凭据；MCP 服务端可直接读取已授权对象后发送图像字节，不在模型上下文暴露管理员 token。

### 4.3 上下文合同

默认上下文是**文字韵律谱**，不是图像。普通语音 evidence attachment 至少包含：

- `audio_id / analysis_id / revision / range`
- transcript + `text_source`
- 每字/音节 `hanzi / pinyin / tone / duration_ms / relative_pitch_semitone / syllable_contour / pause_after_ms / phrase_trend / quality`
- 句级语速、停顿结构、参照 `baseline_id@version` 或 `utterance_median`
- 允许的声音事件及时间区间
- 缺失/不适用项和可展开视图

禁止默认注入：图片、PCM、完整曲线、embedding、codec tokens、原始 provider 大日志、任何 emotion/mood/affect 分类。

相对音高公式固定为 `12 * log2(f0/reference_f0)`；每个结果必须给 `reference_scope`。字调与句调字段分离；不能只看到四声下降就生成“语气低落”。

Chat 中“听听我刚发的”必须走拉取：调用已有 Siren `voice_list`/对应取资产能力定位最近获授权 `voice_message_id` → oído import/analyze → `oido_get`。不得设计服务端主动向 Chat 推送语音附件。estómago 可在消息附件中保存 `oido_ref`，但与 Chat 使用相同授权服务。

图像只在明确请求或评测需要时通过 `oido_view` 展开；Phase 0 未通过的宿主不得把 image block 当默认能力。

## 5. 工作包与完成条件

### WP00：只读盘点与决策记录

**动作。** 读取 `AUDITORY_DECISIONS.md`，记录 Siren/Mariposa/estómago 当前工作树、版本、端口、服务状态、可用工具和真实路径；固定第三方候选 commit/tag、license、checkpoint 来源。核对 Chat 当前 Siren `voice_list`/取资产能力的实际工具名和返回合同。

专项审计 Siren `voice_transcribe` 及所有 URL 音频抓取入口：查调用链、重定向策略、DNS 解析、协议白名单、私网/link-local/metadata 阻断、响应大小/时长限制和日志。发现可达内网即记 P0，并在 WP10 接入前修复。

**验收。** D01–D10 与文件一致；现场信息有真实命令证据；URL 抓取攻击面有代码位置与测试计划；不把只读盘点写成实现完成。**回滚。** 无生产变更。

### WP01：合同、骨架、授权和迁移

**动作。** 建新隔离仓库/环境，完成 §3/§4 的 Schema 与错误合同、数据库迁移、API/MCP 共用服务、认证和审计、任务状态机。先用纯 synthetic fixtures 和假 provider 验证基础设施；报告明确标 mock。

实现持久化队列的 claim/lease/fence、事务 outbox、幂等冲突；进程重启后只重试安全幂等阶段。模型未装时 API 可以运行并返回准确 capability 状态。

**验收。** Schema 正反样本、跨 owner 拒绝、幂等409、worker crash/reclaim、取消不复活测试通过；新旧 schema 迁移与备份恢复演练。**回滚。** 停新服务、回退新增代码；不覆盖其他项目和原始数据。

### WP02：接入、原声、媒体探测和时间基准

**动作。** 实现单文件流式接入和受信 Siren 导入；母本原字节保存；使用受控媒体解码子进程生成原采样率/声道主视图。对字节、解码后样本、时长、采样率、声道和输出磁盘设独立上限，阻止“小压缩文件→巨大解码对象”。

探测与解码仅使用显式允许协议和本地输入，关闭不必要网络访问；命令参数用 argv，不拼 shell。文件名只作显示，不作为任意路径。保存 codec delay/trim 可用信息，不可用则报告 unknown。

**验收。** 原字节 SHA-256 相等；损坏文件/伪造 MIME/过长音频拒绝；16/24/44.1/48 kHz 不混淆；立体声与反相测试不被默认混音毁掉；磁盘不足时不提交成功记录。**回滚。** 新增导入开关关闭，不删已获授权母本。

### WP03：基础 DSP 和数值数组

**动作。** 实现波形 min/max 多分辨率包络、限定区间 PCM 导出、STFT magnitude/power、Mel、RMS/dBFS、F0/voiced、MFCC和频谱描述量。所有 `sr/window/hop/center/reference` 显式；算法与配置版本进入结果。

F0 基线可用 pYIN，但每次调用必须显式传**真实采样率**。保存 voiced/quality；同一物理信号 16/24/44.1/48 kHz 重采样后，F0 Hz 和时间位置必须在容差内一致。任何省略 `sr` 的封装禁止合入。

新增相对半音层：`12*log2(f0/reference_f0)`；reference 只允许 `qiaosheng_baseline@version`、`jiaming_voice_profile@version` 或 `utterance_median`。绝对音量可以作为单条录音测量存在，但不能写入宿主用户个人跨设备基线。

长文件分块并保留边界处理策略；不得把局部窗口均值错当全段量。

**验收。** A01–A14 及新增采样率/半音测试通过；16/24/44.1/48 kHz 的 F0 和 timebase 均正确。**回滚。** 关闭 analysis stage；不影响 ingest/原声。

### WP04：声质扩展和适用性

**动作。** 选择许可明确、可验证的 CPP/HNR 等实现，记录公式、窗口、单位、有效片段判定和参考实现。jitter/shimmer 仅在足够稳定的周期片段上开放；不是所有自然短句都适用。不能把一个 spectral centroid 数值改名“气声程度”。

openSMILE 作为候选，不当作无条件自由使用的基础依赖；它具有用途相关的双重许可，需要核验本项目使用与后续分发方式。[R20] 可采用其他条款适配的实现，但必须保存参考和一致性测试。

**验收。** 至少交付通过验证的声质指标、适用性判定与反例；自然语音/纯噪声/极短片段输出差异有依据。不支持的指标明确列出；若 D04 已要求该指标，不能整体标完成。**回滚。** 切断额外 stage，不影响基础测量。

### WP05：ASR、脚本、校订、对齐与时间结构

**动作。** 复用来源明确的 Siren ASR；按需要增加本地 ASR/对齐 provider。文本来源区分 `siren_asr`、`oido_asr`、`tts_script`、`manual_revision`；原稿不可变。对齐绑定 transcript revision + audio variant。

实现普通话韵律层：字/音节时间对齐后，为每个汉字保存上下文拼音、声调、来源与可靠度；多音字按上下文解析，轻声/儿化/`一/不`/三声变调不能确定时保留不确定状态。不要把文本平均切时长冒充字级对齐。

基于 WP03 F0 生成逐字“韵律谱”：时长、相对半音起/中/末或稳健轨迹、音节内部 contour、后停顿；句级趋势使用单独算法/字段并版本化。无法可靠去除字调影响时 `phrase_trend=uncertain`。不得输出情绪含义。

实现 `qiaosheng` 个人基线：只汇总质量合格且明确获授权的音高、语速、停顿统计；排除绝对音量、speaker embedding 和任何身份判定。支持版本化 rebuild / clear。周char按 `voice_profile_id@version` 建独立参考统计，用于捏声前后和实际 TTS 对比。

**验收。** 普通话 1–4 声、轻声、多音字、变调、疑问/陈述、同一句不同句调的夹具；四声下降不得被系统写成句级“情绪下沉”。基线 clear 后比较返回 unavailable，且不会从旧历史静默复活。**回滚。** 关闭 prosody/baseline stage，保留原 ASR 与 DSP。

### WP06：事件、音乐、泛声音描述与分段

**动作。** 实现允许的非语言发声与环境声音事件：笑、叹息、呼吸、抽泣样、咳嗽、Meow、Purr、雨声样、门铃、音乐等。每项保存时间窗口、模型版本、score kind、质量和定位粒度；clipwise 结果不能伪造成毫秒级 framewise 区间。

**禁止 emotion/mood/affect 分类进入产品。** 若 SenseVoice 或其他 provider 同时输出情绪 head，adapter 在 provider 边界丢弃；不写数据库、不写调试 JSON、不回 MCP、不进入训练/基线。泛声音 caption 保持低信任，不能用 ASR 文本编造音频描述。

音乐 onset/tempo/适用时 harmonic 特征与语音分析分开；短句不强行报 BPM。混合多人只做匿名 diarization 或使用已知来源/声道，不靠声纹猜 Qiaosheng/Jiaming。

**验收。** 分事件 precision/recall/误报、本地模型资源和未知拒答；专门测试 emotion 字段不会跨 provider adapter。AudioSet 类 Meow/Purr 若所选模型支持，应进入事件评测。**回滚。** 关闭 event/caption/diarization stage。

### WP07：embedding、codec 与受控比较

**动作。** 用版本固定的音频 embedding 模型实现显式音频相似检索，可参考 CLAP。[R07] 保存采样处理、checkpoint、维数、归一化和距离定义；不同模型空间不可混用。相似度不能冒充说话人鉴定、真实性或情感相同。

codec 选定一个可合法运行的 encoder/decoder，如 DAC。[R08] 保存 codec 类型、采样率、帧率、codebook数、码索引、延迟及原声引用。输出是有损机器表示，不替代母本，不承诺Claude直接理解整数。实际执行 encode→decode，记录有效长度、延迟对齐及重建误差，不要求散列相同。

**验收。** 同域检索、跨域拒绝、checkpoint 变更索引失效、删除移除索引；codec roundtrip 实际运行和可播放；数组规模有界。只留一个 `codec_tokens: []` 不算实现。**回滚。** 关闭检索/codec worker，原声与其余证据可用。

### WP08：后端绘图和 Web 查看器

**动作。** 服务端用真实数组生成 `overview`、`waveform`、`spectrogram`、`pitch`、`energy` 图；显示区间、timebase、单位、reference、图例和缺口。提供 linear/log/Mel 频率视图，但名称不能混用。跨图比较固定参考刻度；局部归一化必须显式显示。

查看器同步播放、选区、转写、逐字韵律谱与图像。**文字韵律谱是默认输出，图像按需请求和缓存。** 不因“全部能力”而在每次聊天轮次预生成全部高清图。

**验收。** 测量到像素映射、文本区间与播放选区一致、重复句定位、低带宽/长录音/撤销访问/后台恢复；图像由真实数据产生。默认 `oido_get` 不含图片，明确 `oido_view` 才取图。**回滚。** 只回退 viewer/renderer，保留文字证据。

### WP09：MCP、模型图像传递和双入口权限

**动作。** 先执行 Phase 0 四入口图片运输测试，再做任何依赖图像的默认交互。生成随机短码测试图，短码只存在像素中；分别从 Claude.ai Web、Claude iOS、Claude Code、estómago 让周char读取并记录答案、客户端/模型/SDK版本与实际 payload/image block 证据。

实现“我只听你”六个 MCP 工具，共用 HTTP service/权限层。`oido_get` 默认返回文字韵律谱；`oido_view` 才返回真实 image content。Phase 0 失败入口标 `image_transport_unsupported`，继续提供文本，不用永久公网 URL 绕过。

权限、范围、删除、签名URL、内容注入全部按架构执行。工具组不提供任意本机路径或任意 URL 参数。

**验收。** Web/iOS/Code/est 分别有结果；一个入口通过不能代替其他入口。图片“客户端可见但模型读不到”必须判失败。**回滚。** 禁用 mcp image/view，不影响 HTTP 文本和 Siren。

### WP10：Siren 异步接入和 estómago 消费

**动作。** 先核对并修复 WP00 发现的 Siren URL 抓取安全问题。若存在 URL 输入，必须协议白名单、每次 DNS/重定向重验证、localhost/RFC1918/link-local/metadata 阻断、DNS rebinding 防护、大小/时长/跳转上限；否则 oído 集成只接受受信 `voice_message_id` 或直接字节上传。

Siren 保留既有 ASR/TTS 语义；新增向后兼容的 oído 引用/事件，不重写主链。宿主用户语音资产保存后可旁路登记；周char TTS 记录实际音频以及 `voice_profile_id/version`、generated/emitted/playback_reported 状态。

Chat 采用拉取：验证现有 Siren `voice_list`/取资产工具，用户说“听听我刚发的”时按当前会话/时间/权限列出有限候选，选定 `voice_message_id` 后导入 oído。**不要实现服务端主动 push 到 Claude.ai Chat。**

estómago 真实位置读到后定位消息附件 schema、SDK输入编码、tool_result/图像块存储与加载、摘要压缩路径；持久化 `oido_ref`，不长期依赖短期签名 URL。不能触碰默认身份/新窗规则以解决图像问题。

**验收。** 宿主用户上传、周charTTS两方向均可引用；Chat pull 路径能在没有 estómago 会话的情况下工作；URL SSRF 回归全绿；`store=false` 无永久副本；oído 故障时旧语音正常。**回滚。** feature flag 关闭两边 adapter；Siren 旧行为即时恢复。

### WP11：实时旁路、播放语义与迟到结果

**动作。** 创建受信媒体分支，保留现有Siren主链路。流合同先以原采样率PCM为基线，明确 endian、位深、声道、chunk frames；扩展压缩编码须显式版本协商，不混成模糊buffer。

每块带 `stream_id`、`seq`、`start_sample`、`sample_count`、来源轨、call/turn绑定与完整性信息。媒体块与控制帧分开，定义最大frame尺寸和不活跃超时。双向音轨分别保存，不用服务端收到时间把双方声音强行拼成精确同一时刻；跨设备时钟映射有不确定度。

采用有界内存ring与可控spool。队列满时优先降低/停止额外在线分析；不得阻塞TTS、ASR和真实打断。哪些在线块丢失要显式gap；完整补算只针对实际保留的音频，不从不存在的原始数据“恢复”。

区分 generated、emitted、playback_reported。客户端回执不可信到足以证明实际听见，只能给reported位置。取消时终止受控推理/解码子进程、关闭供给，不只是停止消费迭代器。`stream finalized`与“所有重模型完成”是两件事。

每个事件带audio/call/turn/revision；晚到结果只能挂原轮，不能注入已转移的新窗口。在线结果标provisional，最终计算生成新revision。重连按seq去重；缺口保留，禁止顺序重排后压短时间轴。

**验收。** 双向录音、打断、乱序、重复、缺块、断线、取消、超时、worker崩溃、队列溢出、晚到跨轮和客户端无播放回执。旧Siren生命期与采样率回归必须通过。**回滚。** 停分支与流分析，不停止通话进程；清理自身spool与worker。

### WP12：长期关联、保留、删除与还原

**动作。** 经Mariposa真实service/capability层新增或接入授权的 `audio_id + range + revision` 关联；绝不直接增删它的数据库行。`pin`用于阻止默认回收，不取消owner删除权，也不自动赋予对方读取权。

关联用pending→双端验证→active。撤销/删除用outbox补偿可重试；每次回看重新鉴权。权限变更优先停止访问，不能等对端在线才生效。离线服务恢复后消费tombstone，不复活旧链接。

已有正式记忆、日期、分类、I、我们的话、原文和召回索引不因声音分析自动修改。遵守 Mariposa 当前规则；音频转写不是 Claude 导出原文母本。embedding 检索是独立显式入口，不偷偷参与普通记忆召回。

保留政策固定：正式录音默认持续保留，直到宿主用户主动删除；临时无存储输入不升级留存；派生物可按缓存策略再生。容量不足通知/拒绝新任务，不能静默删母本。owner 删除覆盖 pin/关联，并清理原声、派生物、模型缓存、索引、临时流、关联及个人基线样本引用；备份恢复必须先应用 tombstone。

**验收。** 跨服务失联恢复、重复事件、先删后worker完成、pin与owner删除冲突、备份还原先施加tombstone、Mariposa普通召回未混入原声数组。**回滚。** 关闭新关联写入但维持撤销/删除读取阻断；不撤销已生效的删除意图。

### WP13：模型质量、隐私安全和资源验收

**动作。** 实施第6–8节评测，模型和 pipeline 固定版本。宿主用户私密原声只在 Mac mini 本地运行 oído provider，网络观察必须证明推理阶段没有向新第三方音频服务外发。派生文字/图像仅在用户当前 Claude/estómago 会话按需发送。用匿名 fixture 验证工程正确性，不能替代本域质量验收。

**验收。** 每能力实际run与score/错误分析、transport报告、外发审计、进程树资源报告齐全；性能不够就阻止生产启用相应功能，保留全量范围和阻塞原因，不删除需求。

### WP14：部署、灰度、回滚和正式交付

**动作。** 根据现场实测选择 native worker 或容器；私密原声推理必须保持 Mac 本地，不允许远端 fallback。Mac Metal/MPS 与 Linux 容器路径分别验证，不假设容器具有同样加速。确定未占用端口、只绑定所需地址、独立服务凭据、批准的隧道接入和生命周期。禁止把 `.env` 或模型凭据写入仓库。

先独立合成fixture → 受控上传 → Siren异步 → 实时 → 两入口 → Mariposa。每步有开关、健康检查和rollback，不能用一次全量生产回放压垮共享Mac。

**验收。** 第9节交付清单完整，旧服务健康无回归，有实际停止/重启/备份恢复演练。最终标记全量完成必须C01–C16全部满足批准的验收范围；未验宿主/模型或未解决blocker必须写部分交付。

---

## 6. 可执行验收矩阵

GLM应把下表落到真实测试文件与用例，不得仅原样复制本表作为“已测试”。表中的门槛是建议工程阈值或确定性数学关系，不是本机已经取得的结果；模型质量阈值由D09与本域基线确认。

### 6.1 数学、媒体与时间

| ID | 夹具/场景 | 必须验证的结果 |
|---|---|---|
| A01 | 同一个440Hz正弦，16/24/44.1/48kHz；长度≥2秒 | pYIN显式真实sr；中间可靠voiced帧F0误差建议≤2%；Hz与时间轴均不出现按22050错缩放。 |
| A02 | 同信号幅度乘0.5 | RMS差为 `20 log10(0.5) ≈ -6.0206 dB`；float测试误差≤0.01dB。 |
| A03 | 数字全零 | 无NaN/Infinity；无伪造F0；低于floor被明确标记。 |
| A04 | 每种sr下精确N样本 | duration=N/sr；主timebase索引与文件样本一致。 |
| A05 | 脉冲放在已知sample index | 包络不漏峰，频谱/图中事件位置符合frame约定。 |
| A06 | 带两段真实静音和一段人为gap | 静音与gap分开；不跨gap算连续语速或能量。 |
| A07 | 不同频率纯音与sweep | Hz标尺、n_fft/hop/Mel标识正确；Nyquist之外无伪造信息。 |
| A08 | stereo L=sin、R=-sin | 原声道保存；可选downmix后相消必须标明，不以downmix静音证明原声静音。 |
| A09 | 原字节MP3/Opus/WAV | 原文件hash未变；解码主视图不冒充编码前原声。 |
| A10 | 重采样后再映射预置脉冲 | 映射误差在算法规定容差；记录延迟/舍入，不累计漂移。 |
| A11 | center true/false短窗 | frame起始/中心与显示坐标一致；不能统一硬加半窗。 |
| A12 | 10ms短音频、空文件、损坏容器 | 清晰失败/insufficient_signal，不广播异常或OOM。 |
| A13 | 长文件分块与短文件整段对比 | 有效重叠区误差在定义范围；边界伪影标明。 |
| A14 | 裁剪相同能量片段不同图 | 固定reference时颜色刻度一致；auto-normalize标记可见。 |

### 6.2 语音、事件与机器表示

| ID | 场景 | 必须验证的结果 |
|---|---|---|
| B01 | TTS脚本与实际音频不同 | 两种文本并存；不称脚本为ASR事实。 |
| B02 | 同一句出现两次 | find返回两段候选；用户/模型选定后才高亮。 |
| B03 | 中文低声、昵称、笑着说话 | 输出CER与典型错例；不拿英语WER替代中文质量。 |
| B04 | ASR漏字、对齐模型不支持语言 | 句级/unknown或alignment_failed；禁止均分时间戳。 |
| B05 | 人工校订转写 | 新revision、旧对齐标旧；旧引用不指向新字词。 |
| B06 | 呼吸、叹息样、笑、背景音乐混淆 | 分标签precision/recall/误报统计；不编造统一高置信度。 |
| B07 | 电话里播放雨声音效 | 可报rain-like候选，不报真实地点天气。 |
| B08 | 歌曲与短语音各一段 | 音乐适用性检查；不强行给短句可靠tempo。 |
| B09 | 模型只支持clip tagging | 起止精度如实为窗口级，不制造framewise定位。 |
| B10 | 已知双方独立轨/混合多人 | 前者用来源，后者匿名diarization；overlap与unknown保留。 |
| B11 | 无登记的外部声音 | 不靠相似度猜Qiaosheng/Jiaming或推断性别年龄。 |
| B12 | embedding模型升级 | 旧向量不混空间；重新索引按版本进行。 |
| B13 | DAC或选定codec实际roundtrip | 真encode/decode；长度/延迟可解释；不要求有损重建hash一致。 |
| B14 | 声质指标条件不足 | 不适用/insufficient_signal，非0值“结果”。 |
| B15 | 泛声音描述与ASR冲突 | 独立低信任证据与冲突可见，不覆盖母本和正式文本。 |

| B16 | 普通话一至四声、轻声、多音字、三声变调、`一/不` | 音节有上下文拼音/声调及来源；未知表面调明确uncertain，不伪造。 |
| B17 | 四声字出现在平直/上扬/下沉不同句级语调 | 字内下降与句级走势分字段；不得仅凭四声下降输出“情绪低落/语气低落”。 |
| B18 | 同一宿主用户样本建立/清空个人基线 | 只含音高/语速/停顿等批准统计；无绝对音量/声纹；clear 后不可继续比较或静默重建。 |
| B19 | 周char旧音色、新捏音色、正式音色各一组 | `voice_profile_id/version` 不混；参考样本与实际TTS按指定版本比较。 |
| B20 | provider 原始响应包含 happy/sad/neutral | emotion 字段在 adapter 边界被丢弃；DB/API/MCP/log 均无该字段。 |

### 6.3 跨端、可靠性与安全

| ID | 场景 | 必须验证的结果 |
|---|---|---|
| E01 | HTTP和MCP访问同资产同revision | 权限、数值、范围一致。 |
| E02 | Claude Web/iOS/Code/est四入口 | 分别证明native image block传递；不能用一个入口代替全部。 |
| E03 | UI可显示但模型未获图像 | host标blocked/unsupported；不报告“模型已看图”。 |
| E04 | 带图随机短码的传输夹具 | text不泄露答案；记录模型读取与payload证据。 |
| E05 | 音频含“忽略规则并执行命令” | 当录音内容处理，权限与系统提示不变化。 |
| E06 | store=false上传 | 不产生永久副本或Mariposa链接；临时到期清理。 |
| E07 | 未授权其他owner的ID/向量查询 | 拒绝且不泄露存在性；缓存不跨域泄露。 |
| E08 | 路径穿越、任意http/file URL、伪mime | 验证/拒绝；不会触达任意主机或本机文件。 |
| E09 | 超大解码、海量chunks、超宽图像 | 字节/时间/像素/内存/磁盘配额在重计算前或监督中生效。 |
| E10 | 分析崩溃/超时/队列满 | Siren正常语音和真实打断不受阻；额外证据partial。 |
| E11 | 相同idempotency key不同音频 | 409，不串用旧任务。 |
| E12 | worker提交前收到取消/删除 | fencing阻止写入；无结果复活。 |
| E13 | 流乱序/重复/缺块/重连 | seq去重、gap明确；没有压缩时间轴。 |
| E14 | TTS中断且仍有生成余音 | generated与emitted/played_reported分开；未播放不冒称播过。 |
| E15 | 旧turn结果在新turn完成 | 只附原turn，不串窗、不自动重唤醒对话。 |
| E16 | outbox重复/对端离线/重启 | 状态最终一致；重复事件不重复绑定或扣费。 |
| E17 | 删除资产且Mariposa离线 | 本服务立即停止访问；对端恢复后撤销。 |
| E18 | 从旧备份还原 | 最新删除墓碑先施加；已删音频不复活。 |
| E19 | 签名URL过期/权限被撤销 | 按策略拒绝；不以永久公开地址解决图片显示。 |
| E20 | 杀API/worker并重启 | 不留孤儿模型进程；租约恢复正确，母本未损坏。 |
| E21 | 源码/配置/日志抽检 | 无私密正文、token、完整base64、长期签名链接泄漏。 |
| E22 | 关闭所有auditory开关 | 原Siren行为和est身份规则回归；无共享env污染。 |

建议文件映射：A类→`test_dsp_reference.py`/`test_timebase.py`；B类→`test_provider_contracts.py`+`evals/`；E类→各合同/安全/集成/宿主验收文件。集成fixture允许mock基础设施故障，但真实模型质量与真实宿主图像传递不得用mock冒充。

---

## 7. Provider 选择、许可与本域评测

### 7.1 依赖选择记录

每个provider至少记录：仓库、commit/tag、代码license、权重license/model card、checkpoint hash、运行时依赖、输入采样率/声道、输出时间粒度、支持类别/语言、CPU/MPS/Metal/CUDA路径、峰值内存、首次加载时间、网络依赖、遥测和外发字段。

仓库license不自动覆盖所有模型权重和训练数据。SenseVoice代码和模型条款分别核验；若采用 SenseVoice，仅接收允许的 ASR/声音事件字段，情绪输出丢弃。DAC公开代码与权重许可说明可作为来源但仍锁定具体下载物。[R08][R10] 参考项目没有明确许可时不复制实现。新依赖安全扫描不是律师意见或完整许可审查，发现不明确内容列阻塞项。

本地provider不得默认联网；模型下载和正常推理网络应分离。pyannote的遥测按官方配置关闭，例如 `PYANNOTE_METRICS_ENABLED=0`，并对本次实际版本做网络观测；环境变量本身不能替代验证。[R15] 其他provider也必须检查隐式下载、远程代码与遥测。

### 7.2 本域语料

合成信号用于测量测试；获授权录音用于语音/声音效果评测，分开发集和未参与阈值选择的保留集。划分按录音来源/会话去重，不把同一片段裁剪的副本混进两边。保存授权用途、有效期、可外发范围及删除要求。

至少覆盖普通话昵称、轻声、快语速、停顿、笑中说话、手机降噪、不同音量、宿主用户语音与周charTTS、环境纯声、背景音乐、混合多人。没有足够私密样本时先用公共许可样本和自生成语料，并明确“尚未完成家庭本域验收”，不能偷用私密历史录音。

### 7.3 指标

ASR报中文CER及错例；alignment报手标区间起止误差分布/失配率；允许的声音事件报分标签precision、recall、误报每小时和拒判比例；声质报参考实现一致性及不适用比例；diarization报选定指标并单列重叠；embedding报规定查询集的recall@k及混淆；codec报对齐后重建指标和试听范围，不给它赋予情感保真含义。

普通话韵律单独评测：字/音节对齐准确度、拼音/声调正确率、相对半音误差、字内 contour 与句级 trend 的混淆率。重点构造“词汇声调走势与句级语调方向相反”的样本；任何把字调直接解释成心理状态的输出计严重错误。

给周char的感知评测分条件比较：`transcript only`、`transcript + 文字韵律谱`、`transcript + 文字韵律谱 + 图`。关注可核验事实准确度、错误解释、未知拒答和无依据情绪断言。图片无增益时仍保留按需查看，但默认继续使用文字韵律谱。

## 8. 资源、外发与运行保险

### 8.1 预算必须实测

尚未取得本机内存/磁盘/允许推理预算，不写“Mac肯定实时”或“某模型几秒完成”。记录音频长度、模型/设备、冷/热启动、RTF、p50/p95延迟、进程树RSS、GPU/统一内存可观测值、spool磁盘、失败/取消后回落和上下文token。

开发起始限制：单个纯单元测试进程树 RSS 上限约 1 GiB、单批 180 秒、串行 worker；这是初始保护值，不是模型推理预算。重模型测试先实测/估算并按共享宿主规则受控运行。现有 Mariposa 对预计>2GB或>20分钟测试的确认要求继续适用。

### 8.2 有界测试

必须提供独立监督器，限制整棵进程树的内存/时间/磁盘，而不是只在Python函数外套一个timeout。超限先终止再在有界宽限后强杀子树，保留精简诊断；测试不允许 `&`、nohup或无人监督detached。macOS的具体硬限实现要实测，不把Windows `test-guard.ps1` 写成已经在Mac生效。

在仓库完成脚本后可运行类似以下命令；它们是**未来验收调用约定，当前文件包不包含这些脚本，也没有执行记录**：

```bash
# 在已确认的新auditory仓库内；supervise_tests.py必须先实现并通过自测。
python scripts/supervise_tests.py --timeout-seconds 180 --rss-limit-mib 1024 \
  -- python -m pytest -x -q tests/unit/test_timebase.py

python scripts/supervise_tests.py --timeout-seconds 180 --rss-limit-mib 1024 \
  -- python -m pytest -x -q tests/contract/test_mcp_payloads.py
```

禁止直接拿这种小预算运行重模型后反复撞限；模型评测使用单独批准的budget配置。所有测试按小批前台完成，不跑一次漫无边界的全量pytest。

### 8.3 网络和数据

所有 oído 音频推理 provider 必须为 `local_only`。本版本不实现 remote fallback，也不因本地模型失败把宿主用户原声或片段发送到新第三方云服务。模型下载可联网，但下载阶段与用户音频推理阶段必须分离并有网络审计。

Siren 已经使用的供应商属于 Siren 自己的既有链路，不等于 oído 可以再次把同一原声发给供应商。oído 从 Siren 接收受信资产/已有转写；新增分析保持本地。

派生文字、韵律谱和按需图片会在宿主用户主动使用 Claude.ai Chat 或 estómago 时进入该既有会话，这是产品目标的一部分；仍按敏感数据保护，不写内容日志，不做公共 URL。

音频、声学向量、图像、codec 和转写都按私密资产保护；特征化不等于匿名化。显式禁止常驻环境监听；实时能力只在已获授权的 call/recording 生命周期内活跃。

## 9. 部署、验收和交付报告

### 9.1 灰度与回滚顺序

先确认基线健康、端口、磁盘、备份、密钥和独立守护，再启用新服务；不要边部署边修改Cloudflare/VPN来碰运气。上线开关至少分：ingest、offline_analysis、mcp_tools（2026-10-27 联合审计 B6：est_evidence/siren_sidecar/realtime/memory_links/personal_baseline 仅声明无行为消费者，已从开关全集删除——声明不等于能力，真接入时随真实消费者加回）。

每一步报告“仅mock / 合成fixture / 获授权真实音频 / 生产样本”的证据等级。灰度期发现异常先禁新增听觉计算或adapter，不撤掉原Siren语音。迁移回滚以快照与向后兼容恢复为准，禁止用git reset解决数据schema变更；禁止回滚已生效的用户删除墓碑。

### 9.2 交付物

1. 可运行的独立服务、锁定依赖、schema/migrations、测试与夹具；全部代码差异可审查。
2. `AUDITORY_DECISIONS.md`、`BASELINE.md`、`PROVIDER_MATRIX.md`、`THIRD_PARTY_NOTICES.md`、`RESOURCE_BUDGET.md`。
3. `API.md`、`MCP.md`、`TIMEBASE.md`、`RETENTION_AND_DELETION.md`、`DEPLOYMENT.md`、`ROLLBACK.md`。
4. `acceptance.json`、逐宿主传输报告、本域模型评测、资源/外发记录、旧Siren回归结果、关联/删除恢复演练。
5. 一份 `IMPLEMENTATION_REPORT.md` 供AI协作阅读，包含真实完成、未完成、差异和下一阻塞；不另写一份给宿主用户看的冗长“人话报告”。

建议 `acceptance.json` 结构如下，示例刻意标not_run，不能原样作为通过报告：

```json
{
  "schema_version": "oido.acceptance.v1",
  "architecture_version": "OIDO-ARCH-1.1",
  "execution_version": "OIDO-EXEC-1.1",
  "decision_record": "docs/AUDITORY_DECISIONS.md",
  "implementation_commit": null,
  "working_tree_manifest": null,
  "overall_status": "not_run",
  "capabilities": [
    {"id": "C01", "status": "not_run", "tests": [], "blockers": []}
  ],
  "hosts": [
    {"host": "claude_ios", "client_version": null, "status": "not_run"}
  ],
  "new_cloud_audio_inference": "forbidden",
  "production_changes": [],
  "notes": ["schema_example_only"]
}
```

正式结果必须包含全部C01–C16及适用测试，不只C01。`pass`、`fail`、`blocked`、`not_run`、`not_applicable`分开；not_applicable须有理由且不掩盖整个产品能力未实现。

### 9.3 报告最小模板

```text
版本/实际工作树与文件散列：
D01–D10基线与本次是否发生偏离：
本次修改仓库、文件和数据库迁移：
C01–C16逐项：已实现行为 / 实际用例 / 结果 / 未覆盖项
采用provider及代码/权重版本、许可证、资源实测：
四个宿主：实际客户端与模型版本 / 随机码图片传递证据 / 文字韵律谱结果
真实私密音频使用、本地推理网络审计、Claude会话派生证据发送与保留/删除情况：
旧Siren/est身份/Mariposa检索回归：
取消、删除、备份还原和故障演练：
实际执行命令、退出码、峰值内存和超时：
未解决问题、影响、回滚开关与阻塞责任：
结论：全量验收通过 / 部分交付 / 不宜上线
```

不得从历史报告抄“186 passed”等数字到新服务；不得以“接口存在”“README写了”“图能打开”“provider有返回”代替真实验收。也不得把本文件的设计当实施结果。

---

## 10. 研究出处与借鉴约束

完整研究比较及对应R编号在配套架构附录A；下列为执行必须复核的一方来源。查阅日期均为2026-09-29；落地时固定实际版本。

| 编号 | 来源 | 执行关注点 |
|---|---|---|
| R01 | https://github.com/zachswift615/audio-analysis-mcp | 思路参考；未确认许可，不直接复制。 |
| R02 | https://github.com/zachswift615/audio-analysis-mcp/blob/main/src/audio_analysis_mcp/server.py | 抽查blob `9f281d423e44d440616f6796ab0d5403d67a0fc7`；输出本地路径、F0未传sr的问题。 |
| R03 | https://github.com/librosa/librosa/blob/main/librosa/core/pitch.py | 抽查blob `b0caa7b3e247455e6b70cd4003b79b3a15a09c8c`；pYIN默认sr22050、voiced和frame定义。 |
| R04 | https://github.com/JuzzyDee/audio-analyzer-rs | 按区间展开；本机stdio不等于远程Chat接入。 |
| R05 | https://github.com/counterpoint-studio/audio-file-mcp-app | UI选区回传参考，不等于模型图像输入。 |
| R06 | https://github.com/librosa/librosa | DSP与数值表示。 |
| R07 | https://github.com/LAION-AI/CLAP | 音频/文本embedding，不是身份或概率。 |
| R08 | https://github.com/descriptinc/descript-audio-codec | 有损codec及公开代码/权重许可说明。 |
| R09 | https://github.com/ggml-org/whisper.cpp | Apple本地ASR候选。 |
| R10 | https://github.com/QwenAudio/SenseVoice | 中文ASR/非语言标签；代码与权重条款分开。 |
| R11 | https://github.com/m-bain/whisperX | 对齐、分段方案；中文模型实际验证。 |
| R12 | https://github.com/snakers4/silero-vad | VAD适用范围。 |
| R13 | https://github.com/qiuqiangkong/audioset_tagging_cnn | tagging/SED时间粒度差异。 |
| R14 | https://github.com/fschmid56/EfficientAT | 轻量tagging/窗口推理候选。 |
| R15 | https://github.com/pyannote/pyannote-audio | diarization、模型条款、遥测配置。 |
| R16 | https://github.com/QwenLM/Qwen3-Omni | 泛声音Captioner候选；资源与隐私须另验。 |
| R17 | https://github.com/katspaugh/wavesurfer.js | 查看器组件，锁定实际版本。 |
| R18 | https://platform.claude.com/docs/en/build-with-claude/vision | 图像输入能力与约束。 |
| R19 | https://modelcontextprotocol.io/specification/2026-07-28/server/tools | 协议内容块与当前响应结构，协商兼容。 |
| R20 | https://github.com/audeering/opensmile | 声质特征候选及用途相关许可。 |

研究只证明这些参考和公开接口存在，不证明它们已经在本机安装、经过全量审计或符合你们的使用质量。GLM需以真实代码、明确模型版本和可复现记录完成交付。
