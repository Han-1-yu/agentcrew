架构决策已定稿：技术选型给出理由与被否决方案，模块划分明确职责与依赖方向，接口契约写到字段级。后端与前端可以并行开工，实现过程中若需变更契约，先在此文档登记理由。

```markdown file=docs/architecture.md
# Todo List Web 应用架构设计

## 1. 技术选型

| 层次 | 选型 | 理由 | 被否决的方案 |
|---|---|---|---|
| Web 框架 | FastAPI | 自带请求体校验、OpenAPI 文档与依赖注入，类型标注即契约 | Flask（需自行拼装校验与文档）、Django（对单表 CRUD 过重） |
| 数据校验 | Pydantic v2 | 与 FastAPI 同源，请求与响应模型可复用，错误信息结构化 | 手写校验（重复且易漏）、marshmallow（多一层适配） |
| 存储 | 进程内 dict + 线程锁 | 演示场景零外部依赖，启动即可用；接口封装后未来可替换 | SQLite（引入文件生命周期问题）、PostgreSQL（需要额外服务） |
| 前端 | 原生 HTML/CSS/JS | 无构建链、无依赖安装，浏览器直接可用，便于验收 | React/Vue（需要 Node 工具链，超出本期目标） |
| 测试 | pytest + fastapi.testclient | 与实现同进程，无需启动真实服务即可覆盖全部路由 | requests 打真实端口（慢且需要进程管理） |
| 部署 | 多阶段 Dockerfile + docker-compose | 固化运行环境，一条命令起服务 | 裸机脚本（环境漂移风险高） |

选型约束：全部依赖必须能通过 `pip install -r backend/requirements.txt` 一次装齐，
且不引入数据库、消息队列等额外进程。

## 2. 模块划分

后端按「入口 → 传输 → 领域 → 存储」单向依赖组织，依赖方向不可逆：

| 模块 | 职责 | 依赖 |
|---|---|---|
| `app/main.py` | 应用工厂、路由注册、异常到 HTTP 状态的映射、前端托管 | config、schemas、store |
| `app/schemas.py` | 请求与响应模型（TodoCreate / TodoUpdate / TodoOut） | 仅 pydantic |
| `app/store.py` | 待办集合的增删改查，线程安全 | models |
| `app/models.py` | Todo 领域实体与状态变更规则 | 仅 pydantic |
| `app/config.py` | 运行期配置（标题、版本、端口、跨域白名单） | pydantic |

前端不由后端构建，只通过 HTTP 契约交互：

| 文件 | 职责 |
|---|---|
| `frontend/index.html` | 页面骨架：表单、状态提示、列表容器与条目模板 |
| `frontend/app.js` | 状态加载与渲染、事件绑定、调用 REST API |
| `frontend/styles.css` | 布局与条目状态样式，含移动端适配 |

依赖方向：`main → schemas/store → models`；`store` 不得反向依赖 `schemas`，
`models` 不得依赖任何 FastAPI 类型——领域层保持框架无关，是后续替换存储实现的前提。

## 3. 数据模型

Todo 实体字段：

| 字段 | 类型 | 约束 |
|---|---|---|
| id | string | 服务端生成的 32 位十六进制串，客户端不可指定 |
| title | string | 必填，1～120 字符 |
| description | string | 可选，0～2000 字符，默认空串 |
| done | boolean | 默认 false |
| created_at | string(datetime) | 服务端生成，UTC |
| updated_at | string(datetime) | 每次成功变更后刷新，UTC |

存储形态：`dict[str, Todo]`，键为 id；读取时按 created_at 倒序返回，
变更操作在 `threading.Lock` 内完成，保证并发请求下不会读到半更新的状态。

## 4. 关键流程

新增待办：

1. 前端提交 POST /api/todos，请求体为 `{ "title": "...", "description": "..." }`。
2. FastAPI 按 TodoCreate 校验；标题为空或超长直接返回 422，不进入存储层。
3. 存储层生成 Todo 实体（含 id 与时间戳）并写入 dict。
4. 路由以 TodoOut 序列化并返回 201，前端把新条目插入列表顶部。

完成状态切换与编辑共用 PATCH /api/todos/{todo_id}：请求体只携带需要变更的字段，
未出现的字段保持原值；目标 id 不存在时返回 404，响应体为 `{ "detail": "..." }`。

删除：DELETE /api/todos/{todo_id} 成功后返回 204 且无响应体；前端据此从列表移除节点。

前端托管：`app.main.create_app()` 在定位到 `frontend/` 目录时把它挂载到根路径，
`/` 返回 `index.html`；未检测到前端目录时返回一个指向 `/docs` 的兜底页面，
保证 API 单独部署时根路径仍可用。

## 5. 错误语义

| 场景 | 状态码 | 响应体 |
|---|---|---|
| 请求体缺字段或类型不符 | 422 | FastAPI 标准校验错误 |
| 目标 id 不存在 | 404 | `{ "detail": "待办不存在：<id>" }` |
| 路径未定义 | 404 | FastAPI 默认 404 |
| 服务内部异常 | 500 | FastAPI 默认 500，日志中记录堆栈 |
```

