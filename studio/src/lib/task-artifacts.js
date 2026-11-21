/**
 * F2：任务抽屉的输入 / 输出产物摘要（纯函数）。
 *
 * 数据优先级：
 * 1. `MANIFEST.json` 的 entries（带 `produced_by_task`）——真实 run 的权威来源，
 *    还能分辨「契约要求了但没产出」；
 * 2. 任务上的 `result.artifacts`——mock / 旧版 run 只有这一层；
 * 3. 契约 `deliverables`——只用来算「应产出」，不能当成已产出。
 *
 * 注意 `result.artifacts` 在真实 `tasks.json` 里可能是**字符串**（历史序列化产物，
 * 值是一串空白），旧抽屉用 `.length` 判断会把它当成 1 个产物再 `a.path` 取到
 * undefined。这里一律要求数组，非数组当没有。
 */

/** 契约要求产出的路径清单（`deliverables` 的元素可能是字符串或 {path}）。 */
function expectedPaths(task) {
  return (task && task.deliverables || [])
    .map((d) => (typeof d === 'string' ? d : d && d.path))
    .filter(Boolean);
}

/** `result.artifacts` 归一成 `[{path, sizeBytes}]`；非数组返回空。 */
function resultArtifacts(task) {
  const raw = task && task.result && task.result.artifacts;
  if (!Array.isArray(raw)) return [];
  return raw
    .map((item) => (typeof item === 'string' ? { path: item } : item))
    .filter((item) => item && item.path)
    .map((item) => ({ path: item.path, sizeBytes: item.size_bytes ?? item.sizeBytes }));
}

/** MANIFEST entries（由 main.js 归一，字段是 `task` / `sizeBytes`）按任务切分。 */
function entriesFor(taskId, entries) {
  return (entries || [])
    .filter((e) => e && e.task === taskId)
    .map((e) => ({ path: e.path, sizeBytes: e.sizeBytes }));
}

/**
 * 汇总一个任务的输入 / 输出。
 * @param {object} task 任务对象
 * @param {Array} tasks  全量任务
 * @param {Array} entries MANIFEST entries（可为空）
 * @returns {{outputs:{files:Array,source:string}, inputs:{files:Array,from:Array},
 *            expected:string[], missing:string[], pending:boolean}}
 */
function summarizeTaskArtifacts(task, tasks, entries) {
  const pending = ['pending', 'ready', 'running', 'rework'].includes(task && task.status);
  const manifestOutputs = entriesFor(task && task.id, entries);
  const source = manifestOutputs.length ? 'manifest' : 'result';
  const outputs = manifestOutputs.length ? manifestOutputs : resultArtifacts(task);

  const depIds = (task && task.dependencies) || [];
  const from = depIds
    .map((id) => (tasks || []).find((t) => t.id === id))
    .filter(Boolean)
    .map((t) => ({ id: t.id, title: t.title || t.id }));

  const inputFiles = [];
  for (const id of depIds) {
    const byManifest = entriesFor(id, entries);
    const depTask = (tasks || []).find((t) => t.id === id);
    const files = byManifest.length ? byManifest : resultArtifacts(depTask);
    for (const f of files) {
      if (!inputFiles.some((x) => x.path === f.path)) inputFiles.push(f);
    }
  }

  const expected = expectedPaths(task);
  // 未完成的任务「缺文件」是正常的（还没写），不报缺失，只标「待产出」
  const missing = pending
    ? []
    : expected.filter((p) => !outputs.some((o) => o.path === p));

  return {
    outputs: { files: outputs, source },
    inputs: { files: inputFiles, from },
    expected,
    missing,
    pending,
  };
}

export { entriesFor, expectedPaths, resultArtifacts, summarizeTaskArtifacts };
