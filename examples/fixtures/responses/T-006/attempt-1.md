容器化配置完成：镜像基于 python:3.12-slim，依赖层与代码层分离以便缓存；compose 文件固化了端口、环境变量与健康检查；部署文档包含构建、启动、验证与回滚步骤。

```dockerfile file=Dockerfile
# syntax=docker/dockerfile:1
# 依赖层与代码层分离：改代码不必重装依赖
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/srv

WORKDIR /srv

COPY backend/requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir -r /srv/requirements.txt

COPY backend/app /srv/app
COPY frontend /srv/frontend

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

```yaml file=docker-compose.yml
services:
  api:
    build:
      context: .
      dockerfile: Dockerfile
    image: todo-list-api:1.0.0
    container_name: todo-list-api
    ports:
      - "8000:8000"
    environment:
      APP_HOST: "0.0.0.0"
      APP_PORT: "8000"
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
    restart: unless-stopped
```

````markdown file=docs/deploy.md
# 部署文档：Todo List Web 应用

## 1. 前置条件

- Docker 24 及以上，附带 compose 插件（`docker compose version` 可用）。
- 构建上下文为交付物根目录（包含 `Dockerfile`、`backend/`、`frontend/`）。

## 2. 构建镜像

```bash
docker build -t todo-list-api:1.0.0 .
```

依赖安装在镜像构建期完成，运行期不访问网络。

## 3. 启动服务

用 compose 启动（推荐，端口与健康检查都在 compose 文件里）：

```bash
docker compose up -d
docker compose ps
```

或直接用 docker 运行：

```bash
docker run --rm -p 8000:8000 --name todo-list-api todo-list-api:1.0.0
```

## 4. 验证

```bash
curl -s http://127.0.0.1:8000/api/health
curl -s http://127.0.0.1:8000/api/todos
```

预期：健康检查返回 `{"status": "ok", ...}`；待办列表初始为空数组。
浏览器打开 http://127.0.0.1:8000 应看到单页界面。

## 5. 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| APP_HOST | 0.0.0.0（镜像内） | 监听地址 |
| APP_PORT | 8000 | 监听端口，需与端口映射一致 |

## 6. 升级与回滚

升级：构建新 tag（如 1.0.1），修改 compose 中的镜像 tag 后执行 `docker compose up -d`。

回滚：把镜像 tag 改回上一个已知可用版本（如 1.0.0）再执行 `docker compose up -d`，
compose 会重建容器；若有状态需要保留，先从容器内导出数据再回滚。

```bash
docker compose down
docker compose up -d --force-recreate
docker compose ps
```

回滚后按第 4 节重新验证健康检查与示例请求，确认服务恢复。

## 7. 常见问题

- 端口被占用：修改 compose 的端口映射（如 `"8080:8000"`）后重新启动。
- 健康检查一直 unhealthy：确认容器日志中 uvicorn 已监听 8000 端口（`docker compose logs api`）。
- 页面 404：确认构建时 `frontend/` 已随上下文拷贝进镜像。
````

镜像与 compose 配置均已按契约交付。构建上下文是交付物根目录，因此 `backend/` 与 `frontend/` 必须一并存在；健康检查依赖 `/api/health`，回滚步骤在文档第 6 节。
