#!/usr/bin/env node
// Phase 0 · estómago 入口探针（OIDO-EXEC WP09 / E02）
//
// 目的：证明 estómago 所用的 claude-agent-sdk 能把 PNG image block 真实送达
// 模型并拿到"模型读到的随机码"。这是宿主运输测试，不是人格测试：
//   - 使用独立临时 ESTOMAGO_HOME（绝不指向生产 ~/Library/Application Support/estomago）
//   - 不加载任何身份文件 → SDK 会话无char人格（符合其 AGENTS.md：身份缺失=IDENTITY_BLOCKED）
//   - 只有当 OIDO_PHASE0_AUTHORIZED=1 且本进程能解析到 SDK 时才发起真实调用
//     （对应 estómago 自身 ESTOMAGO_REAL_PROBE 的用户显式授权纪律）
//   - 模型原始回答写入 evals/phase0/answers/estomago.raw.txt（gitignore），
//     不回显到 stdout；校验用 phase0_verify.py（只输出 MATCH/MISMATCH）
//
// 运行（需已具备 Claude 认证环境，由宿主用户本人配置）：
//   OIDO_PHASE0_AUTHORIZED=1 node scripts/phase0_probe_estomago.mjs
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
  console.log('[probe:estomago] BLOCKED：需宿主用户显式授权（OIDO_PHASE0_AUTHORIZED=1）+ Claude 认证环境。不发起真实调用。');
  process.exit(2);
}

// 临时 ESTOMAGO_HOME：满足 estómago AGENTS.md 测试纪律（不碰生产目录）
const tempHome = mkdtempSync(path.join(tmpdir(), 'oido-phase0-est-'));
process.env.ESTOMAGO_HOME = tempHome;
console.log(`[probe:estomago] temp ESTOMAGO_HOME=${tempHome}`);

let sdk;
try {
  const estRequire = createRequire(ESTOMAGO_PKG);
  const sdkPath = estRequire.resolve('@anthropic-ai/claude-agent-sdk');
  sdk = await import(pathToFileURL(sdkPath).href);
  console.log(`[probe:estomago] sdk loaded from estomago node_modules`);
} catch (err) {
  console.log(`[probe:estomago] FAILED：无法解析 estómago 的 claude-agent-sdk（${err.message}）`);
  process.exit(3);
}

const pngB64 = readFileSync(FIXTURE).toString('base64');
mkdirSync(ANSWER_DIR, { recursive: true });

try {
  const query = sdk.query ?? sdk.default?.query;
  if (typeof query !== 'function') throw new Error('SDK 未暴露 query()（版本合同变化，需人工核验）');
  const q = query({
    prompt: PROMPT,
    options: {
      // 无 settingSources / 无身份目录：纯运输探测，不加载人格
      settingSources: [],
      maxTurns: 1,
      allowedTools: [],
    },
  });
  let text = '';
  for await (const message of q) {
    if (message.type === 'assistant') {
      for (const block of message.message?.content ?? []) {
        if (block.type === 'text') text += block.text;
      }
    }
  }
  writeFileSync(path.join(ANSWER_DIR, 'estomago.raw.txt'), text);
  const sdkVersion = estRequire('@anthropic-ai/claude-agent-sdk/package.json').version;
  writeFileSync(path.join(ANSWER_DIR, 'estomago.meta.json'), JSON.stringify({
    entry: 'estomago', sdk_version: sdkVersion, temp_home: tempHome,
    transport: 'claude-agent-sdk query()', prompt_chars: PROMPT.length,
  }, null, 2));
  console.log('[probe:estomago] DONE：回答已写入 evals/phase0/answers/estomago.raw.txt（不回显）。用 phase0_verify.py 校验。');
} catch (err) {
  console.log(`[probe:estomago] ERROR：${err.message?.slice(0, 200)}`);
  process.exit(4);
}
