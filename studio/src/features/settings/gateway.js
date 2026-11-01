/**
 * 组 3「网关高级功能」：数据脱敏、路由策略、会话亲和性、配额入口。
 * 这些开关只有在落到主进程设置后才会影响 CLI 子进程，页面本身不做校验。
 */
import { h } from '../../lib/dom.js';

import { openQuotaModal } from './quota.js';

const ROUTING_STRATEGIES = [
  ['weighted', '权重随机'],
  ['rotate', '轮询'],
  ['usage', '最少使用'],
  ['order', '顺序'],
];

const AFFINITY_MODES = [
  ['off', '关闭'],
  ['session', '会话保持'],
  ['turn', '每轮可换'],
];

/** 一组互斥按钮：点击后写回 cfg 并切换 active。 */
function buildSegment(cfg, key, options, dataAttr, fallback) {
  const seg = h('div', { class: 'seg', id: `set-${key}` });
  const btns = {};
  options.forEach(([val, label]) => {
    const b = h('button', { type: 'button', [dataAttr]: val, onclick: () => {
      cfg[dataAttr === 'data-strategy' ? 'routingStrategy' : 'affinityMode'] = val;
      Object.values(btns).forEach((x) => x.classList.remove('active'));
      b.classList.add('active');
    } }, label);
    if ((cfg[dataAttr === 'data-strategy' ? 'routingStrategy' : 'affinityMode'] || fallback) === val) b.classList.add('active');
    btns[val] = b;
    seg.appendChild(b);
  });
  return seg;
}

function buildGatewayGroup(cfg) {
  // 脱敏：总开关关闭时，其余子项与自定义词一并禁用
  const redactEnabled = h('input', { id: 'set-redact-enabled', type: 'checkbox', checked: cfg.redactEnabled !== false });
  const redactApiKeys = h('input', { id: 'set-redact-apikeys', type: 'checkbox', checked: cfg.redactApiKeys !== false });
  const redactPii = h('input', { id: 'set-redact-pii', type: 'checkbox', checked: cfg.redactPii !== false });
  const redactCredentials = h('input', { id: 'set-redact-creds', type: 'checkbox', checked: cfg.redactCredentials !== false });
  const redactCustom = h('input', { id: 'set-redact-custom', class: 'set-input', type: 'text', value: cfg.redactCustomWords || '', placeholder: '逗号分隔，如：内部代号,项目名' });
  const updateRedactState = () => {
    const on = redactEnabled.checked;
    [redactApiKeys, redactPii, redactCredentials, redactCustom].forEach((el) => { el.disabled = !on; });
  };
  redactEnabled.addEventListener('change', updateRedactState);
  updateRedactState();

  const routingSeg = buildSegment(cfg, 'routing', ROUTING_STRATEGIES, 'data-strategy', 'weighted');
  const affinitySeg = buildSegment(cfg, 'affinity', AFFINITY_MODES, 'data-affinity', 'off');

  return h('section', { class: 'settings-group' },
    h('h3', { class: 'settings-group-title' }, '网关高级功能'),
    h('div', { class: 'settings-field' },
      h('label', {}, '数据脱敏'),
      h('div', { class: 'set-check-row' },
        h('label', { class: 'set-check' }, redactEnabled, h('span', {}, '启用脱敏')),
        h('label', { class: 'set-check' }, redactApiKeys, h('span', {}, 'API Key')),
        h('label', { class: 'set-check' }, redactPii, h('span', {}, '个人信息')),
        h('label', { class: 'set-check' }, redactCredentials, h('span', {}, '凭据'))),
      h('div', { class: 'set-row' },
        h('span', { class: 'set-row-label' }, '自定义词'),
        redactCustom)),
    h('div', { class: 'settings-field' },
      h('label', {}, '路由策略'),
      routingSeg,
      h('div', { class: 'set-hint' }, '多 key 池化时选择模型的方式：权重随机 / 轮询 / 最少使用优先 / 顺序')),
    h('div', { class: 'settings-field' },
      h('label', {}, '会话亲和性'),
      affinitySeg,
      h('div', { class: 'set-hint' }, '保持对话在同一模型上以利用 prompt 缓存；缓存 5 分钟未用自动失效')),
    h('div', { class: 'settings-field' },
      h('label', { for: 'set-affinity-ttl' }, '亲和性记录保留 (天)'),
      h('input', { id: 'set-affinity-ttl', class: 'set-input set-input-sm', type: 'number', min: '1', max: '30', value: cfg.affinityTtlDays || 7, placeholder: '7' })),
    h('div', { class: 'settings-field' },
      h('label', {}, '多窗口配额'),
      h('div', { class: 'set-row' },
        h('button', { class: 'btn btn-sm', type: 'button', onclick: () => openQuotaModal() }, '管理配额'),
        h('span', { class: 'set-hint', style: 'margin:0' }, '按别名设置日/周/月调用次数、Token、成本上限'))));
}

export { buildGatewayGroup };
