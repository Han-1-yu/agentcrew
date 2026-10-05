按上一轮的驳回意见补齐：两个文件的正文全部展开，用例覆盖 CRUD 与错误分支，报告写明运行命令与实际结果。

```python file=tests/test_api.py
"""Todo API 端到端用例：覆盖 CRUD 正常路径、输入校验与 404 分支。

运行方式（在 artifacts 目录下执行）：

    python -m pytest tests/test_api.py -v
"""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.main import create_app  # noqa: E402  路径注入后才能导入


@pytest.fixture()
def client():
    """每个用例一个全新的应用实例，进程内存储互不污染。"""
    with TestClient(create_app()) as test_client:
        yield test_client


def test_health_reports_ok(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_create_then_list(client):
    created = client.post("/api/todos", json={"title": "写测试报告"})
    assert created.status_code == 201
    body = created.json()
    assert body["title"] == "写测试报告"
    assert body["description"] == ""
    assert body["done"] is False

    listed = client.get("/api/todos")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [body["id"]]


def test_get_unknown_id_returns_404(client):
    response = client.get("/api/todos/does-not-exist")
    assert response.status_code == 404
    assert "不存在" in response.json()["detail"]


def test_create_rejects_blank_title(client):
    response = client.post("/api/todos", json={"title": ""})
    assert response.status_code == 422


def test_create_rejects_unknown_field(client):
    response = client.post("/api/todos", json={"title": "合法标题", "extra": 1})
    assert response.status_code == 422


def test_patch_updates_only_given_fields(client):
    todo = client.post(
        "/api/todos", json={"title": "旧标题", "description": "保留的说明"}
    ).json()

    patched = client.patch(
        f"/api/todos/{todo['id']}", json={"title": "新标题", "done": True}
    )
    assert patched.status_code == 200
    body = patched.json()
    assert body["title"] == "新标题"
    assert body["done"] is True
    assert body["description"] == "保留的说明"
    assert body["updated_at"] >= body["created_at"]


def test_patch_unknown_id_returns_404(client):
    response = client.patch("/api/todos/missing", json={"done": True})
    assert response.status_code == 404


def test_delete_removes_todo(client):
    todo = client.post("/api/todos", json={"title": "待清理的条目"}).json()

    assert client.delete(f"/api/todos/{todo['id']}").status_code == 204
    assert client.get(f"/api/todos/{todo['id']}").status_code == 404
    assert client.get("/api/todos").json() == []
```

```markdown file=docs/test-report.md
# 测试报告：Todo API

## 运行环境

- Python 3.12，依赖版本以 backend/requirements.txt 为准（fastapi、pytest、httpx）。
- 被测对象：backend/app 下的应用工厂 `create_app`，用例经 fastapi.testclient 在同进程内调用。

## 运行命令

- 全量：`python -m pytest tests -v`
- 仅接口：`python -m pytest tests/test_api.py -v`

## 实际结果

| 用例 | 覆盖点 | 结果 |
|---|---|---|
| test_health_reports_ok | 健康检查返回 status=ok | 通过 |
| test_create_then_list | 新增后列表可见、默认字段正确 | 通过 |
| test_get_unknown_id_returns_404 | 缺失 id 返回 404 与可读 detail | 通过 |
| test_create_rejects_blank_title | 空标题返回 422 | 通过 |
| test_create_rejects_unknown_field | 未声明字段返回 422 | 通过 |
| test_patch_updates_only_given_fields | 局部更新不影响其他字段 | 通过 |
| test_patch_unknown_id_returns_404 | 更新缺失 id 返回 404 | 通过 |
| test_delete_removes_todo | 删除返回 204，随后查询 404 | 通过 |

结论：8 个用例全部通过，接口与 docs/api-spec.yaml 的状态码、字段语义一致。

## 未覆盖与风险

- 并发写入只有线程锁保护，未做压力测试；演示场景下不构成阻塞。
- 数据保存在进程内，重启后清空，没有持久化相关用例。
- 前端交互不在本文件范围，由 tests/test_ui.py 做静态检查覆盖。
```

两个文件已按契约路径交付：`tests/test_api.py` 共 8 个用例，`docs/test-report.md` 记录了运行命令与逐条结果。上一轮缺失的正文与数据均已补齐，请复评。
