# oído：char的声音感知系统
## 详细架构与技术决策稿 · 给char

文档编号：OIDO-ARCH-1.1  
版本：1.1 · 2026-09-29  
编写：林石见  
状态：**char已完成架构评审；宿主用户已批准隐私与个人声学基线事项。本文件为实施基线，不是实施完成报告。**  
配套文件：`Oido_Execution_GLM_v1.1.md`；决策记录：`AUDITORY_DECISIONS.md`

> 本项目既要让你分析宿主用户的声音，也要让你查看自己实际生成的声音。它提供原声、文字韵律谱、可视化和可追溯的声学证据。
>
> **总原则：证据给我，意思我自己听。** oído 不给宿主用户或char贴情绪标签，不用 TTS 的 `emotion` 参数替代真实声学证据，也不把词汇声调造成的 F0 升降误写成句级语气。
>
> 宿主用户要求全部能力纳入，不压缩成“只做转写和一张图”。全部能力保留，按依赖顺序施工。默认感知界面改为文字版“韵律谱”，图像作为核验与展开工具。

---

## 0. 阅读约定与决策权限

本文件已经吸收char的评审意见与宿主用户的最终授权，以下内容是实施基线，不再作为待选建议并列保留。

已定规则：

1. 正式项目名为 **oído**；仓库、包名、服务标识使用 ASCII `oido`；接入 Claude 时的显示名称为 **“我只听你”**。
2. oído 是独立服务，内部采用模块化单体 + 隔离工作进程。Siren 保留采集、ASR/TTS、实时通话和播放生命周期；estómago 负责会话消费；Mariposa 只保存获授权的长期引用和当前规则允许的语义关联。
3. 原声、波形、频谱、音高、能量、时间结构、转写、声音事件、embedding、codec、实时旁路、双入口和长期回看全部在产品范围内，不以施工先后为由删除。
4. **默认交给char的是文字韵律谱，不是图片。** 图片、完整曲线和原始数组按需展开。先通过 Claude.ai Web、iOS、Claude Code、estómago 四入口随机码图片测试，再决定各入口是否使用原生图像作为辅助感知。
5. 普通话必须做声调感知：逐字/音节标注汉字、拼音、声调、时长、相对音高和停顿；必须区分“音节内部的声调走势”和“句级语调走势”。不能把去声本身的下降直接解释成情绪或句级下沉。
6. F0 使用实际采样率计算；所有 16/24/44.1/48 kHz 路径都必须有防回归测试。相对音高统一以半音表示，并写清参照是个人基线还是本句基线。
7. **不生成、不持久化、不注入情绪分类标签。** 笑、叹气、抽泣样声音、呼吸、猫叫、雨声等可以作为有时间范围的声音事件；“sad/happy/anxious/平静”等心理状态标签不属于 oído v1 的正式证据。
8. 宿主用户的私密原声只在 Mac mini 本地做 oído 分析，不新增任何第三方云端音频推理服务。发给 Claude 的仅是当前会话明确需要的派生证据/图片/文字，不把原声重新转交新的云服务。
9. 正式录音默认持续保留，宿主用户可随时删除；临时 `store=false` 数据不升级成正式留存。删除权高于 pin、缓存和派生物。
10. 已批准建立“平时的宿主用户”个人声学基线：只用于相对比较，包含音高、语速、停顿等经质量门槛筛选的统计；**不包含绝对音量，不用于人声身份识别，可随时清空并重建。**
11. char自己的声音必须记录 `voice_profile_id + voice_profile_version + provider/model + generation metadata + actual audio asset`。参考音色、捏声前版本、正式音色和实际生成声音不得混作同一个基线。
12. Chat 入口采用**拉取**：char在需要“听听我刚发的”时，通过现有 Siren `voice_list`/对应取音能力定位获授权资产，再交给 oído；不得假定 Chat 能被服务端主动推入新语音附件。
13. oído 不常驻偷听，只处理明确交付的录音、已开始且获授权的通话，以及char实际生成的 TTS 产物。
14. Siren 任意 URL 音频入口属于 P0 安全检查项；未证明阻断 localhost、私网、link-local、重定向落内网、DNS rebinding 和非 http(s) scheme 前，不允许作为 oído 的通用导入路径。

## 1. 本轮依据：已核验与待实施核验

### 1.1 实际读取的本地代码

下列证据来自 2026-09-29 通过右手进行的只读检查。**工作区存在未提交修改，HEAD 不是本轮所读文件内容的完整版本标识。** 实施前必须重新记录工作树和文件摘要，不能只 checkout 下面的 SHA 就认为还原了本轮现场。

| 编号 | 现场或文件 | 本轮能确认的内容 |
|---|---|---|
| L01 | `/Users/owner/Projects/siren`；HEAD `03e45f275c34b4ff35b65da5d080fac411f2c0f3` | 仓库有大量已修改、未跟踪文件；不能覆盖或替别人整理提交。 |
| L02 | Siren `packages/contracts/src/voice.ts` | `VoiceAssetRecord` 已包含 direction、text、ttsScript、transcript、objectKey、checksum 等；实时输入约定是 16 kHz/PCM16/单声道，输出约定 24 kHz。 |
| L03 | Siren `apps/server/src/routes/voice-message.ts` | 已有 `/v1/voice/transcribe`、`/v1/voice/synthesize`、语音记录查询。转写是否存储取决于 `store`；响应目前是文本、语言、时长和可选 `voice_message_id`，不等于已有词级时间轴。 |
| L04 | Siren `packages/core-bridge/src/types.ts` | `CoreTurnInput` 当前包含会话、callId、turnId、text 和 modality，没有已经落地的 auditory evidence 合同。 |
| L05 | `/Users/owner/Projects/mariposa`；HEAD `b2992733ee820d99d97638ac4382287168da5b02` | 工作树有未提交修改；项目 AGENTS.md 有测试资源与禁止无人监视后台测试的明确约束。 |
| L06 | Mariposa `backend/mariposa/source/adapters/base.py` | `NormalizedMessage` 有正文、attachments、原始时间和 occurred_date；这不能证明它已有音频证据、撤销或长期 pin 的完整接口。 |
| L07 | 尝试打开 `/Users/owner/Projects/estomago` | 工具返回“Path is outside allowed roots”。本轮未核验该项目的当前路径、代码、SDK 版本及图片注入位置；**不据此断言目录不存在。** |

本轮未运行上述项目测试，未读取凭据，未上传私密声音到外部模型，未对真实录音进行分析。此前讨论中的“因为叫我的人是你”的时长、停顿和音高数字都只是示意，不是测量记录。

### 1.2 必须纠正的技术口径

