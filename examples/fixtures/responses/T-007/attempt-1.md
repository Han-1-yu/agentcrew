README 与使用文档已交付：README 覆盖项目定位、目录结构、快速开始、测试与 API 一览；usage.md 给出可复制执行的启动命令、页面操作说明与 curl 示例。两份文档都包含命令示例，外层围栏按产物格式要求使用 4 个反引号，避免示例被提前截断。

````markdown file=README.md
# Todo List Web 应用

一个轻量的 Todo List 单页应用：浏览器里记录、完成、编辑与删除待办事项，数据由 FastAPI 后端统一保存。
技术栈刻意保持精简——FastAPI + Pydantic v2 + 进程内存储 + 原生 HTML/CSS/JS，不引入数据库与前端构建链，
`pip install` 之后即可运行。

## 目录结构

| 路径 | 说明 |
|---|---|
| `backend/app/main.py` | FastAPI 应用工厂与全部 REST 路由 |
| `backend/app/models.py` | Todo 领域实体与状态变更规则 |
| `backend/app/schemas.py` | 请求/响应模型（TodoCreate / TodoUpdate / TodoOut） |
| `backend/app/store.py` | 进程内存储（dict + 线程锁） |
| `backend/requirements.txt` | 后端依赖清单 |
| `frontend/` | 单页界面：index.html、app.js、styles.css |
| `tests/` | pytest 用例（接口与前端静态检查） |
| `docs/` | 需求、架构、接口契约、测试报告与部署文档 |
| `Dockerfile`、`docker-compose.yml` | 容器化构建与启动配置 |

## 快速开始

前置条件：Python 3.10 及以上（开发环境为 3.12），无需数据库。

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload
```

启动后：

- 打开 http://127.0.0.1:8000 使用页面；
- 打开 http://127.0.0.1:8000/docs 查看自动生成的接口文档。

## 运行测试

```bash
cd artifacts
python -m pytest tests -v
```

接口用例见 `tests/test_api.py`，前端静态检查见 `tests/test_ui.py`。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET | `/api/todos` | 列出全部待办（最新在前） |
| POST | `/api/todos` | 新增待办 |
| GET | `/api/todos/{todo_id}` | 查看单条待办 |
| PATCH | `/api/todos/{todo_id}` | 局部更新（标题、说明、完成状态） |
| DELETE | `/api/todos/{todo_id}` | 删除待办 |

字段与状态码详见 `docs/api-spec.yaml`；使用示例见 `docs/usage.md`。

## 常见问题

- 端口被占用：`uvicorn app.main:app --port 8001` 换端口启动。
- 数据重启后消失：存储是进程内 dict，属本期设计（演示用途），持久化留待后续迭代。
- 页面能打开但列表报错：确认后端进程仍在运行，且浏览器访问的是同一个端口。
````

````markdown file=docs/usage.md
# 使用文档

本文覆盖三件事：启动服务、在页面上操作、直接用 API 操作。所有命令都可以复制执行。

## 前置条件

- Python 3.10 及以上。
- 已安装依赖：在 `backend/` 目录下执行 `pip install -r requirements.txt`。

## 启动服务

```bash
cd backend
uvicorn app.main:app --reload
```

看到 `Uvicorn running on http://127.0.0.1:8000` 即启动成功。换端口用 `--port 8001`。

## 页面操作

1. 在输入框填写标题并点击「添加」，新待办出现在列表顶部。
2. 勾选左侧复选框标记完成，再次点击取消完成。
3. 点击「编辑」就地修改标题：回车保存，Esc 取消。
4. 点击「删除」移除该条；列表为空时显示引导文案。

状态栏会实时显示总条数与未完成条数；请求失败时状态栏给出错误消息，页面不会崩溃。

## API 使用示例

创建一条待办：

```bash
curl -s -X POST http://127.0.0.1:8000/api/todos \
  -H "Content-Type: application/json" \
  -d '{"title": "写周报"}'
```

列出全部待办：

```bash
curl -s http://127.0.0.1:8000/api/todos
```

标记完成（把 `<id>` 替换为上一步返回的 id）：

```bash
curl -s -X PATCH http://127.0.0.1:8000/api/todos/<id> \
  -H "Content-Type: application/json" \
  -d '{"done": true}'
```

删除：

```bash
curl -s -X DELETE -o /dev/null -w "%{http_code}\n" \
  http://127.0.0.1:8000/api/todos/<id>
```

删除成功返回 204；对不存在的 id 调用会返回 404，响应体形如 `{"detail": "待办不存在：<id>"}`。

## 常见问题

- 422 校验失败：标题为空、超过 120 字，或请求体带了未声明字段。
- 页面提示「请求失败」：后端未启动或端口不一致，先访问 `/api/health` 确认。
- 中文乱码：请求头需要 `Content-Type: application/json`，示例中已包含。
````

文档中的端口、路径与字段均取自当前实现（backend/app/main.py 与 docs/api-spec.yaml），命令已在本地逐条核对可执行。若后续接口有变更，请同步更新本目录下的使用说明。
