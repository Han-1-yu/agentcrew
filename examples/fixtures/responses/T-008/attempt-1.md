前端没有浏览器测试依赖，因此用静态检查覆盖：直接读取 frontend/ 下的三个文件，断言页面结构、资源引用、关键类名与 API 调用点。用例同样以 pytest 运行。

```python file=tests/test_ui.py
"""前端静态检查：不启动浏览器，直接断言文件内容与约定一致。

运行方式（在 artifacts 目录下执行）：

    python -m pytest tests/test_ui.py -v
"""

import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


@pytest.fixture(scope="module")
def index_html() -> str:
    return (FRONTEND / "index.html").read_text(encoding="utf-8")


def test_index_references_local_assets(index_html):
    assert 'href="./styles.css"' in index_html
    assert 'src="./app.js"' in index_html


def test_index_has_required_element_ids(index_html):
    for element_id in ("todo-form", "todo-input", "todo-list", "status"):
        assert f'id="{element_id}"' in index_html, f"缺少元素 #{element_id}"


def test_index_declares_language_and_encoding(index_html):
    assert 'lang="zh-CN"' in index_html
    assert 'charset="utf-8"' in index_html


def test_index_loads_single_script(index_html):
    assert index_html.count("<script") == 1
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", index_html), "不允许内联脚本"


def test_app_js_calls_todo_api():
    script = (FRONTEND / "app.js").read_text(encoding="utf-8")
    assert '"/api/todos"' in script
    assert "fetch(" in script
    assert "renderTodos" in script


def test_app_js_handles_edit_and_delete():
    script = (FRONTEND / "app.js").read_text(encoding="utf-8")
    assert '"PATCH"' in script
    assert '"DELETE"' in script


def test_styles_cover_key_selectors():
    styles = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    for selector in (".todo-list", ".todo-item", ".status"):
        assert selector in styles, f"样式缺少 {selector}"
```

静态检查共 7 个用例：资源引用、元素 id、编码与语言、脚本加载方式、API 调用点、编辑删除分支与关键样式选择器。该文件只断言结构约定，真正的交互行为需要浏览器环境，未覆盖部分记入 docs/test-report.md 的风险清单。