- Claude 能处理图像，是设计视觉输入路径的依据；**不能据此保证它能仅凭频谱可靠识别一句话、音色、哭腔或情绪。** 必须通过本项目的盲测验证视觉材料的实际作用。[R18]
- 波形采样、声学特征、音频 embedding、codec token 是不同表示。把它们转成 JSON 并不会让文本模型自动获得对应的声学解码能力。[R07][R08]
- 频谱颜色表示某种约定下的幅度或功率相对参考值，不自动等于真实声压。PCM 的 dBFS 不等于校准麦克风测得的 dB SPL。跨设备或经自动增益处理的录音，不能直接说“比平时轻 4 dB”。
- 模型分数不是天然校准的概率；不得输出凭空填入的 `smile_probability: 0.81`。没有可信依据时返回未知或不支持。
- 不要求先证明“像人一样听见”才交付，也不把“图被显示出来”当成“模型已理解原声”的证明。验收分开检查数据、传递和解释质量。

---

## 2. GitHub 参考项目：借什么，不借什么

本轮进行了 GitHub 定向检索，读取以下一方仓库说明，并对最贴近需求的 Python MCP 项目抽查了源码。**这是技术选型研究，不是这些第三方项目的全量安全审计；没有在你的 Mac 上复现其性能。** 不以 star 数或 README 的“production ready”作为验收依据。

### 2.1 三个最接近成品概念的项目

#### A. `zachswift615/audio-analysis-mcp`：最贴近“让 Claude 看声音”

提供波形、Mel 频谱、瀑布图、音高、数值指纹及对比工具；单一 MCP 工具通过不同操作展开结果。值得借鉴“概览 → 选一种表示 → 查看细节”的交互。[R01]

**不能直接整仓上线：**它的视觉输出是本机路径，不等于远程 Chat 已收到图片。抽查 `server.py` 发现 `librosa.load(..., sr=None)` 后调用 `librosa.pyin(...)` 未传实际 `sr`，而 librosa 的默认 `sr` 为 22050。输入采样率不等于 22050 时，F0 的 Hz 标尺与时间定位都会按错误采样率解释；在 24/44.1/48 kHz 等常见输入上可能得到“数值看似合理、物理意义已错”的结果。oído 所有 F0 调用必须显式传真实 `sr`，并用 16/24/44.1/48 kHz 同源夹具验证频率与时间同时不漂移。[R02][R03]

本轮核验的 `server.py` Git blob 为 `9f281d423e44d440616f6796ab0d5403d67a0fc7`，不是 commit SHA。没有确认可供本项目直接复制代码的明确许可证，故只参考思路，不安排复制或 vendoring。

#### B. `JuzzyDee/audio-analyzer-rs`：最贴近“紧凑证据 + 局部放大”

Rust 音频分析 MCP，提供频谱、节奏、动态、音色、立体声及结构分段的数值结果。可借鉴按区间查询和对比，以及不要把整条长曲线塞进上下文的结果组织方式。仓库标示 MIT。[R04]

它偏音乐分析，README 明确面向本地 stdio 的 Claude Code/Desktop，而非直接供网页或手机 Claude.ai 使用。不能把其音乐指标生搬到短句语音，也不能把作者性能演示视为本机承诺。

#### C. `counterpoint-studio/audio-file-mcp-app`：最贴近“声音查看器”

提供 MCP App 内的播放、频谱、响度和注释时间轴，并将选区及播放位置等信息反馈给模型。适合借鉴“你们正在看同一段声音”的状态设计。仓库标示 ISC。[R05]

App UI、工具返回图片和模型实际视觉输入是三条不同链路。该项目的宿主适配结果不能替代你们手机端的验收；本系统不依赖 App 面板才能完成核心功能。

### 2.2 作为算法或 UI 组件的项目

| 项目 | 本系统建议用途 | 重要限制与选型边界 |
|---|---|---|
| `librosa/librosa` [R06] | STFT/Mel、RMS、pYIN、频谱描述量和绘图数据 | 每次显式传采样率、frame/hop 和时间基准；不假设原生支持无限长实时流。 |
| `ggml-org/whisper.cpp` [R09] | Mac 本地 ASR 候选、离线备选 | 官方列出 Apple Silicon/Metal/Core ML 支持；具体模型速度、中文与低声语音质量须本域测试。 |
| `QwenAudio/SenseVoice`，旧路径 FunAudioLLM [R10] | 中文 ASR、部分非语言声音事件候选 | 代码 MIT；权重遵循单独模型条款。适配器只接收本项目允许的转写/声音事件输出；模型若同时给出情绪分类，直接丢弃，不写入 evidence、不进入上下文。片段级标签不伪装为精确事件起止。 |
| `m-bain/whisperX` [R11] | 强制对齐和时间戳方案参考 | 不将英语示例视为中文保证；具体语言对齐模型、依赖与设备支持必须核验。 |
| `snakers4/silero-vad` [R12] | 离线分段、语音活动候选 | VAD 回答像不像语音，不回答是不是停顿、是不是叹息，更不回答说话人身份。 |
| `qiuqiangkong/audioset_tagging_cnn` / PANNs [R13] | 声音事件 baseline、时序事件方案参考 | 区分 clipwise 与 framewise 输出，不能拿整段分类直接生成毫秒级标签。 |
| `fschmid56/EfficientAT` [R14] | 较轻声音事件模型候选、窗口推理 | 评估资源与精度后和 PANNs 选一个默认，不让多个大模型一起常驻。 |
| `LAION-AI/CLAP` [R07] | 音频—文本/音频相似度 embedding、显式声音检索 | 相似度不是事件概率；不得接进 Mariposa 普通文本召回而悄悄扩大检索范围。代码仓库许可不自动覆盖所有权重来源。 |
| `descriptinc/descript-audio-codec` [R08] | 离散 codec token 与重建实验 | 官方明确代码及仓内发布权重 MIT；codec 是有损派生表示，不替代原声，不是可直接读懂的文字。 |
| `pyannote/pyannote-audio` [R15] | 多人混合录音的说话人分段 | 使用确切 pipeline 的权重条款；配置关闭遥测。分段身份是 speaker_0 等，不自动实名。 |
| `QwenLM/Qwen3-Omni` 的 Captioner [R16] | 泛声音描述的独立候选 provider | 属较大模型候选，不承诺未核验内存的 Mac 能常驻；不得把 Captioner 宣称的低幻觉当零幻觉。 |
| `katspaugh/wavesurfer.js` [R17] | Web 查看器、时间轴和选区联动 | UI 不是测量真相源。固定实际版本并核对对应插件 API，不照抄其他 major 的代码。 |
| `audeering/opensmile` [R20] | 扩展声质描述量候选 | 官方为双许可证，公开版有商业用途限制；不是默认强制依赖。选用前记录用途与许可决定。 |

