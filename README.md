# oído

声音证据服务 · Claude 工具组显示名 **“我只听你”** · 总原则：**证据给我，意思我自己听。**

- 实施基线：`docs/pack/Oido_Architecture_owner_v1.1.md` + `docs/pack/Oido_Execution_GLM_v1.1.md`（OIDO-ARCH/EXEC-1.1），决策 `docs/pack/AUDITORY_DECISIONS.md`（D01–D10 锁定）。
- 当前状态：**WP00 + WP01 + WP03 的 P0 基础层完成**（评审定名，非"首期功能完成"——韵律谱/渲染/MCP/真实语音验收未落地）。Phase 0 四入口测试已武装（`evals/phase0/PROTOCOL.md`）。诚实清单见 `docs/IMPLEMENTATION_REPORT.md` 与 `acceptance.json`。

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
