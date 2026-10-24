# oído

- 2026-10-24（追加，未部署）：她裁定延伸——快照探针夹具修复（转写排队前插入，断言原样）；全套 291 绿零失败。
- 2026-10-24（隔离分支，未部署）：GLM CallHome 执行轮 B3——RE-13 生命周期（async 等待/取消按 DB 权威/终态非伪 409/wait0 幂等回概览/PCM 后取消不启新段）+RE-14（find 游标真根因=函数内 import 遮蔽、末页无假游标、MCP v2 补 cursor、compare 超预算 422、claim_next 租约函数、转写快照不一致拒执行 transcript_changed）；独立探针 13/14（transcript 快照探针结构性矛盾已记录+补充语义测试 2 绿）；290 绿/1 红（同矛盾项）。
- 2026-10-23（独立审计隔离分支，未部署）：修复模型回执冗余、MCP schema/开关边界、字节幂等、转写删除、派生物迟到复活、范围裁剪、F0时序与帧尾；保留异步调度/取消/真实宿主与真人评测阻塞。见 `docs/AUDIT_20261023.md`，不将离线通过视作生产验收。

- 2026-10-22（二批，已部署生产）：她批「全做完」——view/find/compare 三工具落地，六工具合同全通：view 五种视图全从真实测量值绘制（PIL+STFT/pYIN；artifacts 幂等复用；range 样本级裁剪；MCP 原生 image content）；find=对齐区间文字匹配（精确/fuzzy；默认3最大10；对齐不可用=409 如实）；compare=已存分析可比量差异+无基线明确拒绝「比平时」。管线质量修复：音节内峰簇合并（<120ms）、单字短语不误标 phrase_tone_unresolved。**249 测试绿**（239+10），生产烟测过（PNG/区间/无基线，烟测数据已清）。commit `d2b76da`。
声音证据服务 · Claude 工具组显示名 **“我只听你”** · 总原则：**证据给我，意思我自己听。**

**定位（宿主用户 2026-09-29）：oído 是插件。** 不拥有账号、登录、凭据、人格——身份与认证永远由宿主提供；测试在宿主已登录的环境里跑，永不搬运凭据。

- 实施基线：`docs/pack/Oido_Architecture_owner_v1.1.md` + `docs/pack/Oido_Execution_GLM_v1.1.md`（OIDO-ARCH/EXEC-1.1），决策 `docs/pack/AUDITORY_DECISIONS.md`（D01–D10 锁定）。
- 2026-10-22（分支 `voice-joint-20261022`，未部署）：联合语音批 `JOINT-VOICE-20261022`——最小完整深听落地：bearer 认证+access_audit、ffmpeg 隔离解码（audio_variants 首次生产写入）、韵律主链（F0/停顿/语速/能量-DP 低置信对齐+unaligned 降级/pypinyin 本调/utterance_median 相对半音）、投影层（schema 单源+字节预算 CI+cursor 快照绑定）、HTTP 全路由（ingest/analyses/overview/prosody/delete，ingest/offline_analysis 双 flag）、MCP stdio 六工具 v2 合同（audio_ref 口径；view/find/compare 如实 NOT_IMPLEMENTED）、0004 转写正本迁移。**239 测试绿**（基线 176+新 63）。真人语音/真实模型/真实宿主 NOT_EXECUTED。commit `409fcbe`。
- 当前状态：**WP00 + WP01 + WP03 的 P0 基础层完成**（评审定名，非"首期功能完成"——韵律谱/渲染/MCP/真实语音验收未落地）。Phase 0 四入口测试已武装（`evals/phase0/PROTOCOL.md`）。诚实清单见 `docs/IMPLEMENTATION_REPORT.md` 与 `acceptance.json`。

- 2026-10-23（分支 `audit-fix-joint-voice-20261023` 叠加，未部署）：R6′ 生命周期闭环（她批 R6P 方案）：分析缓存键纳入版本成分（PIPELINE_METHOD_VERSION + 转写内容 hash——同源新版本=新分析，旧错结果不命中；操作幂等键 409 语义不变）；`__main__` 启动进程内串行执行器线程（此前 wait_ms=0 只落 queued 无消费者、正值内联跑完不守期限——现 EXECUTOR_ACTIVE 下 HTTP wait_ms=真实轮询上限，到限返回票据）；`POST /v1/analyses/{id}/cancel`（DB fence；阶段间检查点停推进，在途计算段不可抢占如实声明）；find 截断带 next_cursor（绑定 analysis+revision+query hash）；compare 结果预算门 compare_result=2048B（新键=收紧）；HTTP find/compare 入收取紧（pydantic extra=forbid）。275 绿。
## 快速开始

```bash
.venv/bin/python scripts/supervise_tests.py --self-test     # 监督器自测（先跑这个）
.venv/bin/python scripts/supervise_tests.py --timeout-seconds 180 --rss-limit-mib 1024 \
  -- .venv/bin/python -m pytest -x -q tests/unit/test_timebase.py
```

## 边界（不可被实现便利改写）

- 不生成/存储/注入情绪分类标签；只报可核验声学量与带时间的声音事件。
- 私密原声只在 Mac mini 本地分析；不新增云端音频推理；无 remote fallback。
- 不常驻偷听；正式录音默认保留、owner 随时可删。
- 工具不接受 URL/本机路径；URL 抓取只存在于 Siren 边界（经 SSRF 加固）。
- 普通话字调与句调分字段；不确定就 uncertain。