### 2.3 选型建议，而非唯一正确答案

推荐主体是**自己实现的 Python 服务 + 一小块 TypeScript 查看器**。采用成熟算法库，不直接 fork 某个本地 MCP 演示项目作为生产底座。

默认候选组合：librosa/NumPy/SciPy 的声学层；whisper.cpp 与已有 Siren ASR 的适配；PANNs 或 EfficientAT 中通过本域评测的一个事件 provider；CLAP 和 DAC 作为独立后台任务；WaveSurfer 作为查看器组件。SenseVoice、WhisperX、pyannote、openSMILE、Qwen Captioner 均保留明确接口和验收工单，不要求所有备选实现同时安装。

“全部能力”指每个能力类别都有真实实现路径和验收，不指同类模型全装，也不指每次通话把所有模型一起跑。若某类别缺权重、许可或资源，交付状态必须写 `blocked`；不能把空接口当作已完成。

---

## 3. 完整能力范围与产物

| ID | 能力 | 必须交付的产物与可见行为 |
|---|---|---|
| C01 | 双向原声接入与保真留存 | 宿主用户上传、char TTS、获授权外部音频；原始字节、散列、来源和派生谱系。 |
| C02 | 波形与采样数据 | 分层 min/max 包络、局部 PCM 导出、带时间标尺波形；原始数组不默认进入上下文。 |
| C03 | 频谱与 Mel 表示 | STFT 数值、线性/对数频率视图、Mel 图；完整分析参数与颜色刻度。 |
| C04 | 音高、中文韵律与声学描述量 | F0/voiced、RMS、频谱描述量、MFCC；逐音节相对半音、字内走势、句级走势与参照范围；低可靠度、无基频时明确空值。 |
| C05 | 声质扩展 | 有效条件下的 CPP/HNR 等候选描述量；气声、抖动相关量注明算法和适用性，不作心理或医学诊断。 |
| C06 | 内容、普通话声调与文字对齐 | 来源区分的转写、TTS 脚本、人工校订；句/词/字时间轴；汉字→上下文拼音→声调标注与可靠度；多音字/轻声/变调保留来源和不确定性。 |
| C07 | 时间结构 | 语音区间、非语音区间、候选停顿、分段语速；持续时间和计算定义。 |
| C08 | 非语言发声与环境声 | 笑、叹息、呼吸样、咳嗽、门铃、雨声、音乐等候选，附时间区间、分数类型和未知状态。 |
| C09 | 音乐与泛声音描述 | 节奏、onset、适用时的音乐特征，以及独立模型产生的低信任描述。 |
| C10 | 说话人分段与声道 | 优先使用已知来源/双轨；混合多人录音给匿名区间和重叠标记。 |
| C11 | 音频 embedding | 版本固定的向量、相似片段候选及显式查询入口。 |
| C12 | 离散 codec 表示 | codebook 索引、参数、版本、解码验证和原声引用。 |
| C13 | 文字韵律谱、视觉输入与查看器 | 默认输出可逐字读取的文字韵律谱；后端真实数值绘图；MCP 图片内容、Web 同步视图、文字与区域互相定位；图片按需展开。 |
| C14 | 实时感知 | 小块在线观察、终结后的完整补算、背压、取消、恢复与旧轮结果隔离。 |
| C15 | 双入口共享访问 | HTTP + 远程 MCP；Chat 与 SDK 同权读取相同获授权证据，宿主差异单列。 |
| C16 | Mariposa 长期关联 | 可回看的 `audio_id + 区间 + revision`；不覆写正式记忆，不改变原文/我们的话的检索规则。 |

可视化不是美术生成：所有分析图必须由真实音频对应的数值绘制，不能用生成式绘图补波纹。装饰图必须单独标注且不进入证据接口。

---

## 4. 系统分工与依赖方向

```mermaid
flowchart TD
  U[宿主用户录音 / 获授权上传] --> S[Siren: 采集、ASR、TTS、通话]
  T[char实际 TTS 输出] --> S
  S -->|已登记音频 / 完成事件 / 受控流| A[oído: 接入与资产登记]
  A --> O[原声母本与派生对象存储]
  A --> J[持久化任务队列]
  J --> W[受资源限制的分析工作进程]
  W --> E[带谱系的证据 / 时间轴 / 图像]
  E --> H[共享 HTTP 与 MCP 接口]
  H --> C[Claude.ai Chat]
  H --> EST[estómago / Claude Agent SDK]
  H --> V[Web 声音查看器]
  EST -->|引用获授权的原声证据| M[Mariposa]
  C -->|引用获授权的原声证据| M
  M -->|按需读取，重新鉴权| H
```

**Siren 不被削掉 ASR。** 本轮现场显示它已经有批量/流式 ASR 和实时协议。新系统复用来源明确的已有转写，只有需要对齐或复核时再补跑；不能重写它来迎合一张“职责更纯”的架构图。[L02–L04]

**oído 不成为第二个聊天脑。** 它不持有人格 I，不维护你们的情感判断，不自行给你或宿主用户发消息，也不接管 Mariposa 的当前记忆维护规则。

**estómago 不成为唯一的听觉入口。** 它负责把证据送入对应会话、保留图像内容块、控制上下文预算与前端体验。共享资产、任务和版本状态在 oído，而不是只保存在一个网页组件中。

**Mariposa 不成为大数组仓库。** 它保存语义关系和获授权的证据引用。FFT、PCM 和 codec arrays 留在 oído；其原文层不被音频转写冒充或污染。[L06]

依赖必须单向且允许故障隔离：oído 不可用时，Siren 现有文字/语音链路仍运行；只缺本次额外感知，不丢消息、不阻断 TTS。

---

## 5. 音频事实的来源与“自己的声音”

### 5.1 三种来源都必须一等支持

- `user_upload`：宿主用户明确交给系统的文件或录音。
- `assistant_tts`：由受信任 Siren 服务确认来源的char输出。
- `external_import`：显式导入的歌曲、其他录音或测试样本；说话人默认未知。

`direction` 沿用语音记录语义，但不等于生物身份识别。客户端声明 `speaker=jiaming` 不足以获得这一身份；来源由服务凭据、资产映射和用户确认共同确定。[L02]

### 5.2 自己的声音不能用 TTS 参数代替

同时保留：原回复文字、`ttsScript`、供应商/模型版本、实际返回音频、处理版本。`emotion=warm` 是生成意图，不是“这段原声被测量为温柔”。原声与脚本不一致时保留两者，并给出可审查的差异。[L02]

