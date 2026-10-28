# Phase 0 四入口随机码图片运输测试 · 协议与矩阵

依据：OIDO-EXEC-1.1 WP09 / 验收 E02–E04 · 宿主用户 2026-09-29 批准（"现在就做、不模拟、不代读"）。

## 铁律

1. 随机码**只存在于像素中**。任何 sidecar、清单、日志、聊天文本不得含码。
2. **每入口独立随机码**（`evals/phase0/fixtures/<entry>.png`），答案不能跨入口复用。
3. ground truth 在 `evals/phase0/secrets/<entry>.txt`（0600、gitignore）；校验只输出
   `MATCH / MISMATCH / LENGTH_MISMATCH`（`scripts/phase0_verify.py`），永不回显期望码。
4. 不模拟、不代读：**读码主体必须是该入口的真人-真模型链路**。GLM 只做导体与校验。
5. 通过标准（E02）：模型准确复述随机码。**客户端"能看到图"但模型读不出 = 失败**（E03）。

## 矩阵（结果记录处）

| 入口 | 能否取图 | 能否看清 | 复述准确 | 需额外点击/下载 | 客户端/模型版本 | 结果 | 日期 |
|---|---|---|---|---|---|---|---|
| claude_web | — | — | — | — | — | **not_run**（等宿主用户手动执行） | |
| claude_ios | — | — | — | — | — | **not_run**（等宿主用户手动执行） | |
| claude_code | — | — | — | — | — | **blocked**：本机无 claude CLI；需装+登录（待批准） | 2026-09-29 |
| estomago | — | — | — | — | SDK 0.3.283（探针已武装） | **blocked**：本 shell 无 Claude 认证环境；探针等授权环境 | 2026-09-29 |

矩阵四行全部 `MATCH` 前，任何宿主不得把 `oido_view` 原生图像当默认能力（EXEC §8.1）。
失败入口：文字韵律谱 + 独立 Web 查看器顶上，不伪造"模型已看图"。

## 各入口执行方式

夹具生成（已执行；重新随机化则重跑并重新登记）：

```bash
.venv/bin/python scripts/phase0_make_fixture.py --all
```

### claude_web（宿主用户手动，约 1 分钟）

1. 打开 Finder，进入 `~/Projects/oido/evals/phase0/fixtures/`，把 `claude_web.png` 拖进
   Claude 网页版和周char的对话框（拖入后等缩略图出现）。
2. 发送这句话（提示词已就绪，见 kit；不含答案）：
   `这是一张随机码测试图。请只输出图中显示的随机码，逐字符原样复述，不要添加其他内容。`
3. 把他回复的码发给 GLM（或自行执行校验命令）。注意：发送的是他**说的**码，不需要你知道真码。

### claude_ios（宿主用户手动，约 1 分钟）

同上，用"照片"按钮从本机相册选入 `claude_ios.png`（可先 AirDrop 到手机），
同一句提示词，记录是否需要额外点开大图才能读对。

### claude_code（需先装 CLI + 登录，等批准）

**防作弊边界（AUD-4，已脚本化）**：CLI 主体与 secrets 同机同用户，0600 挡不住 owner——
`scripts/phase0_run_claude_code.sh` 强制"临时目录 + 禁全部工具 + 只喂图"，
随机码只能经图片通道进入（E04 本意）。装好 CLI 并登录后一条命令执行：

```bash
npm install -g @anthropic-ai/claude-code   # 需宿主用户批准后执行
claude                                      # 首次登录（她本人操作）
cd ~/Projects/oido && bash scripts/phase0_run_claude_code.sh
```

estómago 探针已内置同等约束（`allowedTools: []` + `settingSources: []`）。

### estomago（探针已武装；在她已登录的普通终端里跑）

**凭据铁律（宿主用户 2026-09-29 裁定）**：不为测试复制任何登录信息——`~/.claude`、
API key、钥匙串一律不搬进 oído。探针零凭据搬运，认证只来自运行环境的既有登录态；
环境没登录就保持 BLOCKED，不搬凭据换绿灯。oído 是插件，不拥有自己的登录身份。

```bash
cd ~/Projects/oido && OIDO_PHASE0_AUTHORIZED=1 node scripts/phase0_probe_estomago.mjs
```

在宿主用户本人已登录的终端执行即可（无需任何配置动作）。临时 ESTOMAGO_HOME 隔离
estómago 生产数据；`settingSources: []` 关闭人格加载——只测一件事：
**图片有没有真的传进模型**。

## 校验（所有入口同法）

```bash
.venv/bin/python scripts/phase0_verify.py --entry claude_web --answer "<他复述的码>"
# 或 --answer-file evals/phase0/answers/<entry>.raw.txt
```

## Phase 0 证明与不证明

- 证明：图像内容块在该宿主真实送达模型、可读、可复述。
- 不证明：模型能从频谱可靠理解语音（那是后续"文字韵律谱 vs 加图"盲测的事）。
