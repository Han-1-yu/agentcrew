/**
 * 状态切换探针（在 verify-transitions.js 生成的测试页里跑）。
 *
 * 覆盖 e2e / selftest 碰不到的两条分支：
 *   1. 演示模式 → 首次真实运行（旧 bug：startSim 不停，污染真实数据）
 *   2. 快速连点两个 run（旧 bug：异步续体把新 run 状态覆盖掉）
 * 外加一条实时通道纪律：切 run 时 SSE 与文件监听必须同时停。
 *
 * window.studio 由测试页里的 inline mock 提供，见 verify-transitions.js。
 */
const results = [];
const check = (name, ok, extra) => results.push({
  name,
  ok: !!ok,
  extra: extra === undefined ? '' : JSON.stringify(extra),
});

async function run() {
  const { S } = await import('../src/lib/store.js');
  const { startSim } = await import('../src/app/mock.js');
  const { loadRealRun } = await import('../src/app/data.js');
  const { resetRealtime } = await import('../src/app/realtime.js');

  const A = 'C:\\runs\\A';
  const A2 = 'C:\\runs\\A2';
  const B2 = 'C:\\runs\\B2';
  S.settings = { apiBaseUrl: '' };   // 默认路径：CLI + 文件监听，不碰 HTTP
  S.realMode = false;
  S.watchDir = null;
  S.dash = null;

  // ── 1. 演示推演不能跟着进真实模式 ──
  startSim();
  check('演示推演定时器已启动', !!S.simTimer);
  const okA = await loadRealRun(A);
  check('loadRealRun 成功', okA === true, okA);
  check('进入真实模式后演示推演被停止', S.simTimer === null, S.simTimer);
  check('真实 run 未被演示逻辑改写',
    !!S.dash && S.dash.run.status === 'running' && S.dash.tasks.length === 0);

  // ── 2. 快速连点两个 run：旧请求必须被淘汰 ──
  window.__delays[A2] = 80;
  window.__delays[B2] = 0;
  const [rA, rB] = await Promise.all([loadRealRun(A2), loadRealRun(B2)]);
  check('旧请求被代次令牌淘汰', rA === false, rA);
  check('新请求生效', rB === true, rB);
  check('最终 watchDir 指向新 run', S.watchDir === B2, S.watchDir);
  check('最终数据来自新 run', !!S.dash && S.dash.run.run_id === B2, S.dash && S.dash.run.run_id);

  // ── 3. 实时通道纪律：切 run 时两边都要停 ──
  const mark = window.__calls.length;
  resetRealtime();
  const tail = window.__calls.slice(mark);
  check('resetRealtime 停掉 SSE', tail.some((c) => c[0] === 'stop'), tail);
  check('resetRealtime 停掉文件监听', tail.some((c) => c[0] === 'unwatch'), tail);
  check('SSE 不可用时回退文件监听', window.__calls.some((c) => c[0] === 'watch'));
}

run()
  .then(() => { window.__verifyResults = results; window.__verifyDone = true; })
  .catch((e) => {
    results.push({ name: '探针脚本异常', ok: false, extra: String((e && e.stack) || e) });
    window.__verifyResults = results;
    window.__verifyDone = true;
  });
