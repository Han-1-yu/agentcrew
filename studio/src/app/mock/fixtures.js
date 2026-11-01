/**
 * 演示数据：与后端 run.json / tasks.json / ledger.jsonl 同构的假快照。
 * 仅在没有选中 run（演示模式）时使用。
 */

import { ROLE_META } from '../../contracts/enums.js';
import { S } from '../../lib/store.js';

function nowTs(offsetMin) {
  const d = new Date(Date.now() - (offsetMin||0)*60000);
  const p = (x,n=2)=>String(x).padStart(n,'0');
  const us = String(d.getMilliseconds()).padStart(3,'0')+'000';
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${us}`;
}

const RUN_ID = 'run-a7f3c2';


function buildMockRun() {
  return {
    run_id: RUN_ID,
    dir: null,
    project_goal: '开发一个高并发短链接生成服务：含 REST API、Redis 缓存、限流、部署流水线与完整文档',
    status: 'running',          // RunStatus
    phase: 'execution',         // planning/execution/delivery
    provider: 'OpenAI',
    model: 'gpt-4o',
    budget_limit_usd: 1.0,
    started_at: nowTs(12),
    updated_at: nowTs(0),
    config_snapshot: { default_model: 'gpt-4o', max_concurrency: 4, mock: false, max_cost_usd: 1.0 },
    stats: {
      started_at: nowTs(12), finished_at: null,
      total_tasks: 14, succeeded: 4, failed: 0, blocked: 1, skipped: 0,
      llm_calls: 87, prompt_tokens: 142300, completion_tokens: 38400, estimated_cost_usd: 0.3821,
    },
  };
}

// 任务：字段对齐 tasks.json。dependencies / attempts / rework_count / result(null) / reviews

function T(id, title, role, deps, status, extra) {
  return Object.assign({
    id, title, description: '', brief: '', role, priority: 5,
    dependencies: deps, attempts: 1, rework_count: 0,
    max_attempts: 3, max_reworks: 2, timeout_seconds: 300,
    status, result: null, reviews: [],
    progress: status==='running' ? 45 : 0,
    error: null,
  }, extra||{});
}

function buildMockTasks() {
  return [
    T('T-01','需求收集与澄清','project_manager',[],'succeeded',{brief:'与干系人确认功能边界与 SLA', result:{summary:'已澄清并发目标 5000 QPS 与 99.9% SLA', duration_ms:420000, artifacts:[{path:'docs/PRD.md',size_bytes:8200}]}}),
    T('T-02','产品原型定义','product_manager',[],'succeeded',{brief:'输出 API 草图与验收标准', result:{summary:'完成 API 草图与页面流程', duration_ms:510000, artifacts:[{path:'docs/prototype.md',size_bytes:5400}]}}),
    T('T-03','系统架构设计','architect',['T-01'],'succeeded',{brief:'分层架构与技术选型', result:{summary:'确定分层架构与 Redis 缓存策略', duration_ms:680000, artifacts:[{path:'docs/arch.md',size_bytes:12000}]}}),
    T('T-04','前端原型搭建','frontend_dev',['T-02'],'succeeded',{brief:'后台页面与落地页原型', result:{summary:'完成控制台与跳转落地页原型', duration_ms:590000, artifacts:[{path:'frontend/proto.html',size_bytes:6100}]}}),
    T('T-05','数据模型设计','backend_dev',['T-03'],'running',{brief:'设计 url_map 表与 TTL 策略', progress:62}),
    T('T-06','API 接口设计','architect',['T-03','T-05'],'blocked',{brief:'定义 RESTful 接口与错误码', description:'定义 RESTful 接口、错误码与鉴权方案。'}),
    T('T-07','后端用户模块','backend_dev',['T-05','T-06'],'pending',{brief:'注册、API Key 与鉴权中间件'}),
    T('T-08','后端短链模块','backend_dev',['T-05','T-06'],'pending',{brief:'短链生成、映射解析与统计'}),
    T('T-09','前端页面实现','frontend_dev',['T-06','T-04'],'pending',{brief:'对接接口实现控制台与落地页'}),
    T('T-10','测试用例编写','qa_engineer',['T-07','T-08'],'pending',{brief:'接口与端到端测试用例'}),
    T('T-11','集成联调','backend_dev',['T-07','T-08','T-09'],'pending',{brief:'前后端打通完整链路'}),
    T('T-12','部署流水线','devops_engineer',['T-11'],'pending',{brief:'CI/CD、容器化与灰度发布'}),
    T('T-13','系统测试','qa_engineer',['T-11','T-10'],'pending',{brief:'压测、回归与安全扫描'}),
    T('T-14','交付文档','technical_writer',['T-13','T-12'],'pending',{brief:'部署、API 与运维文档'}),
  ];
}


function buildMockAgents() {
  // agent status 派生自任务：queued/executing/reviewing/done
  return {
    project_manager:  { status:'done',      task:'空闲',     action:'已交付需求文档',   done:1, fail:0, model:'gpt-4o',      tokens:8200,  cost:0.021, updated:nowTs(10) },
    product_manager:  { status:'done',      task:'空闲',     action:'已完成原型评审',   done:1, fail:0, model:'gpt-4o',      tokens:11400, cost:0.034, updated:nowTs(9) },
    architect:        { status:'queued',    task:'T-06 API 接口设计', action:'等待 T-05 数据模型', done:1, fail:0, model:'gpt-4o', tokens:15600, cost:0.048, updated:nowTs(1) },
    backend_dev:      { status:'executing', task:'T-05 数据模型设计', action:'正在建表与索引',   done:0, fail:0, model:'gpt-4o', tokens:9800, cost:0.029, updated:nowTs(0) },
    frontend_dev:     { status:'done',      task:'空闲',     action:'等待接口契约',     done:1, fail:0, model:'gpt-4o', tokens:9800, cost:0.027, updated:nowTs(8) },
    qa_engineer:      { status:'queued',    task:'空闲',     action:'等待可测构建',     done:0, fail:0, model:'gpt-4o-mini', tokens:0, cost:0, updated:nowTs(12) },
    devops_engineer:  { status:'queued',    task:'空闲',     action:'等待联调产物',     done:0, fail:0, model:'gpt-4o-mini', tokens:0, cost:0, updated:nowTs(12) },
    technical_writer: { status:'queued',    task:'空闲',     action:'等待交付物',       done:0, fail:0, model:'gpt-4o-mini', tokens:0, cost:0, updated:nowTs(12) },
  };
}

// 三层门禁：layer static(G1)/llm(G2)/delivery(G3)。状态为 UI 派生聚合。

function buildMockGates() {
  return [
    { id:'G1', layer:'static', name:'静态校验', status:'running', checks:{total:12, passed:8, failed:0}, duration_ms:null,
      verdict:'正在运行 lint / 类型检查 / 依赖扫描', rework:true, need_human:false, task_ref:'T-07, T-08' },
    { id:'G2', layer:'llm', name:'LLM 评审', status:'pending', checks:{total:8, passed:0, failed:0}, duration_ms:null,
      verdict:'等待集成联调完成后触发', rework:true, need_human:true, task_ref:'T-11' },
    { id:'G3', layer:'delivery', name:'交付确认', status:'pending', checks:{total:5, passed:0, failed:0}, duration_ms:null,
      verdict:'等待系统测试通过后由人工确认', rework:false, need_human:true, task_ref:'T-14' },
  ];
}

// ledger 事件：{ ts, run_id, event, data }

function ev(tsOffset, event, data, taskId, role) {
  return { ts: nowTs(tsOffset), run_id: RUN_ID, event, data: Object.assign(taskId?{task_id:taskId}:{}, role?{role}:{}, data||{}) };
}

function buildMockEvents() {
  return [
    ev(12,'run_started',{ goal:'短链接生成服务' }),
    ev(11,'plan_created',{ task_count:14, roles:Object.keys(ROLE_META), planner_attempts:1 }),
    ev(11,'plan_scored',{ total:0.81, coverage:0.8, specificity:0.85, dependency_quality:0.9, deliverable_clarity:0.8 }),
    ev(10,'task_started',{ attempt:1 },'T-01','project_manager'),
    ev(10,'task_finished',{ attempt:1, duration_ms:420000 },'T-01','project_manager'),
    ev(9,'task_started',{ attempt:1 },'T-02','product_manager'),
    ev(9,'task_finished',{ attempt:1, duration_ms:510000 },'T-02','product_manager'),
    ev(7,'task_started',{ attempt:1 },'T-03','architect'),
    ev(7,'task_finished',{ attempt:1, duration_ms:680000 },'T-03','architect'),
    ev(6,'task_started',{ attempt:1 },'T-04','frontend_dev'),
    ev(6,'task_finished',{ attempt:1, duration_ms:590000 },'T-04','frontend_dev'),
    ev(3,'checkpoint_saved',{ reason:'周期检查点' }),
    ev(2,'task_started',{ attempt:1 },'T-05','backend_dev'),
    ev(1,'llm_failover',{ from:'OpenAI', to:'Anthropic', reason:'429 rate limit' }),
    ev(0,'budget_warning',{ pct:38 }),
  ];
}


function initMockData() {
  S.dash = {
    run: buildMockRun(), tasks: buildMockTasks(),
    agents: buildMockAgents(), gates: buildMockGates(), events: buildMockEvents(),
  };
}

/* ============================================================
   DAG 布局（拓扑分层，用 dependencies 字段）
   ============================================================ */

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
};
