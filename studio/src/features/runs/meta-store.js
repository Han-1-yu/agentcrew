/**
 * B3：runMeta 本地存储（settings.json 的 runMeta 键）。
 *
 * conversation = run 的 1:1 别名层：alias / pinned / archived 是纯前端元数据，
 * 存 settings.json（主进程白名单已放行 runMeta 键）。读改写统一走这里：
 * 乐观更新 → setSettings；写失败回滚内存态并重渲染（沿用 cycleTheme 的
 * 乐观更新 + 回滚模式），避免「看着改了、重启复原」。
 */
import { nextRunMeta, normalizeRunMeta, pruneRunMeta } from '../../lib/run-meta.js';
import { S } from '../../lib/store.js';

import { toast } from '../../ui/toast.js';

async function writeRunMeta(next) {
  const prev = S.settings && S.settings.runMeta;
  S.settings.runMeta = next;              // 乐观更新（渲染层读 S.settings.runMeta）
  try {
    await window.studio.setSettings({ runMeta: next });
    return true;
  } catch (e) {
    S.settings.runMeta = prev;            // 回滚
    toast('error', '保存失败', String((e && e.message) || e));
    return false;
  }
}

/** 设置 / 清除一条 run 的元数据字段（patch 只带要改的字段）。 */
async function patchRunMeta(runId, patch) {
  if (!runId) return false;
  return writeRunMeta(nextRunMeta(S.settings && S.settings.runMeta, runId, patch));
}

/** deleteRun 之后裁掉幽灵键，避免幽灵元数据。 */
async function pruneRunMetaAfterDelete(runIds) {
  return writeRunMeta(pruneRunMeta(S.settings && S.settings.runMeta, runIds));
}

/** 读当前内存里的 runMeta（容错归一化，渲染层直接用）。 */
function currentRunMeta() {
  return normalizeRunMeta(S.settings && S.settings.runMeta);
}

export { currentRunMeta, patchRunMeta, pruneRunMetaAfterDelete };