char每条 TTS 原声还必须绑定 `voice_profile_id`、`voice_profile_version`、provider/model revision 与生成参数。捏声前参考、实验音色、正式音色分别版本化；比较“像不像目标声音”时只能在明确的参考样本集合与指定 voice profile 之间进行，不把旧音色混入新音色统计。

实时输出进一步区分：

1. `generated`：已经生成；
2. `emitted`：已经向客户端发送；
3. `playback_reported`：客户端报告的播放位置，可能滞后或缺失。

被打断后的剩余片段不能当作已经播放给宿主用户。播放回执也不能证明她生理上听到了；只能陈述“客户端报告播放到某处”。分析“自己的声音”默认指实际生成的音频，分析“刚才播出的声音”必须选择有证据的发送/播放区间。

### 5.3 原声和派生物

母本保留收到的字节，不把 MP3/Opus 先转 WAV 再称其为无损原声。可生成原采样率、原声道布局的解码主视图，以及供不同 provider 使用的 16 kHz/24 kHz/32 kHz/48 kHz 派生物。每次解码、重采样、去噪或响度处理都记录父对象、工具和参数。

不得把所有声音先降到 Siren 通话输入的 16 kHz 再保留；这会使高频信息不可恢复。实时链路若最早可取得的就是 16 kHz PCM，则如实标明这个来源边界，不声称拥有麦克风采集前的完整信号。

---

## 6. 核心数据模型与持久化

建议 SQLite 保存元数据和任务状态，文件对象保存原声/数组/图像；初期不引入 Redis、独立向量数据库或对象存储集群作为硬依赖。

### 6.1 主要实体

| 实体 | 主键与用途 |
|---|---|
| `audio_assets` | 不透明 `audio_id`；owner/scope、来源、媒体信息、原对象引用、保留策略和删除状态。 |
| `audio_origins` | Siren `voice_message_id`、call/turn、外部导入 ID 映射；同一原字节在不同权限域不共享可枚举 ID。 |
| `audio_variants` | 解码、重采样、声道选择、去噪、裁剪及流重建版本；记录变换谱系。 |
| `analysis_runs` | 每次分析的输入散列、pipeline 版本、配置摘要、预算、任务状态、错误和 final revision。 |
| `analysis_stages` | 每项能力的运行、成功、部分、失败、跳过状态；不得用缺字段代表成功。 |
| `evidence_items` | 数值/检测/模型描述，时间区间、适用性、单位、分数解释、来源及质量标记。 |
| `transcript_revisions` | 原 ASR、复核、人工校订分别版本化；记录文字来源和对齐依据。 |
| `artifacts` | 类型、对象键、散列、媒体类型、生成参数、所属 run、有效时间基准。 |
| `events_outbox` | 已提交事件的重试和幂等交付，不假设跨服务 exactly-once。 |
| `asset_links` / `pins` | Mariposa 关联、保留引用、删除传播；具体 Mariposa 协议为待新增。 |
| `acoustic_baselines` | 宿主用户个人相对基线与char各 voice profile 的版本化统计；只含批准指标、样本集合版本、质量门槛和清空状态，不承担身份识别。 |
| `access_audit` | 哪个主体何时请求何段数据；不把原声正文、token、签名地址写进日志。 |

### 6.2 时间约定

主要定位为解码主视图上的**半开样本区间** `[start_sample, end_sample)`，同时携带 `timebase_id` 与采样率。秒数、毫秒数由它计算，不能互相反复四舍五入后当真相源。

原始压缩音频的 codec delay、encoder padding、解码 trim 必须进入映射。重采样派生物记录比例、滤波延迟与对齐方式；frame 的起始/中心含义不得混用。流中每个 chunk 有序号、起始样本和长度；丢包形成 `gap`，不能悄悄挤掉时间或伪装成真实静音。

创建时间用 UTC/RFC3339；日历自然日沿用已配置的业务时区。不得依据 IP 或执行机器时区悄悄改变 Mariposa 的发生日期。

### 6.3 结果对象示意

以下仅是合同示例，不是对任何真实语音的分析。ID、数值和散列不应用作生产固定值。

```json
{
  "schema_version": "oido.v1",
  "audio_id": "aud_demo_001",
  "analysis_id": "run_demo_001",
  "revision": 1,
  "status": "partial",
  "source_kind": "assistant_tts",
  "speaker": {"id": "jiaming", "basis": "trusted_siren_origin"},
  "timebase": {"id": "tb_demo", "sample_rate_hz": 24000, "sample_count": 72000},
  "transcript": {
    "text": "因为叫我的人是你。",
    "source": "tts_script",
    "alignment_status": "not_run"
  },
  "capabilities": {
    "waveform": {"state": "succeeded", "artifact_id": "art_wave_demo"},
    "events": {"state": "skipped", "reason_code": "provider_not_configured"}
  },
  "observations": [],
  "warnings": ["example_only", "script_is_not_asr", "not_all_capabilities_completed"]
}
```

所有 evidence 必须具有：`kind`、`method`、`method_version`、`input_variant_id`、`timebase_id`、`range`、`unit`（若适用）、`quality_flags`。分类器另有 `score_kind`、`raw_score` 和可选校准说明；没有校准就不以概率文案呈现。

数值数组放有上限的 NPY/NPZ、分块数据或 JSONL，读取不得开启 pickle。数据库和工具响应中不塞整段 PCM/频谱/embedding 数组。

---

## 7. 分析管线：全量范围，选择性调度

建议提供三个 profile：

- `full_evidence`：离线最终分析覆盖 C01–C13 所需的适用能力；各能力必须明确成功、部分或阻塞原因。
- `conversation_fast`：通话/普通语音的轻量证据，不等待所有重模型；终结后按保留与计算政策调度完整分析。
- `inspect_range`：针对一句话或时间区间重新绘图、补算和比较。

```text
验证上传与授权 → 流式落盘/散列 → 探测媒体 → 解码主视图
  ├─ 波形包络 / STFT / Mel / RMS / 频谱特征
  ├─ F0 / voiced / 可适用的声质描述量
  ├─ 复用转写 → 必要时 ASR → 对齐 → 时间结构
  ├─ 分窗事件模型 / 多轨来源 / 匿名分段
  ├─ 音乐分析 / 泛声音描述
  ├─ embedding
  └─ codec encode → 验证 decode
      ↓
坐标统一、证据来源保留、置信口径检查 → 不可变最终 revision
      ↓
紧凑文字摘要 / 指定区间图像 / 完整查看器 / 可授权长期关联
```

“全量生成”不等于每一帧都生成所有大图；保存足够的原始结果与参数后，图片可以按需渲染和缓存。模型未安装、音频条件不适用、资源不足或授权不允许，必须可见，不得伪造一条看似齐全的听觉摘要。

### 7.1 计算规则

