#!/usr/bin/env node
// Phase 0 · estómago 入口探针（OIDO-EXEC WP09 / E02）
//
// 审计 F09 修复：此前 pngB64 读而未用、query 只收到字符串——本探针现在
// 通过 SDK 的 AsyncIterable<SDKUserMessage> 输入发送标准 Messages API
// content blocks（image base64 + text），图片真实进入模型上下文。
// 审计 F10 修复：依赖解析器提升到模块作用域，收到回答后不再 ReferenceError。
//
// 纪律：
//   - 独立临时 ESTOMAGO_HOME（绝不指向生产 ~/Library/Application Support/estomago）
//   - settingSources: [] 关闭人格加载；tools: [] 禁用全部内置工具（审计补充点6）
//   - cwd 指向临时目录：主体与仓库/secrets 隔离，随机码只能经图片通道进入
//   - OIDO_PHASE0_AUTHORIZED=1 才发起真实调用（用户显式授权纪律）
//
// 凭据铁律（宿主用户 2026-09-29 裁定）：零凭据搬运——不读、不复制、不引用任何
// 登录信息（~/.claude、API key、钥匙串一律不碰）。认证只来自运行时所在环境的
// 既有登录态；环境没登录就让它失败，不为绿灯搬凭据。
//
// 运行（在宿主用户本人已登录的普通终端里，无需任何额外配置）：
//   cd ~/Projects/oido && OIDO_PHASE0_AUTHORIZED=1 node scripts/phase0_probe_estomago.mjs
//   .venv/bin/python scripts/phase0_verify.py --entry estomago \
//       --answer-file evals/phase0/answers/estomago.raw.txt

import { createRequire } from 'node:module';
import { mkdtempSync, writeFileSync, mkdirSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const ESTOMAGO_PKG = '/Users/owner/Projects/estomago/package.json';
const FIXTURE = path.join(ROOT, 'evals/phase0/fixtures/estomago.png');
const ANSWER_DIR = path.join(ROOT, 'evals/phase0/answers');
const PROMPT = '这是一张随机码测试图。请只输出图中显示的随机码，逐字符原样复述，不要添加其他内容。';

const authorized = process.env.OIDO_PHASE0_AUTHORIZED === '1';
console.log(`[probe:estomago] authorized=${authorized}`);
console.log(`[probe:estomago] fixture=${path.relative(ROOT, FIXTURE)}`);
if (!authorized) {
  console.log('[probe:estomago] BLOCKED：需宿主用户显式授权（OIDO_PHASE0_AUTHORIZED=1）+ 已登录环境。不发起真实调用。');
  process.exit(2);
}

// F10：解析器在模块作用域，供两处使用
const estRequire = createRequire(ESTOMAGO_PKG);

// 临时 ESTOMAGO_HOME：estómago 生产数据隔离 + 探针 cwd（与仓库 secrets 隔离）
const tempHome = mkdtempSync(path.join(tmpdir(), 'oido-phase0-est-'));
process.env.ESTOMAGO_HOME = tempHome;
console.log(`[probe:estomago] temp ESTOMAGO_HOME=${tempHome}`);

let sdk;
try {
  const sdkPath = estRequire.resolve('@anthropic-ai/claude-agent-sdk');
  sdk = await import(pathToFileURL(sdkPath).href);
  console.log('[probe:estomago] sdk loaded from estomago node_modules');
} catch (err) {
  console.log(`[probe:estomago] FAILED：无法解析 estómago 的 claude-agent-sdk（${err.message}）`);
  process.exit(3);
}

const pngB64 = readFileSync(FIXTURE).toString('base64');
mkdirSync(ANSWER_DIR, { recursive: true });

try {
  const query = sdk.query ?? sdk.default?.query;
  if (typeof query !== 'function') throw new Error('SDK 未暴露 query()（版本合同变化，需人工核验）');

  // F09：图片真实进入模型——SDKUserMessage 数组异步迭代器 + image content block
  async function* input() {
    yield {
      type: 'user',
      message: {
        role: 'user',
        content: [
          {
            type: 'image',
            source: { type: 'base64', media_type: 'image/png', data: pngB64 },
          },
          { type: 'text', text: PROMPT },
        ],
      },
      parent_tool_use_id: null,
    };
  }

  const q = query({
    prompt: input(),
    options: {
      settingSources: [],     // 无人格/设置加载
      tools: [],              // 禁用全部内置工具（含 Read/Bash）
      cwd: tempHome,          // 与仓库及 secrets 隔离
      maxTurns: 1,
    },
  });
  let text = '';
  let sawImageAck = false;
  for await (const message of q) {
    if (message.type === 'user' && message.isSynthetic) sawImageAck = true;
    if (message.type === 'assistant') {
      for (const block of message.message?.content ?? []) {
        if (block.type === 'text') text += block.text;
      }
    }
  }
  writeFileSync(path.join(ANSWER_DIR, 'estomago.raw.txt'), text);
  let sdkVersion = 'unknown';
  try {
    sdkVersion = estRequire('@anthropic-ai/claude-agent-sdk/package.json').version;
  } catch { /* 版本读取失败不掩盖主结果 */ }
  writeFileSync(path.join(ANSWER_DIR, 'estomago.meta.json'), JSON.stringify({
    entry: 'estomago', sdk_version: sdkVersion, temp_home: tempHome,
    transport: 'claude-agent-sdk query(AsyncIterable<SDKUserMessage>) with image content block',
    image_block_sent: true, synthetic_user_seen: sawImageAck,
    prompt_chars: PROMPT.length, png_base64_chars: pngB64.length,
  }, null, 2));
  console.log('[probe:estomago] DONE：回答已写入 evals/phase0/answers/estomago.raw.txt（不回显）。用 phase0_verify.py 校验。');
} catch (err) {
  console.log(`[probe:estomago] ERROR：${err.message?.slice(0, 200)}`);
  process.exit(4);
}
