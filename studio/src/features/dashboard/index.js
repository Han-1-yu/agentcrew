/**
 * 看板装配：组合 DAG / 协作 / 门禁 / 成本 / 事件五个区块。
 */
import { S } from '../../lib/store.js';

import { renderChrome } from '../../app/shell.js';

import { renderAgents } from './agents.js';
import { renderCostPanel } from './cost.js';
import { renderEvents } from './events.js';
import { renderGates } from './gates.js';
import { renderRunStatusBar } from './status-bar.js';
import { renderDAG, renderDAGFallback } from './dag.js';
function renderDashboard(){
  if(!S.dash)return;
  renderRunStatusBar(); renderDAG(); renderDAGFallback();
  renderAgents(); renderGates(); renderCostPanel(); renderEvents(); renderChrome();
}

export { renderDashboard };