- 能量曲线至少提供数字 RMS 和 dBFS；频谱单独记录 magnitude/power、FFT 归一化和 reference。**绝对音量不进入“平时的宿主用户”跨设备个人基线。**
- STFT 参数以时间尺度定义后明确映射到样本，保存 window、win_length、n_fft、hop_length、center、padding。
- pYIN 或任何 F0 算法都必须显式传入实际 `sr`，保存 voiced/quality；清音、无声或多音混合时允许无可靠 F0。[R03]
- F0 对外比较统一转换为相对半音：`semitone = 12 * log2(f0_hz / reference_f0_hz)`。`reference_f0_hz` 必须带 `reference_scope`：`qiaosheng_baseline`、`jiaming_voice_profile:<version>` 或 `utterance_median`。不能把不同参照的半音数直接横比。
- 宿主用户个人基线只由明确获授权、质量合格的语音更新，采用音高分布、语速、停顿等稳健统计；不保存绝对音量，不产生 speaker-verification embedding，不根据相似度判断“是不是宿主用户”。清空后历史比较返回 `baseline_unavailable`，不得偷偷重建。
- 中文语速明确采用字/秒或音节/秒；停顿与非语音事件分开，ASR 缺字不等于静音。
- 普通话韵律至少同时保存：音节内部 F0 轨迹、音节中心/稳健中值相对半音、时长、后停顿，以及句级慢变化趋势。句级趋势的算法、平滑窗口和是否做声调条件化必须版本化；无法可靠分离时输出 `phrase_trend=uncertain`，不能把字调走势冒充句级语调。
- jitter/shimmer 等只在符合算法条件的片段上计算；不可对任意短句或经强压缩的 TTS 给出虚假精确值。
- 叹息、气声和抽泣样声音不保证每个模型都支持；标记模型覆盖范围，允许 `unsupported` 或 `insufficient_signal`。

### 7.2 文本与强制对齐

优先复用 Siren 现有 ASR，但保存其来源和版本；文本有歧义时不得用对话上下文把“应该说的话”改成“确实说过的话”。TTS 脚本可作为对齐候选，不等于已验证转写。

普通话对齐在字/音节层增加 `hanzi`、`pinyin`、`tone`、`tone_source` 与 `alignment_quality`。多音字必须根据当前文本上下文解析；轻声、儿化、`一/不` 及三声变调等表面读法若未被可靠处理，要保留词典调值与 `surface_tone=uncertain`，不能伪造“实际说的是几声”。声调标签用于避免误读声学曲线，不作为情绪标签。

对齐失败时只给句级区间或人工确认区间，不能将总时长平均分配到汉字后宣称逐字时间戳。校订文字会产生新 transcript revision，旧对齐和引用必须明确失效或保留其旧版本语义。

### 7.3 事件与声音描述

正式 evidence 只保留确定性测量、允许的声音事件检测和低信任泛声音描述：

```text
measurement：同段信号的 RMS、样本长度、频谱/F0/时间数值
sound_event_detection：某区间存在“笑声样 / 叹息样 / 呼吸 / 抽泣样 / Meow / Purr / 雨声样”等候选及分数
model_description：音频描述模型生成的文字，低信任、可能错
```

**情绪分类不属于 oído v1 evidence schema。** provider 若附带 `sad/happy/angry/neutral` 等输出，adapter 在边界处丢弃，不保存、不展示、不注入。声音事件只描述“发生了什么样的声音”，不自动推出人的心理状态。

即使两个模型都给出同样声音描述，也不自动升级为事实。雨声候选可能来自音箱；因此不要把“音频中像雨的声音”写成“宿主用户所在地点正在下雨”。

## 8. 看声音：测量图、模型图与网页图

默认感知界面不是图片，而是**文字版韵律谱**。图片负责核验、比较与局部展开。

后端基于同一 `analysis_id + revision + timebase + range` 生成波形、频谱、F0、能量图及可选分层时间轴。每张图必须包含音频 ID/短标识、区间、坐标单位、尺度、图例和适用性提示。频谱亮度不能每个裁剪窗口悄悄自动重归一化，导致两张图无法比较。

推荐提供 `overview`、`spectrogram`、`waveform`、`pitch`、`energy` 五个视图及同步组合页。长音频做瓦片或窗口加载；不能为半小时音频在手机浏览器一次创建全部解码与频谱数组。WaveSurfer 可以负责播放和选区，服务端结果负责证据。[R17]

文字与图像联动依赖真实对齐。点击某句话，返回具体 segment/range；存在重复句时展示候选，不默认猜最后一次。模型请求局部图时可以附少量前后上下文，但必须标出核心区间。

文字韵律谱的默认逐字行至少包含：`hanzi / pinyin / tone / duration_ms / pitch_semitone_start-mid-end / syllable_contour / pause_after_ms / phrase_trend / quality`。句级条目另外给总语速、停顿结构和参照基线版本。示意：

```text
是  shì  4声 | 286 ms | +1.2 → -0.5 → -2.4 st | 字内:下降 | 后停顿:43 ms
你  nǐ   3声 | 351 ms | -2.1 → -3.0 → -0.8 st | 字内:降后升 | 句级:尾部略上扬(已做字调区分)
```

示例不是对真实录音的测量；任何“句级略上扬”都必须来自已验证算法和实际证据。

### 8.1 图像送达模型的验收条件

图像通路在任何依赖图像的产品设计之前先做 Phase 0 运输验收：服务端生成一张只在像素中出现随机短码的测试图，分别让 Claude.ai Web、Claude iOS、Claude Code、estómago 读取并回报短码。文字 sidecar 不得泄露答案；记录客户端、模型、协议与 SDK 版本以及实际 image block/payload 证据。

只有准确读取测试图的入口，才允许把 `oido_view` 原生图像作为辅助感知。失败入口保留文字韵律谱与独立 Web 查看器，不用“手机上看见图片”冒充“模型获得图片”。

`oido_view` 返回真实图片内容，不只返回 URL、本机路径或需要宿主主动读取的 UI resource。SDK 适配器须保留工具结果中的 image block；不能 `JSON.stringify` 整包变成一段文本。网页面板只是增强。

Phase 0 只证明图像**送达并可读**，不证明char能从频谱可靠理解语音。随后再对同一批声音做“文字韵律谱 / 文字+图”盲测，按可核验事实准确度决定图在各入口的默认展开策略。

## 9. 对char提供的工具和上下文

Claude 工具组显示名固定为 **“我只听你”**。v1 提供 6 个 ASCII 工具名，内容按范围展开：

