/**
 * C3（部分）：新建运行弹窗的流程下拉。
 *
 * 数据来自 GET /flows（已连 API 才有值，未连保持「不使用流程」单选项）；
 * 只列校验通过的流程（带错的流程起跑会被后端拒绝，提前藏掉）。
 * flowName 预选时自动展开高级折叠区，让「已选流程」可见。
 */
import { apiFetch } from '../../lib/api.js';
import { h } from '../../lib/dom.js';

async function initFlowOptions(sel, { flowName } = {}) {
  if (!sel) return;
  try {
    const data = await apiFetch('/flows');
    const flows = (data && data.flows) || [];
    for (const f of flows) {
      if (f && f.name && !(Number(f.errors) > 0)) {
        sel.appendChild(h('option', { value: f.name }, `${f.name}（${f.tasks || 0} 任务）`));
      }
    }
  } catch { /* 未连 API：保持「不使用流程」单选项 */ }
  if (flowName) {
    sel.value = String(flowName);
    const hint = document.getElementById('nr-flow-hint');
    if (hint && sel.selectedIndex <= 0) {
      hint.textContent = `流程 ${flowName} 不在流程库中（或校验有错），请检查`;
    }
    const adv = document.getElementById('nr-advanced');
    if (adv && sel.value) adv.classList.add('open');
  }
}

export { initFlowOptions };
