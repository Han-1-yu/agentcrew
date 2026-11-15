"""C6：真语义 embedding 端点接入（OpenAI 兼容 /embeddings）。

真实性优先：跑在**真实本地 HTTP 服务**上，断言请求次数、批量切分、状态码分流与
维度校验，而不是只看一个假对象。四个关键契约：

1. 默认关闭时 ``build_embedder`` 返回 ``None``（零网络、零行为变化）；
2. 开启后走 ``HttpEmbedder`` 并被认为「语义通道」（``is_semantic_embedder`` 为真）；
3. 401 不重试、500 有限重试、超时有限重试，错误消息不含密钥；
4. 缓存键含维度——换维度不复用旧向量，避免「不报错但全 0 分」的静默退化。
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from agentcrew.config import Settings, _yaml_values
from agentcrew.crew.crew import Crew
from agentcrew.knowledge.embeddings import (
    CachedEmbedder,
    EmbeddingError,
    HttpEmbedder,
    build_embedder,
    is_semantic_embedder,
)
from agentcrew.models.embedding import embedding_target


@pytest.fixture
def embed_server():
    """记录请求体；``fail_times`` 控制前几次 500，``status`` 控制硬失败。"""
    state: dict[str, object] = {
        "received": [], "fail_times": 0, "status": 200, "dim": 4, "delay": 0.0,
    }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            state["received"].append({                     # type: ignore[union-attr]
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "body": body,
            })
            delay = state["delay"]
            if isinstance(delay, (int, float)) and delay > 0:
                time.sleep(float(delay))
            remaining = state["fail_times"]
            if isinstance(remaining, int) and remaining > 0:
                state["fail_times"] = remaining - 1
                self.send_response(500)
                self.end_headers()
                return
            status = int(state["status"] or 200)       # type: ignore[arg-type]
            if status != 200:
                self.send_response(status)
                self.end_headers()
                self.wfile.write(b'{"error":"boom"}')
                return
            count = len(body.get("input") or [])
            dim = int(state["dim"] or 4)               # type: ignore[arg-type]
            payload = {"data": [{"embedding": [0.1] * dim} for _ in range(count)]}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode("utf-8"))

        def log_message(self, *args: object) -> None:
            """静音。"""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1", state
    finally:
        server.shutdown()
        server.server_close()


def mk_embedder(url: str, **overrides) -> HttpEmbedder:
    params = {"model": "text-embedding-3-small", "api_key": "sk-test",
              "base_url": url, "timeout_seconds": 2.0, "batch_size": 32}
    params.update(overrides)
    return HttpEmbedder(**params)


# ── HTTP 通道 ─────────────────────────────────────────────────────────────


async def test_posts_to_embeddings_and_infers_dim(embed_server):
    url, state = embed_server
    embedder = mk_embedder(url)
    vectors = await embedder.embed(["第一句", "第二句"])
    assert len(vectors) == 2 and len(vectors[0]) == 4
    assert embedder.dim == 4
    request = state["received"][0]
    assert request["path"] == "/v1/embeddings"
    assert request["auth"] == "Bearer sk-test"
    assert request["body"]["model"] == "text-embedding-3-small"
    assert request["body"]["input"] == ["第一句", "第二句"]


async def test_splits_into_batches(embed_server):
    url, state = embed_server
    embedder = mk_embedder(url, batch_size=2)
    vectors = await embedder.embed(["a", "b", "c", "d", "e"])
    assert len(vectors) == 5
    assert [len(item["body"]["input"]) for item in state["received"]] == [2, 2, 1]


async def test_retries_transient_500_then_succeeds(embed_server):
    url, state = embed_server
    state["fail_times"] = 1
    vectors = await mk_embedder(url).embed(["x"])
    assert len(vectors) == 1
    assert len(state["received"]) == 2


async def test_auth_error_is_not_retried(embed_server):
    url, state = embed_server
    state["status"] = 401
    with pytest.raises(EmbeddingError, match="HTTP 401") as excinfo:
        await mk_embedder(url).embed(["x"])
    assert "sk-test" not in str(excinfo.value)
    assert len(state["received"]) == 1


async def test_timeout_is_bounded_and_reported(embed_server):
    url, state = embed_server
    state["delay"] = 0.5
    embedder = mk_embedder(url, timeout_seconds=0.1, max_attempts=1)
    with pytest.raises(EmbeddingError, match="embedding 调用失败"):
        await embedder.embed(["x"])


async def test_dim_mismatch_is_rejected(embed_server):
    url, _ = embed_server
    embedder = mk_embedder(url, dim=8)      # 服务端固定返回 4 维
    with pytest.raises(EmbeddingError, match="维度不符"):
        await embedder.embed(["x"])


def test_wrong_item_count_is_rejected():
    embedder = HttpEmbedder(
        model="text-embedding-3-small", base_url="http://127.0.0.1:9/v1")
    with pytest.raises(EmbeddingError, match="条数不符"):
        embedder._parse('{"data": []}', 2)


# ── 缓存与装配 ────────────────────────────────────────────────────────────


class StubEmbedder:
    def __init__(self, dim: int, name: str = "stub") -> None:
        self.name = name
        self.dim = dim
        self.calls: list[list[str]] = []

    async def embed(self, texts):  # noqa: ANN001
        self.calls.append(list(texts))
        return [[1.0] * self.dim for _ in texts]


async def test_cache_key_includes_dim(tmp_path):
    cache = tmp_path / "embeddings.jsonl"
    first, second = StubEmbedder(2), StubEmbedder(3)
    await CachedEmbedder(first, cache).embed(["同一句话"])
    await CachedEmbedder(second, cache).embed(["同一句话"])
    assert len(first.calls) == 1 and len(second.calls) == 1


async def test_cache_hit_does_not_call_backend(tmp_path, embed_server):
    url, state = embed_server
    cache = tmp_path / "embeddings.jsonl"
    first = CachedEmbedder(mk_embedder(url), cache)
    await first.embed(["同一句话"])
    second = CachedEmbedder(mk_embedder(url), cache)
    await second.embed(["同一句话"])
    assert len(state["received"]) == 1
    assert second.name.startswith("cached:openai:")


def test_build_embedder_defaults_off(tmp_path):
    assert build_embedder(Settings(), output_dir=tmp_path) is None


def test_build_embedder_uses_http_channel(tmp_path):
    settings = Settings(
        embedding_enabled=True, embedding_batch_size=8,
        embedding_timeout_seconds=3.0)
    embedder = build_embedder(
        settings, output_dir=tmp_path,
        target=("text-embedding-3-small", "k", "http://127.0.0.1:9/v1"))
    assert embedder is not None
    assert embedder.name == "cached:openai:text-embedding-3-small"
    assert is_semantic_embedder(embedder) is True
    assert embedder._inner._batch_size == 8       # type: ignore[attr-defined]
    assert embedder._inner._timeout == 3.0        # type: ignore[attr-defined]


def test_embedding_target_prefers_configured_base_url():
    settings = Settings(
        embedding_model="openai:text-embedding-3-small",
        embedding_base_url="http://127.0.0.1:9/v1")
    target = embedding_target(settings)
    assert target is not None
    assert target[0] == "text-embedding-3-small"
    assert target[2] == "http://127.0.0.1:9/v1"


# ── 配置通道 ──────────────────────────────────────────────────────────────


def test_embedding_env_channels(monkeypatch):
    monkeypatch.setenv("AGENTCREW_EMBEDDING_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("AGENTCREW_EMBEDDING_TIMEOUT_SECONDS", "3.5")
    monkeypatch.setenv("AGENTCREW_EMBEDDING_BATCH_SIZE", "8")
    settings = Settings.load()
    assert settings.embedding_base_url == "http://127.0.0.1:9/v1"
    assert settings.embedding_timeout_seconds == 3.5
    assert settings.embedding_batch_size == 8


def test_embedding_yaml_channels_are_registered(tmp_path):
    path = tmp_path / "agentcrew.yaml"
    path.write_text(
        "embedding_base_url: http://127.0.0.1:9/v1\n"
        "embedding_timeout_seconds: 5\n"
        "embedding_batch_size: 16\n",
        encoding="utf-8")
    values = _yaml_values(path, strict=True)     # strict 会拒未知键
    assert values["embedding_base_url"] == "http://127.0.0.1:9/v1"
    assert values["embedding_timeout_seconds"] == 5
    assert values["embedding_batch_size"] == 16


# ── 规划期权重（B5 遗留 bug）─────────────────────────────────────────────


class RecordingStore:
    def __init__(self) -> None:
        self.seen: tuple | None = None

    async def query(self, query: str, *, limit: int, vector_weight: float):
        self.seen = (query, limit, vector_weight)
        return []


async def test_planning_query_uses_effective_weight():
    """哈希兜底时 ``_docs_vector_weight`` 已压 0，规划期不能读配置原值。"""
    crew = Crew.__new__(Crew)
    crew._docs_store = RecordingStore()
    crew._docs_vector_weight = 0.0
    crew._settings = SimpleNamespace(
        docs_rag_limit=4, docs_rag_vector_weight=0.5)
    await crew._docs_query_async("目标")
    assert crew._docs_store.seen == ("目标", 4, 0.0)