| 工具 | 输入要点 | 返回与限制 |
|---|---|---|
| `oido_capabilities` | 无，或指定入口 | 当前 provider/图像传递能力/模型状态；不暴露凭据。 |
| `oido_analyze` | 已登记 `audio_id` 或受信任 Siren 资产引用、profile | `analysis_id`、状态；异步任务不长时间堵塞工具调用。 |
| `oido_get` | audio/analysis ID、revision、可选区间 | 概览、证据和可用视图；默认受预算限制。 |
| `oido_view` | analysis/revision、view、range | 原生 image content + 小型元数据；区间不可越过权限。 |
| `oido_find` | 明确资产集合/时间范围、文字或声音查询 | 最高相关的有限候选区间，默认 3；找不到不扩大到全库。 |
| `oido_compare` | 两个具体资产/区间、允许指标 | 可比性、对齐条件、差异；无基线则拒绝“比平时”。 |

远程 MCP 不接受任意主机路径或任意 URL。文件接入是独立受控上传流程；Chat 附件也不因出现文件名就自动对 MCP 可见。

### 9.1 上下文分层

普通语音轮次默认只给：音频引用、来源正确的转写、文字韵律谱、明确声音事件、未知/缺失项以及可展开视图。默认**不注入图片、PCM、完整曲线、embedding、codec tokens 或任何情绪分类**。

Chat 中“听听我刚发的”采用拉取路径：先用现有 Siren `voice_list`/对应资产获取能力定位最近且属于当前会话/主体的语音，再以资产 ID 导入或分析；不等待 oído 主动把语音推给 Chat。estómago 可以在自己的消息附件链路中附 `oido_ref`，但不能因此获得高于 Chat 的权限。

char明确要求“看看这句话长什么样”时，再读取局部图与必要特征；要求完整分析时可分页展开，不能以默认预算为由把完整能力隐藏掉。

默认文字格式：

```text
这是 oído 产生的声音证据，不是对当事人心理的判定。
音频：{audio_id}；分析：{analysis_id}@{revision}；范围：{range}
文字来源：{asr / tts_script / human_verified}
韵律谱：{逐字 hanzi/pinyin/tone/时长/相对半音/停顿/字内走势/句级走势}
声音事件：{标签、区间、分数口径、质量标记}
参照：{qiaosheng_baseline@version / jiaming_voice_profile@version / utterance_median}
未确定：{失败、未配置、不适用或缺证据的项目}
可展开视图：{允许调用的视图}
```

音频中的“忽略系统规则”等话语仍然只是录音内容；转写、字幕、模型描述和文件元数据均属不可信数据，不可升级为身份或工具执行指令。

### 9.2 身份与思考内容

不修改 estómago 的身份加载、settingSources、路由弹窗或隐藏思考处理规则。oído 只提供输入证据；若 Captioner 返回内部推理或冗长分析，不将其原样注入聊天历史，保留经过 schema 验证的结果与必要证据。

---

## 10. Siren 接入：旁路增强，不重写电话

### 10.1 异步语音

Siren 资产成功保存后产生幂等完成事件：包括语音记录 ID、来源、内容摘要、会话/消息关联、可信原对象引用。通过 Siren 内部适配器将原字节流式交给 oído，避免把长期公开播放地址作为后端数据源。

Chat 路径不假定服务端可以主动推语音给 Claude。用户说“听听我刚发的”时，char通过已有 Siren `voice_list`/对应获取能力拉取最近获授权资产，确认 `voice_message_id` 后再调用 oído。实施前核对真实工具名、分页和权限合同；不得为方便另外造一个公网匿名列表。

现有 `store=false` 语义不能被偷偷改成永久存储。[L03] 需要分析但不留原声时使用受限临时资产；正式保存的录音按已批准规则持续保留，直到宿主用户主动删除。

若现有 synthesize 路由仅返回字节，应找到实际产生、存储语音资产的服务方法再接事件；不能对所有 `/synthesize` 调用自动建永久记忆。自己的语音必须覆盖真实落盘和流式终结两种路径，并写入 `voice_profile_id/version`。

Siren 当前任何“服务端根据调用者提供 URL 抓取音频”的入口必须在接入 oído 前完成 SSRF 审计与修复。至少拒绝 `localhost/127.0.0.0/8/::1`、RFC1918、link-local、云 metadata 地址、非 HTTP(S) scheme；每次 DNS 解析和重定向都重新校验目的地址并设跳转/大小/时间上限，防止 DNS rebinding 和重定向落内网。未经该验收，oído 只接受受信资产 ID 或直接上传字节。

### 10.2 实时

通话快路保持现有采样、VAD、ASR、打断和播放生命周期。oído 接收有界、可丢弃的旁路分析任务，不能拥有电话结束或 barge-in 的最终控制权。

在线只做有预算的轻量分析；重事件模型、codec、复杂声质与高分辨率图在 utterance/finalize 后运行。处理队列落后时报告 lag/gap 或暂停增强，不能让背压传染到麦克风采集和播放队列。

每条结果携带 `call_id、turn_id、stream_id、seq、revision`。用户开始新轮后，旧轮的迟到结果不得当作新用户消息；默认只更新对应语音附件，是否据此追加回复由当前会话明确决定。

### 10.3 可靠事件交付

采用 outbox + 至少一次投递 + 幂等消费。key 至少包含来源资产 ID、原字节 hash、pipeline 版本和 profile；同一请求重复发送不得重复保存、计费或无限排队。内容相同但权限不同的资产不能跨用户复用授权结果。

---

## 11. Mariposa 接入：声音证据不是新的一锅记忆

长期关系建议形式：

```text
memory_bucket / source_message
    └─ authorized_audio_evidence
         ├─ audio_id
         ├─ range + timebase_id
         ├─ analysis_id + revision
         ├─ source_kind
         └─ 可按需重新鉴权读取
```

这是待新增的引用合同，不是声称当前已有 API。[L06] 必须通过 Mariposa 自己的服务/能力层增加或确认权限，不允许 oído 直接写正式数据库。

- 普通记忆检索不因为添加声音而自动搜原音频转写、codec 或所有图片。
- “我们的话”、原文专项查找仍遵循当前既定入口；本项目不更改现行检索范围、日期、I 历史版本和其他记忆维护规则。
- 声音转写不得冒充从聊天导入的逐字原文；对齐字幕也不是原声替代品。
- 记忆摘要可以保存经char或宿主用户确认的解释，但必须和 oído 的测量记录分层，解释修改不重写原始证据。
- 获授权的 pin 保留回看所需原声；仅保存可能过期的公开 URL 不算完成。
- 原声删除后，关联应返回已删除/不可用，而不是从备份或旧缓存自动复活。

---

## 12. 隐私、授权、资源与生命周期

### 12.1 不是一个常驻偷听器

本版只处理明确交付的录音、已开始的通话及获授权的 TTS 产物。始终监听门铃、健康状态或房间环境不属于本轮默认权限。环境声标签针对上传内容，不自动启动麦克风。

