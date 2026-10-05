前端页面完成：结构、样式与交互分三个文件交付，页面通过 fetch 对接后端 REST 接口，覆盖加载中、空列表、请求失败三种状态。本地查看方式见文末。

```html file=frontend/index.html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Todo List</title>
  <link rel="stylesheet" href="./styles.css" />
</head>
<body>
  <main class="app">
    <header class="app-header">
      <h1>Todo List</h1>
      <p class="subtitle">新增、完成、编辑与删除你的待办事项</p>
    </header>

    <form id="todo-form" class="todo-form" autocomplete="off">
      <label class="sr-only" for="todo-input">待办标题</label>
      <input
        id="todo-input"
        name="title"
        type="text"
        maxlength="120"
        placeholder="今天要做什么？"
        required
      />
      <button type="submit">添加</button>
    </form>

    <p id="status" class="status" role="status" aria-live="polite"></p>

    <ul id="todo-list" class="todo-list"></ul>

    <template id="todo-template">
      <li class="todo-item">
        <input class="todo-toggle" type="checkbox" aria-label="标记完成" />
        <span class="todo-title"></span>
        <button class="todo-edit" type="button">编辑</button>
        <button class="todo-delete" type="button">删除</button>
      </li>
    </template>

    <footer class="app-footer">
      <span>数据保存在服务端进程内，重启服务后清空。</span>
    </footer>
  </main>

  <script src="./app.js"></script>
</body>
</html>
```

```js file=frontend/app.js
// Todo List 单页交互：原生 fetch + DOM，无构建链、无第三方依赖。
const API_BASE = "/api/todos";

const form = document.getElementById("todo-form");
const titleInput = document.getElementById("todo-input");
const listEl = document.getElementById("todo-list");
const statusEl = document.getElementById("status");
const itemTemplate = document.getElementById("todo-template");

function setStatus(message, kind = "info") {
  statusEl.textContent = message;
  statusEl.dataset.kind = kind;
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    let detail = `请求失败（HTTP ${response.status}）`;
    try {
      const body = await response.json();
      if (body && typeof body.detail === "string") {
        detail = body.detail;
      } else if (body && body.detail) {
        detail = "请求体不合法，请检查输入。";
      }
    } catch (error) {
      // 响应不是 JSON：保留默认提示
    }
    throw new Error(detail);
  }
  if (response.status === 204) {
    return null;
  }
  return response.json();
}

function renderItem(todo) {
  const node = itemTemplate.content.firstElementChild.cloneNode(true);
  node.dataset.id = todo.id;
  node.classList.toggle("done", todo.done);
  node.querySelector(".todo-toggle").checked = todo.done;
  node.querySelector(".todo-title").textContent = todo.title;
  return node;
}

function renderTodos(todos) {
  listEl.replaceChildren();
  if (!todos.length) {
    setStatus("还没有待办事项，先添加一条吧。", "empty");
    return;
  }
  const fragment = document.createDocumentFragment();
  todos.forEach((todo) => fragment.append(renderItem(todo)));
  listEl.append(fragment);
  const remaining = todos.filter((todo) => !todo.done).length;
  setStatus(`共 ${todos.length} 条待办，未完成 ${remaining} 条。`, "ok");
}

async function loadTodos() {
  setStatus("加载中……", "loading");
  const todos = await request(API_BASE);
  renderTodos(todos);
}

async function refresh() {
  try {
    await loadTodos();
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function mutate(action) {
  try {
    await action();
  } catch (error) {
    setStatus(error.message, "error");
    return;
  }
  await refresh();
}

function startEditing(node, todo) {
  const titleEl = node.querySelector(".todo-title");
  const editor = document.createElement("input");
  editor.type = "text";
  editor.className = "todo-edit-input";
  editor.maxLength = 120;
  editor.value = todo.title;
  titleEl.replaceWith(editor);
  editor.focus();
  editor.setSelectionRange(editor.value.length, editor.value.length);

  let settled = false;
  const finish = (save) => {
    if (settled) {
      return;
    }
    settled = true;
    const nextTitle = editor.value.trim();
    if (!save || !nextTitle || nextTitle === todo.title) {
      refresh();
      return;
    }
    mutate(() =>
      request(`${API_BASE}/${todo.id}`, {
        method: "PATCH",
        body: JSON.stringify({ title: nextTitle }),
      })
    );
  };

  editor.addEventListener("blur", () => finish(true));
  editor.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      finish(true);
    }
    if (event.key === "Escape") {
      event.preventDefault();
      finish(false);
    }
  });
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const title = titleInput.value.trim();
  if (!title) {
    setStatus("标题不能为空。", "error");
    return;
  }
  mutate(async () => {
    await request(API_BASE, {
      method: "POST",
      body: JSON.stringify({ title }),
    });
    titleInput.value = "";
    titleInput.focus();
  });
});

listEl.addEventListener("change", (event) => {
  const toggle = event.target.closest(".todo-toggle");
  if (!toggle) {
    return;
  }
  const node = toggle.closest(".todo-item");
  mutate(() =>
    request(`${API_BASE}/${node.dataset.id}`, {
      method: "PATCH",
      body: JSON.stringify({ done: toggle.checked }),
    })
  );
});

listEl.addEventListener("click", (event) => {
  const node = event.target.closest(".todo-item");
  if (!node) {
    return;
  }
  if (event.target.closest(".todo-delete")) {
    mutate(() => request(`${API_BASE}/${node.dataset.id}`, { method: "DELETE" }));
    return;
  }
  if (event.target.closest(".todo-edit")) {
    startEditing(node, {
      id: node.dataset.id,
      title: node.querySelector(".todo-title").textContent,
    });
  }
});

loadTodos().catch((error) => setStatus(error.message, "error"));
```

