/**
 * 新建档案的起点模板：不是固定模板，只是把常见组合填进表单。
 *
 * 与 CrewAI / Agent Builder 的模板库同一个作用：降低第一次创建的门槛，
 * 同时把「写手 / 复核 / 代码执行」的典型参数示范出来。
 */
const TEMPLATES = [
  { label: '空白档案', draft: {} },
  {
    label: '快速写手',
    draft: {
      key: 'fast-writer', title: '快速写手',
      description: '短文档、摘要与改写；优先速度与成本，不承担架构决策。',
      capabilities: 'text、summary、rewrite', temperature: 0.4,
      cost_ceiling_usd: 0.05, run_cost_ceiling_usd: 0.5,
    },
  },
  {
    label: '严格复核者',
    draft: {
      key: 'strict-reviewer', title: '严格复核者',
      description: '只读复核：对照验收标准挑问题、给修改建议，不改稿。',
      capabilities: 'review、qa', toolsMode: 'list',
      tools: ['read_file', 'list_files', 'grep'], temperature: 0.1,
      cost_ceiling_usd: 0.08,
    },
  },
  {
    label: '代码执行者',
    draft: {
      key: 'code-runner', title: '代码执行者',
      description: '实现与运行验证：按契约写文件，能用工具跑一遍再交付。',
      capabilities: 'code、test', temperature: 0.2,
      path_scope: 'src/\ntests/',
    },
  },
  {
    label: '长文写手',
    draft: {
      key: 'long-writer', title: '长文写手',
      description: '长文档、方案与报告：先列提纲再展开，保持结构一致与术语统一。',
      capabilities: 'writing、outline、edit', temperature: 0.6,
      max_output_tokens: 8192, cost_ceiling_usd: 0.12, run_cost_ceiling_usd: 1.2,
    },
  },
  {
    label: '数据分析师',
    draft: {
      key: 'data-analyst', title: '数据分析师',
      description: '读数据、算指标、写结论：只读文件与检索，产出可复核的表格与口径说明。',
      capabilities: 'analysis、data、report', toolsMode: 'list',
      tools: ['read_file', 'list_files', 'grep'], temperature: 0.15,
      cost_ceiling_usd: 0.08, run_cost_ceiling_usd: 0.8,
    },
  },
];

export { TEMPLATES };
