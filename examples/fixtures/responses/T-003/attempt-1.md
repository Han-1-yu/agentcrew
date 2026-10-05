后端按接口契约实现完毕：领域模型与传输模型分离，存储层用 dict + 线程锁保证并发安全，路由全部对齐 docs/api-spec.yaml。启动方式见文末说明。

```python file=backend/app/__init__.py
"""Todo List 后端包：应用工厂、领域模型、请求模型与进程内存储。

模块划分见 docs/architecture.md；对外唯一入口是 ``app.main.create_app``。
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
```

```python file=backend/app/config.py
"""运行期配置：默认值面向本地演示，可用环境变量覆盖。"""

import os

from pydantic import BaseModel, Field


class Settings(BaseModel):
    """服务配置；所有字段都有可直接运行的默认值。"""

    title: str = "Todo List API"
    host: str = "127.0.0.1"
    port: int = 8000
    allowed_origins: list[str] = Field(
        default_factory=lambda: [
            "http://127.0.0.1:8000",
            "http://localhost:8000",
        ]
    )

    @classmethod
    def from_env(cls) -> "Settings":
        """读取 APP_HOST / APP_PORT 覆盖默认监听地址。"""
        overrides: dict[str, object] = {}
        app_host = os.environ.get("APP_HOST")
        if app_host:
            overrides["host"] = app_host
        app_port = os.environ.get("APP_PORT")
        if app_port:
            overrides["port"] = int(app_port)
        return cls(**overrides)
```

```python file=backend/app/models.py
"""领域模型：待办事项实体与状态变更规则，不依赖任何 Web 框架类型。"""

from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, Field


def _now() -> datetime:
    """当前 UTC 时间；统一时区，避免前端拿到含义不明的时间戳。"""
    return datetime.now(timezone.utc)


class Todo(BaseModel):
    """一条待办事项；``id`` 由服务端生成，客户端不可指定。"""

    id: str = Field(default_factory=lambda: uuid4().hex)
    title: str
    description: str = ""
    done: bool = False
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    def apply(
        self,
        *,
        title: str | None = None,
        description: str | None = None,
        done: bool | None = None,
    ) -> "Todo":
        """返回应用了非空变更的新实例，并刷新 ``updated_at``。

        ``None`` 表示「本次不修改该字段」，因此前端可以只提交需要变更的字段。
        """
        changes: dict[str, object] = {}
        if title is not None:
            changes["title"] = title
        if description is not None:
            changes["description"] = description
        if done is not None:
            changes["done"] = done
        if not changes:
            return self
        changes["updated_at"] = _now()
        return self.model_copy(update=changes)
```

```python file=backend/app/schemas.py
"""请求/响应模型：与 docs/api-spec.yaml 的字段一一对应。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TodoCreate(BaseModel):
    """POST /api/todos 的请求体；未声明字段一律拒绝。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120, description="待办标题，必填")
    description: str = Field(default="", max_length=2000, description="可选的详细说明")


class TodoUpdate(BaseModel):
    """PATCH /api/todos/{todo_id} 的请求体；未提供的字段保持不变。"""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    done: bool | None = None


class TodoOut(BaseModel):
    """待办事项的响应表示，与 Todo 实体的字段一一对应。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    description: str
    done: bool
    created_at: datetime
    updated_at: datetime


class ErrorOut(BaseModel):
    """统一错误响应：``detail`` 为人类可读消息。"""

    detail: str
```

```python file=backend/app/store.py
"""进程内存储：dict + 线程锁，零外部依赖，重启即清空（演示用途）。"""

import threading

from app.models import Todo


class TodoNotFoundError(KeyError):
    """按 id 找不到待办事项。"""

    def __init__(self, todo_id: str) -> None:
        super().__init__(todo_id)
        self.todo_id = todo_id


class TodoStore:
    """线程安全的待办集合；所有读写都在同一把锁内完成。"""

    def __init__(self) -> None:
        self._items: dict[str, Todo] = {}
        self._lock = threading.Lock()

    def get(self, todo_id: str) -> Todo:
        """按 id 取一条；缺失时抛 TodoNotFoundError。"""
        with self._lock:
            item = self._items.get(todo_id)
        if item is None:
            raise TodoNotFoundError(todo_id)
        return item

    def list(self) -> list[Todo]:
        """返回全部待办，按创建时间倒序（最新在前）。"""
        with self._lock:
            items = list(self._items.values())
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    def create(self, *, title: str, description: str = "") -> Todo:
        """新建并保存一条待办。"""
        todo = Todo(title=title, description=description)
        with self._lock:
            self._items[todo.id] = todo
        return todo

    def update(
        self,
        todo_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        done: bool | None = None,
    ) -> Todo:
        """局部更新一条待办；缺失 id 抛 TodoNotFoundError。"""
        with self._lock:
            current = self._items.get(todo_id)
            if current is None:
                raise TodoNotFoundError(todo_id)
            updated = current.apply(title=title, description=description, done=done)
            self._items[todo_id] = updated
        return updated

    def delete(self, todo_id: str) -> None:
        """删除一条待办；缺失 id 抛 TodoNotFoundError。"""
        with self._lock:
            if todo_id not in self._items:
                raise TodoNotFoundError(todo_id)
            del self._items[todo_id]

    def clear(self) -> None:
        """清空全部待办（本地演示与测试用）。"""
        with self._lock:
            self._items.clear()
```

