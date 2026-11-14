/* 看板装配：一次构建图模型，再组合状态条 / DAG / 协作 / 门禁 / 成本 / 事件。 */
import { S } from '../../lib/store.js';

import { renderChrome } from '../../app/shell.js';

import { renderAgents } from './agents.js';
import { renderCostPanel } from './cost.js';
import { renderEvents } from './events.js';
import { renderGates } from './gates.js';
import { renderRunStatusBar } from './status-bar.js';
import { buildDagModel, renderDAG, renderDAGFallback } from './dag.js';

function renderDashboard() {
  if (!S.dash) return;
  const tasks = S.dash.tasks || [];
  const dagModel = buildDagModel(tasks);
  renderRunStatusBar();
  renderDAG(dagModel);
  renderDAGFallback(dagModel);
  renderAgents();
  renderGates();
  renderCostPanel();
  renderEvents();
  renderChrome();
}

export { renderDashboard };
