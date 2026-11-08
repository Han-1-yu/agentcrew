'use strict';
// 状态切换探针的宿主（无 GUI 窗口）。
//
// 做法：临时生成一份基于 index.html 的测试页 —— 保留完整 DOM 骨架与原 CSP
// （渲染模块的顶层依赖都在），去掉 boot.js 入口（不启动真实应用 / 不读真实
// output），用外部脚本 mock 顶替 window.studio，再加载 client 跑断言。
//
// 用法（本会话需清掉宿主注入的 ELECTRON_RUN_AS_NODE）：
//   env -u ELECTRON_RUN_AS_NODE -u NODE_OPTIONS npm run verify:transitions

const fs = require('fs');
const path = require('path');
const { app, BrowserWindow } = require('electron');

const ROOT = path.join(__dirname, '..');
const TMP_HTML = path.join(ROOT, '.verify-transitions.html');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function run() {
  const src = fs.readFileSync(path.join(ROOT, 'index.html'), 'utf8');
  // mock 与 client 都是外部同源脚本，生产 CSP（default-src 'self'）原样保留，
  // 不需要移除 CSP / 放宽 unsafe-inline，也就没有 Electron 的安全警告。
  const html = src
    .replace(/<script type="module" src="src\/app\/boot\.js"><\/script>/i, '')
    .replace('</head>', '<script src="tools/verify-transitions-mock.js"></script>\n</head>')
    .replace('</body>', '<script type="module" src="tools/verify-transitions-client.js"></script>\n</body>');
  fs.writeFileSync(TMP_HTML, html, 'utf8');

  const errors = [];
  const win = new BrowserWindow({
    show: false, width: 1200, height: 800,
    webPreferences: { contextIsolation: true, nodeIntegration: false, backgroundThrottling: false },
  });
  // Electron 39 起事件参数是对象（旧签名 (event, level, message) 已弃用）。
  win.webContents.on('console-message', (details) => {
    if (details && typeof details === 'object' &&
        (details.level === 'error' || details.level === 'warning')) {
      errors.push(String(details.message || ''));
    }
  });
  await win.loadFile(TMP_HTML);

  let results = null;
  for (let i = 0; i < 80 && !results; i++) {
    try {
      results = await win.webContents.executeJavaScript(
        'window.__verifyDone ? window.__verifyResults : null');
    } catch { /* 页面还没就绪 */ }
    if (!results) await sleep(200);
  }

  win.destroy();
  try { fs.rmSync(TMP_HTML, { force: true }); } catch { /* 非致命 */ }

  if (!results) {
    console.error('FAIL  探针超时，页面未返回结果');
    for (const e of errors.slice(0, 10)) console.error('  页面错误：' + e);
    app.exit(1);
    return;
  }
  for (const c of results) {
    console.log(`${c.ok ? 'PASS' : 'FAIL'}  ${c.name}${c.ok ? '' : ' — ' + c.extra}`);
  }
  for (const e of errors.slice(0, 10)) console.log('WARN  页面控制台：' + e);
  const failed = results.filter((c) => !c.ok).length;
  console.log(`状态切换验证：${results.length - failed}/${results.length} 通过`);
  app.exit(failed ? 1 : 0);
}

app.whenReady().then(run).catch((e) => {
  console.error('验证脚本异常:', e);
  try { fs.rmSync(TMP_HTML, { force: true }); } catch { /* 非致命 */ }
  app.exit(1);
});
