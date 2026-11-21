/**
 * 新建运行弹窗的供应商下拉：按「大模型供应商 / Agent 智能体」分组。
 * 单独成文件是为了让 new-run 主流程留在 320 行上限内，也便于 e2e 直接断言分组。
 */
import { findProvider } from '../../contracts/providers.js';

import { h } from '../../lib/dom.js';

/** 把一组 `{key,label,type}` 填进 select；internal（mock）由调用方先过滤。 */
function fillProviderOptions(select, list) {
  select.innerHTML = '<option value="">自动（设置默认）</option>';
  const groups = [
    ['大模型供应商', (list || []).filter(x => x.type !== 'agent')],
    ['Agent 智能体', (list || []).filter(x => x.type === 'agent')],
  ];
  for (const [label, items] of groups) {
    if (!items.length) continue;
    select.appendChild(h('optgroup', { label },
      ...items.map(x => h('option', { value: x.key }, x.label))));
  }
}

/** 后端 /providers 只给名字时，用内置目录补 label/type，并丢掉 mock。 */
function normalizeApiProviders(list) {
  return (list || [])
    .map((p) => {
      const preset = findProvider(p.name);
      return {
        key: p.name,
        label: (preset && preset.label) || p.name,
        type: preset ? preset.type : 'llm',
      };
    })
    .filter(p => p.type !== 'internal');
}

export { fillProviderOptions, normalizeApiProviders };
