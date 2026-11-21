/**
 * C1：启动编排 —— REST POST /runs 优先，CLI 兜底，绝不双发。
 *
 * composer 直发（A2）与新建运行弹窗共用同一条编排：已连 API（地址 + Key）
 * 且模式可映射时走 REST（返回 run_id，按目录定位后切换）；401/403/400/503
 * 等服务端明确答复才降级 CLI；超时/断连视为「结果未知」，绝不自动重发。
 * ask（--plan-only）与 demo 在 REST 白名单里没有等价键，恒走 CLI。
 */
import { S } from '../../lib/store.js';
import { toast } from '../../ui/toast.js';

import { switchRun } from '../runs/index.js';
import { isTransportError, createRunViaRest, locateAndSwitchRestRun, restCapable } from './rest-launch.js';
import { waitForStartedRun } from './run-launch.js';

/** 附件 path 数组 → POST /runs 的 images 形状（base64 内联）。 */
async function toRestImages(imageRefs) {
  if (!Array.isArray(imageRefs) || !imageRefs.length) return [];
  const out = [];
  for (const ref of imageRefs) {
    const path = typeof ref === 'string' ? ref : ref && ref.path;
    if (!path) continue;
    const r = await window.studio.readImageFiles([path]);
    if (!r || !r.ok) throw new Error(String((r && r.error) || '附件读取失败'));
    const img = r.images[0];
    if (img) out.push({ mime: img.mime, data: img.dataBase64 });
  }
  return out;
}/**
 * 启动一次运行。
 * @param {object} p { goal, demo, mock, mode, project, model, maxCost,
 *                     maxLlmCalls, images(路径数组), slow }
 * @returns {'rest'|'rest-unknown'|'cli'}
 */
async function launchRun(p) {
  const images = Array.isArray(p.images) ? p.images : null;

  if (!p.demo && restCapable() && p.mode !== 'ask') {
    try {
      const restImages = images ? await toRestImages(images) : [];
      const payload = await createRunViaRest({
        goal: p.goal, mode: p.mode, mock: p.mock, model: p.model,
        maxCost: p.maxCost, maxLlmCalls: p.maxLlmCalls,
        flowName: p.flowName, images: restImages,
      });
      const runId = payload && payload.run_id;
      toast('success', '已启动（API）', `run ${runId} 已在服务端创建`);
      const dir = await locateAndSwitchRestRun(runId, { switchRun });
      if (!dir) {
        toast('info', '已启动，待同步', '暂未在本地输出目录发现该 run，请在运行列表手动切换');
      }
      return 'rest';
    } catch (err) {
      if (isTransportError(err)) {
        // 请求结果未知（可能已创建）：不降级、不重试，明确告知。
        toast('error', '启动结果未知',
          'API 请求超时，run 可能已在服务端创建；请在运行列表确认，不要重复发送');
        return 'rest-unknown';
      }
      toast('info', '已改用本地 CLI 启动',
        `API 通道不可用（${String((err && (err.detail || err.message)) || err)}），正在降级`);
    }
  }

  // CLI 通道：目录发现走 proc:run-dir + 低频扫描兜底（waitForStartedRun）。
  // C-6：快照必须在 startRun 之前取，否则会把新 run 算进 before。
  let before = new Set();
  const previousDir = S.watchDir;
  try { before = new Set(((await window.studio.listRuns()) || []).map((x) => x.dir)); } catch { /* 不判重 */ }
  const started = await window.studio.startRun({
    goal: p.goal,
    demo: !!p.demo,
    mock: !!p.mock,
    mode: p.mode,
    project: p.project || null,
    model: p.model || null,
    maxCost: p.maxCost || 0,
    maxLlmCalls: p.maxLlmCalls || 0,
    flowName: p.flowName || null,
    images: images || null,
    env: (p.mock && p.slow) ? { AGENTCREW_MOCK_LATENCY_MS: '2000' } : null,
  });
  toast('success', '已启动', p.mock ? 'Mock 运行已提交…' : '真实模型运行已提交，耗时数分钟…');
  waitForStartedRun({ key: started && started.key, before, previousDir }).then((result) => {
    if (result.timedOut) toast('info', '已启动', '未检测到新的 run 目录，请在运行列表中手动切换');
    else if (result.error) toast('error', '切换失败', String(result.error.message || result.error));
  });
  return 'cli';
}

export { launchRun };
