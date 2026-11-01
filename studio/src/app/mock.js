/**
 * 演示数据入口：对上层保持稳定的导入路径。
 * 数据装配在 fixtures.js，状态推演在 simulator.js。
 */
export {
  RUN_ID,
  T,
  buildMockAgents,
  buildMockEvents,
  buildMockGates,
  buildMockRun,
  buildMockTasks,
  ev,
  initMockData,
  nowTs,
} from './mock/fixtures.js';

export { applyDemoState, pushEvent, simTick, startSim, stopSim } from './mock/simulator.js';
