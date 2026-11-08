/**
 * 组 1「运行环境」：Python 命令（可探测）、仓库根目录（只读）、输出目录。
 */
import { $, h } from '../../lib/dom.js';

function buildEnvironmentGroup(cfg) {
  const pythonStatus = h('div', { class: 'set-inline-status', id: 'set-python-status' });
  const detectBtn = h('button', { class: 'btn btn-sm', id: 'set-detect-python', type: 'button' }, '探测');
  detectBtn.addEventListener('click', async () => {
    detectBtn.disabled = true;
    const orig = detectBtn.textContent;
    detectBtn.textContent = '探测中…';
    pythonStatus.className = 'set-inline-status pending';
    pythonStatus.textContent = '正在检测 Python 解释器…';
    try {
      // 探测输入框里刚填的命令，而不是已保存的旧值（否则「可用」是误导性的）。
      const candidate = ($('#set-python') && $('#set-python').value.trim()) || '';
      const r = await window.studio.detectPython(candidate);
      if (r && r.version) {
        pythonStatus.className = 'set-inline-status ok';
        pythonStatus.textContent = `Python ${r.version}`;
      } else {
        pythonStatus.className = 'set-inline-status err';
        pythonStatus.textContent = '未找到可用 Python';
      }
    } catch (e) {
      pythonStatus.className = 'set-inline-status err';
      pythonStatus.textContent = '未找到可用 Python';
    } finally {
      detectBtn.disabled = false;
      detectBtn.textContent = orig;
    }
  });

  return h('section', { class: 'settings-group' },
    h('h3', { class: 'settings-group-title' }, '运行环境'),
    h('div', { class: 'settings-field' },
      h('label', { for: 'set-python' }, 'Python 命令'),
      h('div', { class: 'set-row' },
        h('input', { id: 'set-python', class: 'set-input', type: 'text', value: cfg.pythonCmd || '', placeholder: 'python / python3 / 完整路径' }),
        detectBtn),
      pythonStatus),
    h('div', { class: 'settings-field' },
      h('label', {}, '仓库根目录'),
      h('div', { class: 'set-repo-root' }, cfg.repoRoot || '—')),
    h('div', { class: 'settings-field' },
      h('label', { for: 'set-output' }, '输出目录'),
      h('input', { id: 'set-output', class: 'set-input', type: 'text', value: cfg.outputDir || '', placeholder: 'E://agentcrew//output' })));
}

export { buildEnvironmentGroup };
