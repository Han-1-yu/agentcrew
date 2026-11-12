# AgentCrew 服务镜像（D-15）
#
# 设计要点：
#   - 依赖层单独缓存：改业务代码不重装依赖；
#   - 非 root 运行（uid 10001）；
#   - 健康检查走 TCP 而非 /health——认证默认开启，探针携带密钥会把密钥
#     写进容器元数据；
#   - 只复制运行时必需目录，不含 studio/（Electron 前端）、tests/、docs/。
#
# 构建：docker build -t agentcrew:1.0.0 .
# 运行：docker run --rm -p 8765:8765 --env-file .env -v "$PWD/output:/app/output" agentcrew:1.0.0

# syntax=docker/dockerfile:1

FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    AGENTCREW_OUTPUT_DIR=/app/output

WORKDIR /app

# ── 依赖层（利用 layer cache）──────────────────────────────────────────────
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

# ── 应用代码 ────────────────────────────────────────────────────────────────
COPY agentcrew/ ./agentcrew/
COPY artifacts/ ./artifacts/
COPY main.py ./

# ── 非 root 用户 ────────────────────────────────────────────────────────────
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/output \
    && chown -R app:app /app
USER app

EXPOSE 8765

# 健康检查：TCP 连通性（不做 HTTP，避免认证头进入容器元数据）
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8765), 3).close()"

ENTRYPOINT ["python", "main.py"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8765"]
