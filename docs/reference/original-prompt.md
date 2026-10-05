
AgentCrew 多智能体协作平台 — 项目提示词
可直接组队开工的虚拟项目团队系统
一、核心目标
打造一个可直接组队开工的虚拟项目团队系统：每个 Agent 有明确的岗位分工、职责边界和能力擅长，由项目经理统一调度，自动拆解项目目标、按角色分配任务、上下游流转协作、质量验收交付，完整跑完从需求到最终产出的全项目流程。
二、核心功能设计
1. 角色分工体系
每个 Agent 拥有独立的：岗位名称、核心职责、输出规范、系统提示词、绑定的大模型、能力边界
内置一套完整的标准项目团队角色，可直接组队执行项目
支持自定义扩展新角色，按需扩充团队
2. 任务拆解与分配机制
项目经理接收顶层项目目标，自动拆解为结构化子任务
每个任务明确：任务描述、负责角色、交付物要求、优先级、前置依赖
自动按角色分工匹配对应 Agent 执行
自动识别串行依赖任务和并行任务，按依赖关系调度
3. 协作流转机制
全局项目上下文共享，所有 Agent 可读取项目背景和前置产出
任务状态流转：待分配 → 进行中 → 待验收 → 已完成 / 驳回重做
上下游任务自动衔接，前置任务完成后自动触发下游
评审驳回机制：产出不合格自动退回执行人优化重做
4. 项目看板与进度追踪
终端实时展示项目看板，呈现所有任务状态、负责人、交付进度
执行过程全程日志留痕
支持中断后从断点继续执行
5. 多模型灵活绑定
不同角色可绑定不同厂商的大模型，按需匹配能力
统一 OpenAI 兼容接口，支持 GPT、Claude、豆包、通义千问、文心一言、Ollama 本地模型
三、内置项目团队角色（分工明确）
1. 项目经理：项目总负责人，拆解目标、制定计划、分配任务、验收成果、协调整体进度
2. 产品经理：需求分析，输出产品需求文档、功能清单、验收标准
3. 架构师：技术选型、架构设计、模块划分、接口定义、输出技术方案
4. 后端开发工程师：负责后端业务逻辑、数据模型、API 接口代码开发
5. 前端开发工程师：负责前端页面、交互逻辑、样式开发
6. 测试工程师：编写测试用例、执行代码测试、输出 Bug 报告和修复建议
7. 运维工程师：输出部署方案、Docker 配置、CI/CD 脚本、环境配置说明
8. 文档工程师：编写项目 README、使用文档、接口文档、部署说明
四、技术栈与约束
Python 3.10+
核心依赖：openai、python-dotenv、pydantic v2、asyncio、rich（终端看板可视化）
严格禁止引入 CrewAI、LangGraph、AutoGen、LangChain 等第三方 Agent 框架，所有核心调度与分工逻辑原生实现
全量类型注解，异常处理完善，代码遵循 PEP 8 规范
五、项目目录结构
agentcrew/
├── .env.example              # API 配置模板
├── requirements.txt          # 依赖清单
├── README.md                 # 项目说明文档
├── main.py                   # 快速启动入口
├── agentcrew/
│   ├── __init__.py
│   ├── models/                # 多模型接入层
│   │   ├── __init__.py
│   │   ├── model_factory.py   # 模型工厂
│   │   └── config.py          # 模型配置
│   ├── agents/                # 角色 Agent 层
│   │   ├── __init__.py
│   │   ├── base_agent.py      # Agent 抽象基类
│   │   ├── project_manager.py
│   │   ├── product_manager.py
│   │   ├── architect.py
│   │   ├── backend_dev.py
│   │   ├── frontend_dev.py
│   │   ├── qa_engineer.py
│   │   ├── devops_engineer.py
│   │   └── technical_writer.py
│   ├── task/                  # 任务管理模块
│   │   ├── __init__.py
│   │   ├── task.py            # 任务数据结构
│   │   └── task_board.py      # 全局任务看板
│   ├── crew/                  # 团队编排核心
│   │   ├── __init__.py
│   │   └── crew.py            # 团队调度器
│   └── utils/
│       ├── __init__.py
│       ├── logger.py
│       └── retry.py
├── examples/
│   └── todo_web_project.py    # 完整项目演示：开发一个Todo List Web应用
└── output/                    # 项目交付物输出目录
六、核心逻辑要求
1. BaseAgent 基类封装通用能力：模型调用、上下文读取、产出提交
2. 每个角色 Agent 继承基类，实现自身的 execute 方法，仅专注本岗位职责内的工作
3. 项目经理是核心调度者，负责全流程的任务拆解、分配、验收、流转
4. TaskBoard 维护全局任务状态，自动按依赖关系调度执行顺序
5. Crew 类封装整个团队，接收项目目标后一键启动全流程执行
七、交付验收标准
1. 所有文件代码完整，无空文件，可直接运行
2. 执行 pip install -r requirements.txt 可正常安装依赖
3. 复制 .env.example 为 .env 填入 API Key 后，示例可直接运行
4. 终端实时渲染项目看板，清晰展示每个角色的分工、任务进度、执行状态
5. 示例完整演示「开发一个 Todo List Web 应用」项目，全流程由 8 个 Agent 分工协作完成
6. 执行完成后，在 output 目录生成完整的项目代码、文档、配置等所有交付物
7. README.md 包含：项目介绍、角色说明、快速开始、自定义角色/团队的开发指南
八、项目关键词
Python、多智能体协作平台、Agent 编排、项目管理、项目经理调度、任务拆分、任务依赖、并发执行、任务状态机、失败重试、评审返工、任务验收、Pydantic v2、结构化输出、模型适配、OpenAI 兼容接口、模拟运行模式、项目状态持久化、暂停恢复取消、终端看板、交付物追踪、自动化测试、安全文件操作、配置管理、日志审计、README 文档生成
九、调研参考范围
为完善本项目规格，已调研以下成熟项目的设计模式，提炼可借鉴的模块与工程实践：
深度调研（5个）
LangGraph — 状态图编排、checkpoint 持久化
Temporal / Prefect — 重试机制、结果缓存、可恢复任务执行
AutoGen — 群体对话、RequestToSpeak 信号调度、human_input_mode
CrewAI — 角色三件套（role/goal/backstory）、guardrail 评审链
OpenHands — Action-Observation 协议、事件流、沙箱隔离
补充调研（10个）
MetaGPT — 共享消息池+订阅过滤、_observe→_think→_act 循环、产物契约驱动
Microsoft Agent Framework (MAF) — 四种编排模式、Durable checkpoint
AgentScope — 事件流看板、目录隔离沙箱、Pipeline 原语
Plandex — 计划树拆解
Mastra — durable workflow + 可观测性
OpenAI Swarm — 极简 handoff 模式对照
OmniNova — 层级架构、动态任务路由
Suna — 并发调度、上下文隔离
LlamaIndex AgentWorkflow — DAG 事件驱动
Swarms — MCP 互操作
十、后续扩展方向（V2 增强项）
外部 CLI 引擎接入：Claude Code / Codex / Cursor / AutoGen（subprocess 调用，不引入库依赖）
MCP 协议互操作
HTTP API 服务端 + WebSocket 实时推送
LLM 摘要式上下文压缩
树状任务拆解
按任务认知复杂度自动选模型
Web UI 交互面板（对话、审批、终端）