```yaml file=docs/api-spec.yaml
openapi: 3.0.3
info:
  title: Todo List API
  version: 1.0.0
  description: Todo List Web 应用的后端接口契约；所有路径均以 /api 为前缀。
servers:
  - url: http://127.0.0.1:8000
    description: 本地开发
tags:
  - name: meta
    description: 服务自检
  - name: todos
    description: 待办事项的增删改查
paths:
  /api/health:
    get:
      tags: [meta]
      summary: 健康检查
      operationId: health
      responses:
        "200":
          description: 服务可用
          content:
            application/json:
              schema:
                type: object
                properties:
                  status:
                    type: string
                    example: ok
                  version:
                    type: string
                    example: 1.0.0
  /api/todos:
    get:
      tags: [todos]
      summary: 列出全部待办
      operationId: listTodos
      responses:
        "200":
          description: 按创建时间倒序的待办列表
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: "#/components/schemas/TodoOut"
    post:
      tags: [todos]
      summary: 新增待办
      operationId: createTodo
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: "#/components/schemas/TodoCreate"
      responses:
        "201":
          description: 创建成功
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/TodoOut"
        "422":
          description: 请求体不合法（标题为空或超长、含未声明字段）
  /api/todos/{todo_id}:
    parameters:
      - name: todo_id
        in: path
        required: true
        schema:
          type: string
    get:
      tags: [todos]
      summary: 查看单条待办
      operationId: getTodo
      responses:
        "200":
          description: 待办详情
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/TodoOut"
        "404":
          $ref: "#/components/responses/NotFound"
    patch:
      tags: [todos]
      summary: 局部更新待办
      operationId: updateTodo
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: "#/components/schemas/TodoUpdate"
      responses:
        "200":
          description: 更新后的待办
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/TodoOut"
        "404":
          $ref: "#/components/responses/NotFound"
        "422":
          description: 请求体不合法
    delete:
      tags: [todos]
      summary: 删除待办
      operationId: deleteTodo
      responses:
        "204":
          description: 删除成功，无响应体
        "404":
          $ref: "#/components/responses/NotFound"
components:
  responses:
    NotFound:
      description: 目标待办不存在
      content:
        application/json:
          schema:
            $ref: "#/components/schemas/ErrorOut"
  schemas:
    TodoCreate:
      type: object
      additionalProperties: false
      required: [title]
      properties:
        title:
          type: string
          minLength: 1
          maxLength: 120
        description:
          type: string
          maxLength: 2000
          default: ""
    TodoUpdate:
      type: object
      additionalProperties: false
      properties:
        title:
          type: string
          minLength: 1
          maxLength: 120
        description:
          type: string
          maxLength: 2000
        done:
          type: boolean
    TodoOut:
      type: object
      required: [id, title, description, done, created_at, updated_at]
      properties:
        id:
          type: string
        title:
          type: string
        description:
          type: string
        done:
          type: boolean
        created_at:
          type: string
          format: date-time
        updated_at:
          type: string
          format: date-time
    ErrorOut:
      type: object
      required: [detail]
      properties:
        detail:
          type: string
```

接口契约覆盖 6 条路径、共 3 个请求/响应模型：`TodoCreate` 与 `TodoUpdate` 均禁止未声明字段，
`TodoOut` 的 6 个字段与领域实体一一对应。后端实现时请以本文件为准；需要变更字段时先更新本文件并说明理由。
