/**
 * 快捷键（O1）：动作清单、键位解析与匹配。纯计算，供 boot.js 与设置页共用。
 *
 * 规格文本形如 `Mod+K`（Mod = Windows 的 Ctrl / macOS 的 ⌘）、`Alt+Shift+P`、
 * `F5`。只收「带修饰键的组合」或功能键（F1–F12）：裸字母/数字会劫持输入框。
 */
const ACTIONS = [
  { id: 'palette', label: '命令面板', hint: '搜索页面与动作', def: 'Mod+K' },
  { id: 'newRun', label: '新建运行', hint: '打开新建运行弹窗', def: 'Mod+N' },
  { id: 'refresh', label: '刷新数据', hint: '重新加载当前运行的数据', def: 'F5' },
];
const MOD_KEYS = new Set(['meta', 'control', 'ctrl', 'alt', 'shift', 'cmd']);
const FN_KEY_RE = /^f([1-9]|1[0-2])$/;

function actionDefs() { return ACTIONS.map((a) => ({ ...a })); }

/** 单个键的规范写法：字母大写、功能键大写，其余原样（如 `ArrowUp`）。 */
function canonKey(key) {
  const k = String(key || '');
  if (k.length === 1) return k.toUpperCase();
  if (FN_KEY_RE.test(k.toLowerCase())) return k.toUpperCase();
  return k;
}

/** `'Mod+K'` → `{ mod, alt, shift, key }`；不可解析的返回 null。 */
function normalizeSpec(raw) {
  const text = String(raw || '').trim();
  if (!text) return null;
  const parts = text.split('+').map((p) => p.trim()).filter(Boolean);
  if (!parts.length) return null;
  const out = { mod: false, alt: false, shift: false, key: '' };
  for (const part of parts) {
    const lower = part.toLowerCase();
    if (lower === 'mod' || lower === 'ctrl' || lower === 'control' || lower === 'cmd' || lower === 'meta') out.mod = true;
    else if (lower === 'alt' || lower === 'option') out.alt = true;
    else if (lower === 'shift') out.shift = true;
    else out.key = canonKey(part);
  }
  if (!out.key) return null;
  if (!out.mod && !out.alt && !out.shift && !FN_KEY_RE.test(out.key.toLowerCase())) return null;
  return out;
}

/** 把一次键盘事件写成规格文本；裸字母/数字或只有修饰键时返回 null（不记录）。 */
function specFromEvent(e) {
  if (!e || !e.key) return null;
  const key = String(e.key);
  if (MOD_KEYS.has(key.toLowerCase())) return null;
  const parts = [];
  if (e.metaKey || e.ctrlKey) parts.push('Mod');
  if (e.altKey) parts.push('Alt');
  if (e.shiftKey) parts.push('Shift');
  const isFn = FN_KEY_RE.test(key.toLowerCase());
  if (!parts.length && !isFn) return null;
  parts.push(canonKey(key));
  return parts.join('+');
}

/** 事件是否命中某条规格（事件对象只需 metaKey/ctrlKey/altKey/shiftKey/key）。 */
function matches(e, raw) {
  const spec = normalizeSpec(raw);
  if (!spec || !e) return false;
  const mod = !!(e.metaKey || e.ctrlKey);
  if (mod !== spec.mod || !!(e.altKey) !== spec.alt || !!(e.shiftKey) !== spec.shift) return false;
  return canonKey(e.key) === spec.key;
}

/** 合并用户覆盖与默认值：未知/非法项一律回默认，不写坏动作表。 */
function resolveShortcuts(overrides) {
  const patch = overrides && typeof overrides === 'object' ? overrides : {};
  const out = {};
  for (const a of ACTIONS) {
    const raw = patch[a.id];
    out[a.id] = (raw && normalizeSpec(raw)) ? String(raw).trim() : a.def;
  }
  return out;
}

/** 冲突检测：同一键位绑了两个动作时返回 `[{ a, b, spec }]`。 */
function shortcutConflicts(map) {
  const merged = resolveShortcuts(map);
  const seen = new Map();
  const conflicts = [];
  for (const [id, spec] of Object.entries(merged)) {
    const parsed = normalizeSpec(spec);
    if (!parsed) continue;
    const key = [parsed.mod ? 'm' : '', parsed.alt ? 'a' : '', parsed.shift ? 's' : '', parsed.key].join('|');
    if (seen.has(key)) conflicts.push({ a: seen.get(key), b: id, spec });
    else seen.set(key, id);
  }
  return conflicts;
}

/** 展示用文本：`Mod+K` → `Ctrl+K`（Windows/Linux）；`F5` 原样。 */
function displaySpec(spec) {
  return String(spec || '').split('+')
    .map((p) => (p.toLowerCase() === 'mod' ? 'Ctrl' : p))
    .join('+');
}

export {
  actionDefs, canonKey, displaySpec, matches, normalizeSpec, resolveShortcuts, shortcutConflicts, specFromEvent,
};
