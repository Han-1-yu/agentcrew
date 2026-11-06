/**
 * 统一的表单取值与校验。
 *
 * 仓库里此前每处表单都自己写 `parseInt(x) || 0` / `Math.max(0, parseFloat(x) || 0)`：
 * - `<input type="number" min="0">` 只约束 UI，不约束 `.value`，`-5` 会原样落盘；
 * - `parseFloat('1e999') === Infinity`，`Math.max(0, Infinity)` 仍是 Infinity；
 * - `parseInt('1e999') === 1`，静默把错误输入变成一个完全不相干的值；
 * - 字符串 URL 直接拼进 fetch / SSE 地址，保存时不给任何行内提示。
 *
 * 这里统一返回 `{ value, error }`：调用方把 `error` 写进行内错误节点（或 toast），
 * 不要再把失败静默当成 0。所有函数都只读，不修改 DOM。
 */

/**
 * 读取一个数值输入。
 *
 * @param {HTMLInputElement|null} el 输入元素
 * @param {{min?:number,max?:number,fallback?:number,integer?:boolean,label?:string}} opts
 * @returns {{value:number,error:string}}
 */
function readNumber(el, opts = {}) {
  const {
    min = 0, max = Infinity, fallback = 0, integer = false, label = '数值',
  } = opts;
  const raw = el ? String(el.value == null ? '' : el.value).trim() : '';
  if (!raw) return { value: fallback, error: '' };
  // 先问 DOM：type=number 的 badInput / stepMismatch / rangeUnderflow 都在这里。
  if (el && el.validity && !el.validity.valid) {
    return { value: fallback, error: `${label}不是合法数值` };
  }
  const n = Number(raw);
  if (!Number.isFinite(n)) return { value: fallback, error: `${label}必须是有限数` };
  if (integer && !Number.isInteger(n)) {
    return { value: fallback, error: `${label}必须是整数` };
  }
  if (n < min) return { value: fallback, error: `${label}不能小于 ${min}` };
  if (n > max) {
    const upper = Number.isFinite(max) ? `不能大于 ${max}` : '超出允许范围';
    return { value: fallback, error: `${label}${upper}` };
  }
  return { value: n, error: '' };
}

/**
 * 读取一个 http(s) URL。与 providers/modals.js 的 addUrl 校验口径一致。
 *
 * @returns {{value:string,error:string}} value 已去掉尾部斜杠，可直接拼 path
 */
function readUrl(el, opts = {}) {
  const { required = false, label = '地址', fallback = '' } = opts;
  const raw = el ? String(el.value == null ? '' : el.value).trim() : '';
  if (!raw) {
    return required ? { value: '', error: `${label}不能为空` }
      : { value: fallback, error: '' };
  }
  let parsed;
  try {
    parsed = new URL(raw);
  } catch {
    return { value: '', error: `${label}不是合法 URL（需含 http:// 或 https://）` };
  }
  if (!/^https?:$/.test(parsed.protocol)) {
    return { value: '', error: `${label}仅支持 http/https` };
  }
  return { value: parsed.href.replace(/\/+$/, ''), error: '' };
}

/**
 * 读取自定义模型别名。后端 parse_alias 要求 `<provider>:<model>`（无冒号即报错），
 * 前端按同一口径提前拦住，避免启动后才发现配置错误。空值表示「用默认」。
 *
 * @returns {{value:string,error:string}}
 */
function readAlias(raw, opts = {}) {
  const { label = '模型别名' } = opts;
  const value = String(raw == null ? '' : raw).trim();
  if (!value) return { value: '', error: '' };
  const cut = value.indexOf(':');
  if (cut <= 0 || cut === value.length - 1 || /\s/.test(value)) {
    return { value: '', error: `${label}格式应为 provider:model` };
  }
  return { value, error: '' };
}

/** 把校验结果写进一个行内错误节点，并同步输入框的错误样式。 */
function showFieldError(input, errorBox, error) {
  if (input) input.classList.toggle('input-error', !!error);
  if (errorBox) errorBox.textContent = error || '';
  return !error;
}

export { readAlias, readNumber, readUrl, showFieldError };
