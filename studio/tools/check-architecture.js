'use strict';
// 前端结构护栏：node tools/check-architecture.js
// 校验目标（对齐 apps/desktop 的 check:architecture 纪律）：
//   1. 目录分层：contracts / lib 不得依赖 ui / app / features
//   2. 模块体量：单文件行数上限，超限说明该拆了
//   3. 导入完整性：相对路径存在，且被导入的名字确实被目标 export
//   4. 入口收敛：index.html 只加载 styles/index.css 与 src/app/boot.js
// 退出码 0 = 通过，1 = 有违规。

const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

const ROOT = path.join(__dirname, '..');
const SRC = path.join(ROOT, 'src');
const STYLES = path.join(ROOT, 'styles');

const LIMITS = {
  js: 320,          // 单模块行数上限
  css: 300,         // 单个样式分片行数上限
};
const LAYERS = ['contracts', 'lib', 'ui', 'app', 'features'];
const FORBIDDEN = {
  // 层 -> 不允许依赖的层
  contracts: ['lib', 'ui', 'app', 'features'],
  lib: ['ui', 'app', 'features'],
  ui: ['app', 'features'],
  app: [],          // app 是装配层，可以依赖任何下层
  features: [],     // 功能页之间允许互相引用（弹窗/跳转），但不得反向依赖 app 的具体实现之外的私有物
};

const problems = [];
const rel = (p) => path.relative(ROOT, p).replace(/\\/g, '/');

function walk(dir, ext, out = []) {
  for (const name of fs.readdirSync(dir)) {
    const full = path.join(dir, name);
    const st = fs.statSync(full);
    if (st.isDirectory()) walk(full, ext, out);
    else if (full.endsWith(ext)) out.push(full);
  }
  return out;
}

// ---- 1. 收集模块的 import/export ----
const files = walk(SRC, '.js');
const exportsOf = new Map();
const importsOf = new Map();

for (const file of files) {
  const txt = fs.readFileSync(file, 'utf8');
  const ex = new Set();
  for (const m of txt.matchAll(/export\s*\{([^}]*)\}/g)) {
    for (const piece of m[1].split(',')) {
      const name = piece.trim().split(/\s+as\s+/).pop();
      if (name) ex.add(name);
    }
  }
  exportsOf.set(file, ex);

  const imps = [];
  for (const m of txt.matchAll(/import\s*\{([^}]*)\}\s*from\s*['"]([^'"]+)['"]/g)) {
    const names = m[1].split(',').map((s) => s.trim().split(/\s+as\s+/)[0].trim()).filter(Boolean);
    imps.push({ names, spec: m[2] });
  }
  importsOf.set(file, imps);
}

// ---- 2. 导入完整性与分层 ----
const layerOf = (file) => {
  const first = path.relative(SRC, file).split(path.sep)[0];
  return LAYERS.includes(first) ? first : 'root';
};

for (const [file, imps] of importsOf) {
  const fromLayer = layerOf(file);
  for (const { names, spec } of imps) {
    if (!spec.startsWith('.')) continue; // 只用相对路径，避免误判
    const target = path.resolve(path.dirname(file), spec);
    if (!fs.existsSync(target)) {
      problems.push(`${rel(file)}: 找不到导入目标 ${spec}`);
      continue;
    }
    const targetLayer = layerOf(target);
    if (fromLayer !== 'root' && (FORBIDDEN[fromLayer] || []).includes(targetLayer)) {
      problems.push(`${rel(file)}: ${fromLayer} 层不得依赖 ${targetLayer} 层（${spec}）`);
    }
    const ex = exportsOf.get(target) || new Set();
    const missing = names.filter((n) => !ex.has(n));
    if (missing.length) {
      problems.push(`${rel(file)}: ${spec} 未导出 ${missing.join(', ')}`);
    }
  }
}

// ---- 2b. 语法：ESM 只能用 node --check 检查 .mjs，逐文件复制到临时目录 ----
const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ac-syntax-'));
for (const file of files) {
  const copy = path.join(tmpDir, path.basename(file, '.js') + '-' + Math.random().toString(36).slice(2) + '.mjs');
  fs.copyFileSync(file, copy);
  try {
    // Windows 上偶发 EAGAIN/文件占用会让 spawn 失败，重试一次再判定
    try {
      execFileSync(process.execPath, ['--check', copy], { stdio: 'pipe' });
    } catch (first) {
      execFileSync(process.execPath, ['--check', copy], { stdio: 'pipe' });
    }
  } catch (e) {
    const msg = String((e.stderr && e.stderr.toString()) || e.message || e.status || '')
      .split('\n').slice(0, 5).join(' ').trim();
    problems.push(`${rel(file)}: 语法错误 ${msg || '（node --check 退出码 ' + e.status + '）'}`);
  } finally {
    fs.rmSync(copy, { force: true });
  }
}
fs.rmSync(tmpDir, { recursive: true, force: true });

// ---- 3. 体量 ----
for (const file of files) {
  const lines = fs.readFileSync(file, 'utf8').split('\n').length;
  if (lines > LIMITS.js) problems.push(`${rel(file)}: ${lines} 行，超过模块上限 ${LIMITS.js}`);
}
if (fs.existsSync(STYLES)) {
  for (const file of walk(STYLES, '.css')) {
    const lines = fs.readFileSync(file, 'utf8').split('\n').length;
    if (lines > LIMITS.css) problems.push(`${rel(file)}: ${lines} 行，超过样式分片上限 ${LIMITS.css}`);
  }
}

// ---- 4. 入口收敛 ----
const html = fs.readFileSync(path.join(ROOT, 'index.html'), 'utf8');
const scripts = [...html.matchAll(/<script[^>]*src="([^"]+)"/g)].map((m) => m[1]);
const links = [...html.matchAll(/<link[^>]*rel="stylesheet"[^>]*href="([^"]+)"/g)].map((m) => m[1]);
for (const s of scripts) {
  if (s !== 'src/app/boot.js' && s !== 'boot-theme.js') problems.push(`index.html: 额外脚本入口 ${s}`);
}
for (const l of links) {
  if (l !== 'styles/index.css') problems.push(`index.html: 额外样式入口 ${l}`);
}

// ---- 输出 ----
const jsCount = files.length;
const cssCount = fs.existsSync(STYLES) ? walk(STYLES, '.css').length : 0;
if (problems.length) {
  console.log(`架构检查未通过（${problems.length} 项）：`);
  for (const p of problems) console.log('  - ' + p);
  process.exit(1);
}
console.log(`架构检查通过：${jsCount} 个 JS 模块 / ${cssCount} 个样式分片，分层、导入、体量、入口均合规。`);