```python file=backend/app/main.py
"""FastAPI 应用入口：Todo REST API 与前端页面托管。"""

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import Settings
from app.models import Todo
from app.schemas import TodoCreate, TodoOut, TodoUpdate
from app.store import TodoNotFoundError, TodoStore

_FALLBACK_PAGE = """<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>Todo List API</title></head>
<body>
  <h1>Todo List API</h1>
  <p>本次部署未附带前端文件，接口文档见 <a href="/docs">/docs</a>。</p>
</body>
</html>
"""


def _frontend_dir() -> Path | None:
    """定位 frontend/ 目录：兼容 artifacts 根布局与容器内 /srv 布局。"""
    here = Path(__file__).resolve()
    for base in (here.parents[1], here.parents[2]):
        candidate = base / "frontend"
        if candidate.is_dir():
            return candidate
    return None


def create_app() -> FastAPI:
    """装配应用：配置、中间件、路由与前端静态资源。"""
    settings = Settings.from_env()
    app = FastAPI(title=settings.title, version=__version__)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type"],
    )
    store = TodoStore()
    app.state.store = store

    def load_or_404(todo_id: str) -> Todo:
        """取一条待办；不存在时转成带中文 detail 的 404。"""
        try:
            return store.get(todo_id)
        except TodoNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"待办不存在：{exc.todo_id}",
            ) from exc

    @app.exception_handler(TodoNotFoundError)
    async def handle_missing_todo(
        _request: Request, exc: TodoNotFoundError
    ) -> JSONResponse:
        """兜底处理：任何遗漏的未捕获缺失都会得到与其他接口一致的 404 语义。"""
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": f"待办不存在：{exc.todo_id}"},
        )

    @app.get("/api/health", tags=["meta"])
    def health() -> dict[str, str]:
        """存活探针：容器健康检查与前端自检共用。"""
        return {"status": "ok", "version": __version__}

    @app.get("/api/todos", response_model=list[TodoOut], tags=["todos"])
    def list_todos() -> list[TodoOut]:
        """列出全部待办（最新创建在前）。"""
        return [TodoOut.model_validate(item) for item in store.list()]

    @app.post(
        "/api/todos",
        response_model=TodoOut,
        status_code=status.HTTP_201_CREATED,
        tags=["todos"],
    )
    def create_todo(payload: TodoCreate) -> TodoOut:
        """新增一条待办；服务端生成 id 与时间戳。"""
        created = store.create(title=payload.title, description=payload.description)
        return TodoOut.model_validate(created)

    @app.get("/api/todos/{todo_id}", response_model=TodoOut, tags=["todos"])
    def get_todo(todo_id: str) -> TodoOut:
        """按 id 查看单条待办。"""
        return TodoOut.model_validate(load_or_404(todo_id))

    @app.patch("/api/todos/{todo_id}", response_model=TodoOut, tags=["todos"])
    def update_todo(todo_id: str, payload: TodoUpdate) -> TodoOut:
        """局部更新：仅提交需要改动的字段。"""
        load_or_404(todo_id)
        updated = store.update(
            todo_id,
            title=payload.title,
            description=payload.description,
            done=payload.done,
        )
        return TodoOut.model_validate(updated)

    @app.delete(
        "/api/todos/{todo_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["todos"],
    )
    def delete_todo(todo_id: str) -> Response:
        """删除一条待办；成功后返回 204 空响应。"""
        load_or_404(todo_id)
        store.delete(todo_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    frontend = _frontend_dir()
    if frontend is not None:
        # 单页与静态资源一起挂到根路径：/ 返回 index.html，
        # HTML 里的 ./app.js、./styles.css 相对引用因此自然可用。
        app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
    else:

        @app.get("/", include_in_schema=False)
        def index_fallback() -> HTMLResponse:
            """前端缺失时的兜底页面，保证根路径仍返回 HTML。"""
            return HTMLResponse(_FALLBACK_PAGE)

    return app


app = create_app()
```

```text file=backend/requirements.txt
fastapi>=0.115,<1.0
uvicorn[standard]>=0.30,<1.0
pydantic>=2.7,<3.0
pytest>=8.0
httpx>=0.27
```

依赖与启动方式：`cd backend && pip install -r requirements.txt && uvicorn app.main:app --reload`，随后访问 http://127.0.0.1:8000 。存储层是进程内 dict，重启进程数据即清空，符合本期「零外部依赖演示」的约定；接口文档由 FastAPI 自动生成在 /docs。
