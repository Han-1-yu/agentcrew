'use strict';
// 状态切换探针的 window.studio 替身（普通脚本，在测试页 head 里先于 client 加载）。
// 只模拟默认路径（CLI + 文件监听）用到的 IPC，不启动任何真实进程或服务。
window.__calls = [];
window.__delays = {};
window.studio = {
  loadRun: async (dir) => {
    const d = window.__delays[dir] || 0;
    if (d) await new Promise((r) => setTimeout(r, d));
    return { run: { run_id: dir, status: 'running', stats: {}, config_snapshot: {} }, tasks: [] };
  },
  ledgerHistory: async () => ({ events: [] }),
  watchRun: async (dir) => { window.__calls.push(['watch', dir]); return { ok: true }; },
  unwatchRun: async (dir) => { window.__calls.push(['unwatch', dir]); return { ok: true }; },
  apiStreamStart: async () => ({ ok: false, status: 401 }),
  apiStreamStop: async () => { window.__calls.push(['stop']); return { ok: true }; },
  on: () => () => {},
};
