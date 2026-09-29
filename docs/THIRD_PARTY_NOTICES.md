# 第三方声明

## 已安装依赖（requirements.lock 全量锁定；许可按包元数据）

- numpy (BSD-3) · scipy (BSD-3) · librosa (ISC) · soundfile (BSD-3, 捆绑 libsndfile LGPL)
- fastapi (MIT) · uvicorn (BSD-3) · pydantic (MIT) · starlette (BSD-3)
- httpx (BSD-3) · jsonschema (MIT) · pytest (MIT) 及其传递依赖

以上为工程库，非音频推理模型；无任何模型权重入库。仓库许可说明 ≠ 权重许可：未来每个模型 provider 安装前必须在 `PROVIDER_MATRIX.md` 单独记录代码许可与权重条款（EXEC §7.1）。

## 仅研究引用（未安装、未复制）

R01–R20 清单见 `docs/pack/Oido_Architecture_owner_v1.1.md` 附录 A。特别地：`zachswift615/audio-analysis-mcp` 未确认许可，只参考思路；其 `pyin` 未传 sr 的缺陷（R02）已转化为本仓 P0 防回归测试。