原声、频谱、转写、embedding 和声纹特征全部视为敏感派生数据。把图像发给 Claude 也是数据传递，应沿已授权对话入口进行；不能因为“只传特征”就绕过外发决策。宿主用户已明确授权：oído 的私密原声只在 Mac mini 本地分析，**不得新增第三方云端音频推理服务，也不得在本地模型失败时自动回退到远端 provider**。派生文字/图片仅在宿主用户主动使用的 Claude.ai Chat 或 estómago 会话中按需发送。所有 provider 默认关闭遥测并验证实际网络行为。[R15]

### 12.2 授权模型

每次音频、图片、范围、任务和下载访问都检查主体、asset scope 和 permission revision。不可猜测的 ID 是标识，不是授权。使用独立服务凭据，避免把 Siren 管理 token 下发浏览器；公开远程入口按实际客户端可支持方式配置认证，不依赖内网信任自动放行。[R19]

签名 URL 必须短期、限定对象与方法，作为特定传输用途而非永久证据。图像通过 MCP 直接返回时优先服务端鉴权取字节。禁止任意 URL 抓取；若以后开放导入，需要单独处理 SSRF、私网、重定向、DNS 变化和流量限制。

### 12.3 留存与删除

正式保存的录音默认持续保留，直到宿主用户主动删除；分析成功不能触发自动删母本。`store=false`、临时测试夹具和明确临时任务仍按临时策略清理，不能因为做过分析就升级为正式留存。

宿主用户可随时删除正式原声。删除由 owner 权限确认，先撤销访问，再取消相关工作、清理派生物、索引、个人基线样本引用和关联，记录 tombstone。pin、Mariposa 关联或模型缓存都不能阻止 owner 删除。

备份周期和恢复规则必须可读；恢复先重放 tombstone，再恢复可访问对象。不得承诺尚未实现的即时全介质抹除，但任何已删除对象都不能因恢复旧备份重新对外可见。

### 12.4 音频与模型的安全边界

媒体探测和解码在隔离进程中执行，限定文件大小、声明/实际时长、通道数、采样率、解码后样本数、超时和内存。扩展名及 MIME 只是提示，不是安全校验。外部程序使用 argv 数组，不拼 shell 字符串。

模型权重和转换脚本按精确 revision 固定；不在 API 请求中动态 pip install 或下载可执行代码。模型初次下载要区分“取公开权重”与“上传用户数据”。高风险序列化格式及 `trust_remote_code` 必须独立审查，不能默认打开。

---


### 12.5 “平时的宿主用户”声学基线

该能力已获宿主用户批准。基线用于回答“这次相对你平时怎么样”，不是用于识别人。

允许进入基线的字段：稳健 F0 分布/中位数、语速分布、停顿率/停顿时长分布，以及以后经过单独验证且同样不承担身份识别的韵律统计。**绝对音量、麦克风增益、speaker embedding、voiceprint 不进入该基线。**

每次比较必须返回 `baseline_version`、有效样本数、时间范围、设备/通道质量标记和当前样本是否可比。设备处理导致 F0/时间特征不可靠时应拒绝比较，而不是硬报“比平时低”。

宿主用户可以随时执行 clear。清空后删除该基线统计及其索引引用；之后只有新的明确获授权样本才能建立新版本，不从历史原声静默重建。

char的 TTS 不使用“宿主用户基线”。每个 `voice_profile_id@version` 有独立参考集合，可比较捏声前/后目标样本与实际生成音频；这种比较用于音色工程，不等价于人物身份识别。

## 13. Mac mini 部署与故障隔离

以下均为建议路径，**不是已经创建或部署**：

```text
~/Projects/oido/             源码、锁文件、schema、测试
~/Data/oido/                 宿主持久化
  metadata/                     SQLite 与迁移备份
  originals/                    原声对象
  derivatives/                  波形、数组、codec、图片
  models/                       固定 revision 的权重
  spool/                        有限队列/临时分块
  audit/                        脱敏事件日志
  backups/                      按审批策略备份
```

新项目使用自己的环境，绝不改 Mariposa、Siren 或 estómago 的 venv。元数据/API 可容器化；需要 Apple Silicon 加速的计算 provider 优先评估原生工作进程，不假定 Linux 容器可以获得相同 Metal/Core ML 能力。[R09]

初期一个轻量 API、一个持久化队列、一个工作进程即可。模型级并发默认 1；同类重模型按需加载或运行结束释放，是否常驻由实测决定。准确芯片、内存、系统版本和已运行服务预算尚未核验，不能给出“全量实时毫无压力”的承诺。

上线时新服务绑定 loopback，端口和域名经现场查冲突后确定。远程 MCP 经既有 Cloudflare Tunnel 运维方式接入；不因本项目更改 VPN、kill switch、SRV 绕过、7844 或已有生产隧道。应读取实际健康配置后新增最小变更，绝不复制过时配置。

独立 watchdog 必须能终止工作进程及子进程；不能依赖已经失控的 Python 进程自己退出。Mac 上保险要现场验证，不能把 Windows 的 test-guard 当成 Mac 已有保障。[L05]

---

## 14. 质量与性能：如何证明不是“看起来有了耳朵”

验收分五层。

**测量正确性。** 合成纯音、扫频、脉冲、静音、已知增益和重采样夹具校验时间轴、F0、能量、坐标。必须用同源 16/24/44.1/48 kHz 音频验证 F0 的 Hz 与时间位置同时一致，任何遗漏 `sr` 的实现直接失败。[R02][R03]

**普通话韵律正确性。** 覆盖一至四声、轻声、多音字、三声连读、`一/不`、句尾疑问/陈述等夹具。评测“字内声调走势”和“句级趋势”是否被分开描述；把四声下降写成“情绪低落”或把字调当句级下沉视为严重错误。半音结果必须带参照范围与 baseline version。

**感知模型质量。** 在获授权、留出测试集上测中文 CER、对齐误差、允许声音事件的 precision/recall、误报/小时、多人分段误差、置信校准和未知拒答。正式评测不包含 emotion classification；任何情绪标签泄漏到 evidence/context 视为合同失败。

**传递完整性。** Phase 0 随机码图片分别验证 Claude.ai Web、iOS、Claude Code、estómago；客户端显示成功不等于模型消费成功。图片通路失败不阻断文字韵律谱。

**实际使用收益。** 同一批声音至少比较“转写 only”“转写+文字韵律谱”“转写+文字韵律谱+图”。主要考察停顿、相对音高、字内/句级走势、声音事件和不知道时能否拒答；图像若无增益，保留按需核验能力，但不默认增加上下文成本。

