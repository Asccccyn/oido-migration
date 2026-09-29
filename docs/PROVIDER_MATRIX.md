# Provider 矩阵（v0.1 现状）

原则：**未安装 = `not_configured`，不是 available**（EXEC §4.1）。安装时逐行补：仓库/commit/代码许可/权重许可/checkpoint hash/输入采样率/输出粒度/资源路径/峰值内存/冷启动/网络依赖/遥测开关。

| Provider | 类别 | 状态 | 网络 | 版本 | 备注 |
|---|---|---|---|---|---|
| librosa (pyin) | DSP/F0 | **available** | 无 | 1.0.0 | 锁于 requirements.lock；非推理 provider |
| numpy / scipy | DSP 基座 | **available** | 无 | 2.5.3 / 1.18.1 | 同上 |
| whisper.cpp | ASR | not_configured | local_only | — | Apple Silicon/Metal 待本域实测 |
| SenseVoice | ASR+事件 | not_configured | local_only | — | 若采用：情绪 head 边界丢弃（B20 已有测试） |
| PANNs | 事件 | not_configured | local_only | — | 与 EfficientAT 二选一 |
| EfficientAT | 事件 | not_configured | local_only | — | 同上 |
| silero-vad | VAD | not_configured | local_only | — | |
| pyannote | 分段 | not_configured | local_only | — | 遥测须关 + 网络实测 |
| CLAP | embedding | not_configured | local_only | — | 不进普通召回 |
| DAC | codec | not_configured | local_only | — | |
| Qwen3-Omni Captioner | 描述 | not_configured | local_only | — | 资源未核验 |
| openSMILE | 声质 | not_configured | local_only | — | 双许可，用途条款未定 |

**Siren 既有 ASR/TTS 供应商属于 Siren 链路**；oído 从 Siren 只收受信资产与既有转写，新增分析一律本地（D05）。
