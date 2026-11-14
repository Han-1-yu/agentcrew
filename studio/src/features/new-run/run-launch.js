/* 新建运行后的目录发现：优先监听主进程 proc:run-dir，低频扫描仅作兜底。 */
import { S } from '../../lib/store.js';

import { switchRun } from '../runs/index.js';

function waitForStartedRun({ key, before, previousDir, timeoutMs = 60000, pollMs = 1500 }) {
  return new Promise((resolve) => {
    let settled = false;
    let off = null;
    let timeoutTimer = null;
    let pollTimer = null;

    const cleanup = () => {
      try { if (off) off(); } catch { /* 已取消 */ }
      off = null;
      if (timeoutTimer) { clearTimeout(timeoutTimer); timeoutTimer = null; }
      if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    };

    const finish = async (runDir, timedOut = false) => {
      if (settled) return;
      settled = true;
      cleanup();
      if (!runDir) { resolve({ switched: false, timedOut }); return; }
      // 等待期间用户亲自切了 run：不要用迟到的目录把用户强行拉走。
      if (S.watchDir !== previousDir) { resolve({ switched: false, canceled: true }); return; }
      try {
        await switchRun(runDir);
        resolve({ switched: true, runDir });
      } catch (error) {
        resolve({ switched: false, error });
      }
    };

    try {
      off = window.studio.on('proc:run-dir', (payload) => {
        if (settled || !payload || !payload.runDir) return;
        if (key && payload.key !== key) return;
        void finish(payload.runDir);
      });
    } catch { /* 非 Electron 环境走扫描兜底 */ }

    const poll = async () => {
      if (settled) return;
      if (S.watchDir !== previousDir) { await finish(null); return; }
      try {
        const runs = (await window.studio.listRuns()) || [];
        const fresh = runs.find(run => !before.has(run.dir));
        if (fresh) { await finish(fresh.dir); return; }
      } catch { /* 网络/磁盘抖动时下轮再试 */ }
      if (!settled) pollTimer = setTimeout(poll, pollMs);
    };
    pollTimer = setTimeout(poll, Math.min(900, pollMs));
    timeoutTimer = setTimeout(() => void finish(null, true), timeoutMs);
  });
}

export { waitForStartedRun };