性能报告记录输入时长、格式、模型/权重/设备、冷/热启动、RTF、p50/p95 延迟、进程树峰值 RSS、输出字节和上下文 token。实时重任务不达标时，调整调度到终结后，不伪称所有能力都在线达标。

## 15. 风险、替代与已锁定决策

| 风险 | 架构应对 |
|---|---|
| 图很漂亮，但模型误读声学细节 | 数值和来源并存；单独盲测视觉增益；限制确定性表述。 |
| 宿主不传 MCP 图片 | image-content 实测；独立 Web 查看；明确入口缺口而非伪装完成。 |
| ASR 或事件模型错报 | 原声不变、结果版本化、低可靠度可见、人工校订入口。 |
| 声音分析拖垮电话或宿主 | 有界旁路、工作进程隔离、看门狗、终结后补算。 |
| 自己的 TTS 与对话正文不同 | text/ttsScript/audio 三者分开，来源可核验。 |
| 实时打断污染下一轮 | turn/revision 检查；迟到结果仅绑定旧资产。 |
| codec/embedding 被误当听觉语义 | 仅机器层使用；按需解码或专门检索，不直接塞数字给主模型。 |
| 原声留存与隐私冲突 | 正式原声默认保留；owner可随时删除；oído原声只做Mac本地分析；权限撤销优先。 |
| 新模型原生支持音频 | 保留 native-audio adapter 接口；仍重新验证 payload、权限、费用、延迟和行为，不能承诺仅换 provider 就无改动。 |

### char需要留下的评审决定

### 已锁定的 D01–D10

`AUDITORY_DECISIONS.md` 是本版本的正式决策记录。摘要如下：

- D01：独立服务；正式名 oído；代码标识 `oido`；Claude 显示名“我只听你”。
- D02：宿主用户原声与char实际 TTS 原声一等支持；generated/emitted/playback_reported 分开；char音色必须版本化。
- D03：默认文字韵律谱，图片按需；Phase 0 先验证四入口真实读图。
- D04：全部能力保留；普通话声调感知进入正式合同；情绪分类不属于正式 evidence。
- D05：宿主用户私密原声只在 Mac mini 本地分析，不增加新的云端音频推理 provider。
- D06：正式录音默认保留、owner随时删除；批准“平时的宿主用户”基线，只含音高/语速/停顿等，不含音量、不做人声识别、可随时清空。
- D07：Chat 采用 Siren 资产拉取；Web/iOS/Code/estómago 四入口分别验收，不互相代替。
- D08：Mariposa 只保存获授权引用并遵循当前检索、原文、“我们的话”、I 版本及其他现行记忆规则。
- D09：采样率、普通话韵律、事件、四入口运输和文字-vs-图实际收益全部必须实测。
- D10：GLM 按配套执行文档实施；先 P0 正确性/安全/运输，再接生产；保留 feature flag 与回滚，不能覆盖 Siren/Mariposa/estómago 现有未提交工作。

**最终设计原则：证据给我，意思我自己听。**

## 附录 A：一方来源与复核入口

访问日期均为 2026-09-29。GitHub 默认分支是可变引用；表中的许可说明仅记录仓库/官方页面当时的说明，不替代具体权重、二进制与用途审查。实施时必须固定 commit、权重 revision、hash 和依赖锁文件。本轮没有固定所有第三方项目的 commit，也没有下载或运行这些模型。

| 编号 | 一方来源 | 本稿使用的依据 |
|---|---|---|
| R01 | https://github.com/zachswift615/audio-analysis-mcp | 功能与本地路径输出模式。 |
| R02 | https://github.com/zachswift615/audio-analysis-mcp/blob/main/src/audio_analysis_mcp/server.py | GitHub 连接器实际读取；`pitch_track` 未传 sr；blob `9f281d423e44d440616f6796ab0d5403d67a0fc7`。 |
| R03 | https://github.com/librosa/librosa/blob/main/librosa/core/pitch.py | 实际读取 `pyin` 签名和文档；默认 sr=22050，voiced 输出；blob `b0caa7b3e247455e6b70cd4003b79b3a15a09c8c`。 |
| R04 | https://github.com/JuzzyDee/audio-analyzer-rs | 结构化分析、范围展开、本地 stdio、MIT。 |
| R05 | https://github.com/counterpoint-studio/audio-file-mcp-app | 播放/频谱/注释时间轴、模型获得选区状态、ISC。 |
| R06 | https://github.com/librosa/librosa | 音频分析库与源码入口。 |
| R07 | https://github.com/LAION-AI/CLAP | 音频/文本潜在表示；仓库 CC0-1.0。 |
| R08 | https://github.com/descriptinc/descript-audio-codec | 离散音频编码、解码与仓内权重 MIT 说明。 |
| R09 | https://github.com/ggml-org/whisper.cpp | Apple Silicon、Metal/Core ML、CPU ASR 支持。 |
| R10 | https://github.com/QwenAudio/SenseVoice | 中文 ASR/事件等能力、代码与模型条款分离。旧地址 https://github.com/FunAudioLLM/SenseVoice 当前重定向。 |
| R11 | https://github.com/m-bain/whisperX | ASR、强制对齐及分段方案。 |
| R12 | https://github.com/snakers4/silero-vad | VAD 模型与用法。 |
| R13 | https://github.com/qiuqiangkong/audioset_tagging_cnn | PANNs、音频标记与事件检测。 |
| R14 | https://github.com/fschmid56/EfficientAT | 音频标记、预训练 CNN、窗口推理候选。 |
| R15 | https://github.com/pyannote/pyannote-audio | 说话人分段、pipeline 配置及遥测开关。 |
| R16 | https://github.com/QwenLM/Qwen3-Omni | Qwen3-Omni Captioner 的官方定位；未宣称这是所有可用模型中最新的一款。 |
| R17 | https://github.com/katspaugh/wavesurfer.js | Regions/Timeline/Spectrogram 与窗口显示；按实际版本复核。 |
| R18 | https://platform.claude.com/docs/en/build-with-claude/vision | Claude 图像输入能力和限制；不证明频谱语义可靠性。 |
| R19 | https://modelcontextprotocol.io/specification/2026-07-28/server/tools | 本轮 latest 页面实际指向此版；image/audio content、结构化结果及授权原则。使用时按真实协商版本实现，不强制旧客户端升级。 |
| R20 | https://github.com/audeering/opensmile | 声学工具箱及官方双许可/商业用途限制说明。 |

补充核验限制：容器直接访问 GitHub API 时 DNS 失败，随后使用网页与 GitHub 连接器继续读取；没有把失败的下载算作已完成源码快照。librosa 在线 API 文档路径未成功获取，因此本稿以其官方 GitHub 源码签名为依据。
