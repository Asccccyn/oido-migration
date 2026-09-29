# WP00 只读盘点基线

日期：2026-09-29 · 执行：GLM（程知行）· 依据：OIDO-EXEC-1.1 §5 WP00
性质：**只读盘点报告，不含任何生产变更。**

## 1. 工程包完整性

`docs/pack/` 内四份文件的 SHA-256 与包内 `MANIFEST.json` 完全一致（2026-09-29 校验）：

- `AUDITORY_DECISIONS.md` `451621f6…faf`
- `Oido_Architecture_owner_v1.1.md` `3afc45c3…d359`
- `Oido_Execution_GLM_v1.1.md` `e0727b2a…036c`
- `README_HANDOFF.md` `0f43b6b0…335`

D01–D10 与 `docs/pack/AUDITORY_DECISIONS.md` 一致，无偏离。

## 2. 一方现场（重录，非沿用包内记录）

| 对象 | 本次实测 | 与包内记录的差异 |
|---|---|---|
| Siren | `/Users/owner/Projects/siren`，HEAD `03e45f275c34b4ff35b65da5d080fac411f2c0f3`，未提交条目 65 | 与包内 L01 一致 |
| Mariposa | `/Users/owner/Projects/mariposa`，HEAD `6df6dfe57933b5f95e78b306b3bd74cc96a033fa`，未提交 2 条（`.venv-old-20260929/`、`D:\mariposa\runtime\models/` 杂项） | **HEAD 已前进**（包内记录 `b299273…`；现为 v1.7 整改分支 `6df6dfe`）。印证"开工必须重录工作树" |
| estómago | `/Users/owner/Projects/estomago` **存在**，HEAD `3ceec9ef245c66231f29503d5c38e67a7a304de0`，v0.2.2，`@anthropic-ai/claude-agent-sdk 0.3.283`，node ≥22.18 | 包内 L07 称"路径不可达"系其工具 allowed-roots 限制，非目录不存在。本机 node v22.23.3 满足 |

### 2.1 Siren 合同核验（抽样，未改动）

- `packages/contracts/src/voice.ts`：`SIREN_EMOTIONS` 枚举（neutral/warm/happy/sad/…）是**生成意图参数**；`REALTIME_INPUT_SAMPLE_RATE = 16000`。→ oído 侧永不把该字段当声学证据（对应 D02/B01）。
- `apps/server/src/routes/voice-message.ts` `serializeAsset`：含 `direction/voice_profile/sample_rate/text/tts_script/transcript/conversation_id/message_id` 等 → D07 拉取路径的真实锚点。
- MCP 工具 `voice_list`（`apps/server/src/mcp/tools/list-voices.ts`）：入参 `conversation_id / message_id / from / to / limit`，返回 `serializeAsset` 条目。**存在且可用**，D07 不需要造新列表工具。
- `packages/core-bridge/src/types.ts` `CoreTurnInput`：仍无听觉证据字段（与包内 L04 一致），WP11 扩展须向后兼容。

## 3. Siren URL 音频抓取 SSRF 审计（P0 项）

**唯一 URL 入口**：MCP 工具 `voice_transcribe`（`apps/server/src/mcp/tools/transcribe.ts`）的 `audio_url` 参数 → `downloadWithLimits()`（`apps/server/src/mcp/tools/audio-fetch.ts`）。限制：25 MiB / 30 s / 最多 3 跳重定向。

已到位的防护（`audio-fetch.ts`，注释标注审计项 F03/F21）：

- scheme 白名单（仅 http/https）；
- 字面 IP 判定：IPv4 阻断 `0/8、10/8、100.64/10、127/8、169.254/16、172.16/12、192.168/16、198.18/15、224/3`；IPv6 阻断 `::、::1、fc00::/7、fe80::/10、ff00::/8、2001:db8::/32`，并对 IPv4-mapped `::ffff:0:0/96` 与 NAT64 `64:ff9b::/96` 按内嵌 v4 判定；
- 域名走 `dns.lookup(all:true)`，**任一**解析结果落禁止段即整体拒绝；
- 重定向 `redirect:'manual'`，每一跳重新执行完整出站校验，上限 3 跳；
- 大小双保险：Content-Length 声明即拒绝 + 流式累计越界即取消；
- 每跳 `AbortSignal.timeout`。

**结论：基础 SSRF 面已加固；无"可达内网"实锤。** 按 OIDO-ARCH §10.1 的标准仍有缺口：

| 编号 | 缺口 | 等级 | 说明 | 修复归属 |
|---|---|---|---|---|
| S-GAP-1 | **DNS rebinding TOCTOU** | P1 | `assertPublicHttpUrl` 用 `lookup()` 校验后，`fetch()` 自行发起**第二次独立解析**；攻击者控制权威 DNS 可让第二次解析返回私网地址。未钉住已验证 IP（无自定义 dispatcher/连接绑定）。 | Siren（WP10 接入前修复；修复须走 Siren 自己的未提交工作保护流程，本仓不代改） |
| S-GAP-2 | 总时长无上限 | P3 | 每跳 30 s 超时，但 4 次（初始+3 跳）串行可达 ~120 s；无整体 deadline。 | 同上，可与 S-GAP-1 一并处理 |

oído 侧对应决定（已在本仓实施）：`oido_analyze` 等工具**不接受任何 URL**，只接受 `audio_id` / 受信 Siren 资产引用 / 直接上传字节。URL 抓取永远只留在 Siren 边界。

## 4. 本机环境

- Apple M6，24 GB 内存，macOS 27.0（26A428）
- Python：系统 3.9.6 过旧；采用 Homebrew **python3.12.14**（`/opt/homebrew/bin/python3.12`）
- node v22.23.3（estómago viewer 未来可用）
- `uv` **未安装**：按 EXEC §1"只选一种管理方式"，采用 **venv + pip + requirements.lock（freeze 全量锁定）**
- `ffmpeg` **未安装**：WP02 媒体探测/解码子进程需要；届时单独提请安装批准（或评估纯 Python 解码范围：wav/flac/ogg 走 libsndfile，mp3/m4a 明确 `unsupported`）。**当前为 WP02 blocker，如实登记，不假装可用。**
- `~/Projects/oido`、`~/Data/oido` 均无冲突（新建）

## 5. 依赖锁定（首次）

`.venv`（python3.12.14）+ `requirements.lock`（pip freeze 全量）。基础依赖：pytest、jsonschema、numpy、scipy、librosa、soundfile、fastapi、uvicorn、httpx。重型模型 provider（PANNs/CLAP/DAC/whisper.cpp 等）**全部未安装**，capability 状态如实报 `not_configured`。

## 6. WP00 验收对照

- [x] D01–D10 与文件一致
- [x] 现场信息有真实命令证据（本文件 §1–§4，均为只读命令输出）
- [x] URL 抓取攻击面有代码位置（§3 精确到文件与函数）与测试计划（S-GAP 修复 + oído 侧"工具不收 URL"的契约测试）
- [x] 未把只读盘点写成实现完成；回滚：无生产变更