```css file=frontend/styles.css
:root {
  --bg: #f4f6fb;
  --surface: #ffffff;
  --text: #1f2937;
  --muted: #6b7280;
  --accent: #2563eb;
  --accent-weak: #dbeafe;
  --danger: #dc2626;
  --border: #e5e7eb;
  --radius: 12px;
  --shadow: 0 10px 30px rgba(31, 41, 55, 0.08);
}

* {
  box-sizing: border-box;
}

body {
  margin: 0;
  min-height: 100vh;
  display: flex;
  justify-content: center;
  padding: 48px 16px;
  background: var(--bg);
  color: var(--text);
  font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
}

.app {
  width: 100%;
  max-width: 640px;
  background: var(--surface);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  padding: 28px 28px 20px;
}

.app-header h1 {
  margin: 0 0 4px;
  font-size: 26px;
}

.subtitle {
  margin: 0 0 20px;
  color: var(--muted);
  font-size: 14px;
}

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
}

.todo-form {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}

.todo-form input {
  flex: 1;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  font-size: 15px;
}

.todo-form input:focus {
  outline: 2px solid var(--accent-weak);
  border-color: var(--accent);
}

.todo-form button {
  padding: 10px 18px;
  border: none;
  border-radius: 8px;
  background: var(--accent);
  color: #fff;
  font-size: 15px;
  cursor: pointer;
}

.todo-form button:hover {
  background: #1d4ed8;
}

.status {
  min-height: 20px;
  margin: 0 0 12px;
  font-size: 13px;
  color: var(--muted);
}

.status[data-kind="error"] {
  color: var(--danger);
}

.status[data-kind="ok"] {
  color: #047857;
}

.status[data-kind="loading"] {
  color: var(--accent);
}

.todo-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.todo-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: #fbfcfe;
}

.todo-item .todo-title {
  flex: 1;
  font-size: 15px;
  word-break: break-word;
}

.todo-item.done .todo-title {
  text-decoration: line-through;
  color: var(--muted);
}

.todo-item .todo-edit-input {
  flex: 1;
  padding: 6px 8px;
  border: 1px solid var(--accent);
  border-radius: 6px;
  font-size: 15px;
}

.todo-item button {
  border: 1px solid var(--border);
  border-radius: 6px;
  background: #fff;
  color: var(--muted);
  font-size: 13px;
  padding: 4px 10px;
  cursor: pointer;
}

.todo-item button:hover {
  color: var(--accent);
  border-color: var(--accent);
}

.todo-item .todo-delete:hover {
  color: var(--danger);
  border-color: var(--danger);
}

.todo-toggle {
  width: 18px;
  height: 18px;
  accent-color: var(--accent);
  cursor: pointer;
}

.app-footer {
  margin-top: 20px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
  color: var(--muted);
  font-size: 12px;
}

@media (max-width: 480px) {
  body {
    padding: 16px 8px;
  }

  .app {
    padding: 20px 16px;
  }

  .todo-item button {
    padding: 4px 8px;
  }
}
```

本地查看方式：启动后端后访问 http://127.0.0.1:8000 ，后端会把本目录挂载到根路径；也可以直接双击 `index.html` 查看静态结构（此时接口请求会失败并显示错误提示，这属于预期行为）。三个文件之间没有构建步骤，改完刷新浏览器即可生效。
