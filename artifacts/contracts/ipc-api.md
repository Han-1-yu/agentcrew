# AgentCrew Studio — IPC 通道契约

逆向自 `studio/preload.js`（contextBridge 暴露面）与 `studio/main.js`（主进程 `ipcMain.handle` 注册）。
本文件**只读分析**，未修改任何源码。

- 暴露对象：`window.studio`（`contextBridge.exposeInMainWorld('studio', {...})`，preload.js:7）
- 调用约定：preload 内统一用 `ipcRenderer.invoke(channel, ...args)`（preload.js:5），因此**除 `on()` 外所有方法都返回 `Promise`**。
- 错误约定：主进程 handler 内 `throw new Error(...)` 会被 Electron 序列化为 **reject**（渲染侧 `await` 抛错）；个别 handler 返回 `{ error }` / `{ ok: false }` 形式，逐条标注。
- 主进程推送（渲染侧订阅）：仅通过 `window.studio.on(channel, cb)` 接收，见文末「主进程 → 渲染进程推送通道」。

> 备注：`main.js:592` 还注册了 `ipcMain.handle('app:relaunch', ...)`，但 **preload.js 未暴露该通道**，渲染进程无法调用。

---

## 一、设置与探测

### `getSettings()`
- **签名**：`getSettings(): Promise<{ settings: Settings, env: { repoOk: boolean, outOk: boolean, electron: string, node: string } }>`
- **参数**：无
- **同步/异步**：异步（`invoke`）
- **IPC channel**：`settings:get`（main.js:517）
- **主进程处理**：`registerIpc()` 内联箭头函数，main.js:517–521
- **返回结构**：
  - `settings` = 持久化设置对象（见 `defaultSettings()`，main.js:40–49）：
    `{ repoRoot: string, outputDir: string, pythonCmd: string, theme: 'dark'|..., defaultMock: boolean, defaultAuto: boolean }`
  - `env.repoOk`：`settings.repoRoot/main.py` 是否存在；`env.outOk`：outputDir 是否存在；`electron`/`node`：运行时版本串。
- **错误处理**：不抛（设置读取失败时回退默认值，main.js:51–59）。
- **示例**：`const { settings } = await window.studio.getSettings();`

### `setSettings(next)`
- **签名**：`setSettings(next: Partial<Settings>): Promise<Settings>`
- **参数**：`next: Partial<Settings>`（对象，浅合并进当前设置；存盘到 `userData/settings.json`）
- **同步/异步**：异步
- **IPC channel**：`settings:set`（main.js:522）
- **主进程处理**：`saveSettings()`，main.js:61–66（handler main.js:522）
- **返回**：合并并落盘后的完整 `Settings` 对象。
- **错误处理**：写盘异常会抛出（reject）。
- **示例**：`await window.studio.setSettings({ theme: 'dark', pythonCmd: 'python' });`

### `detectPython()`
- **签名**：`detectPython(): Promise<{ cmd: string, version: string } | null>`
- **参数**：无（preload 未透传 candidate；handler 接受可选 candidate 但渲染侧不传，main.js:523）
- **同步/异步**：异步（handler 为 `async`）
- **IPC channel**：`python:detect`（main.js:523）
- **主进程处理**：`detectPython()` → `tryCommand()` / `resolveAbsolute()`，main.js:118–131 / 72–116
- **返回**：探测到解释器时 `{ cmd: 绝对路径或原命令, version: 'Python 3.x.x' }`；候选链（settings.pythonCmd → `python` → `python3` → `py -3`）全部不可用返回 `null`。
- **错误处理**：内部吞掉 ENOENT/超时并重试，不 reject；最终失败返回 `null`。
- **示例**：`const py = await window.studio.detectPython();`

---

## 二、运行列表 / 快照 / 事件

### `listRuns()`
- **签名**：`listRuns(): Promise<RunSummary[]>`
- **参数**：无（扫描 `settings.outputDir`）
- **同步/异步**：异步
- **IPC channel**：`runs:list`（main.js:525）
- **主进程处理**：`scanRuns()` → `summarizeRun()`，main.js:154–175 / 177–198
- **返回 `RunSummary[]`**（按 createdAt/mtime 倒序）：
  `{ runId: string, dir: string, group: string|null, goal: string, status: string, createdAt: string, updatedAt: string, stats: object|null, error: string|null, mtime: number }`
- **错误处理**：outputDir 不可读时 **reject**（`无法读取输出目录 …`，main.js:158）；单个 run.json 损坏不抛，记入 `error` 字段。
- **示例**：`const runs = await window.studio.listRuns();`

### `loadRun(dir)`
- **签名**：`loadRun(dir: string): Promise<{ run: object|null, tasks: Task[]|null, errors: { run: string|null, tasks: string|null } }>`
- **参数**：`dir: string`（run 目录绝对路径）
- **同步/异步**：异步
- **IPC channel**：`run:load`（main.js:526）
- **主进程处理**：`readRunSnapshot()`，main.js:200–211
- **返回**：读取 `<dir>/run.json` 与 `<dir>/tasks.json`；`tasks` 取 `tasks.data.tasks || []`；文件解析失败时对应字段为 `null` 且 `errors.*` 给出原因。
- **错误处理**：文件不存在/坏 JSON 不 reject，体现在 `run`/`tasks` 为 `null` + `errors`。
- **示例**：`const snap = await window.studio.loadRun(runDir);`

### `ledgerHistory(dir, maxLines)`
- **签名**：`ledgerHistory(dir: string, maxLines?: number): Promise<{ offset: number, events: object[], skipped: number }>`
- **参数**：`dir: string`（run 目录）；`maxLines?: number`（默认 400，main.js:528）
- **同步/异步**：异步
- **IPC channel**：`ledger:history`（main.js:527）
- **主进程处理**：`ledgerHistory()`，main.js:238–250（读 `<dir>/ledger.jsonl` 末尾 N 行）
- **返回**：`offset`（已消费字节数，供增量续读）、`events`（解析后的事件对象数组）、`skipped`（因取末尾 N 行而跳过的行数）。
- **错误处理**：文件不存在返回 `{ offset: 0, events: [], skipped: 0 }`，不 reject；坏行静默跳过。
- **示例**：`const h = await window.studio.ledgerHistory(runDir, 400);`

---

## 三、子进程（run 管理）

### `startRun(p)`
- **签名**：`startRun(p: { goal?: string, demo?: boolean, mock?: boolean, auto?: boolean, maxCost?: number, maxLlmCalls?: number, outputDir?: string, env?: object }): Promise<{ key: string }>`
- **参数**（渲染侧 `p` 对象，main.js:530–544）：
  - `demo?: boolean` — 为真走 `--demo`（内置 Todo Web 目标）；否则必须给 `goal`
  - `goal?: string` — 项目目标文本（映射为 `--goal TEXT`）
  - `mock?: boolean` — `--mock`
  - `auto?: boolean` — `--auto`（等价 `--gate none`）
  - `maxCost?: number` — `--max-cost USD`（>0 才追加）
  - `maxLlmCalls?: number` — `--max-llm-calls N`（>0 才追加）
  - `outputDir?: string` — 默认 `settings.outputDir`；追加 `--output-dir`
  - `env?: object` — 额外环境变量，合并进子进程 env
  - 固定追加：`--no-live`
- **同步/异步**：异步（立即返回进程 key，不等 run 结束）
- **IPC channel**：`run:start`（main.js:530）
- **主进程处理**：构造 args → `spawnCli('run', args, outputDir, p.env)`，main.js:264–335
- **返回**：`{ key: 'p1' }`（进程句柄，后续 cancel/listProcs 用）。真实退出码通过推送 `proc:exit` 回报。
- **错误处理**：`r.error` 存在时 **reject**（当前代码路径几乎不产生，启动失败改由 `proc:error` 推送）。
- **示例**：`const { key } = await window.studio.startRun({ goal: '做一个待办网页', mock: true, auto: true });`

### `resumeRun(p)`
- **签名**：`resumeRun(p: { runDir: string, mock?: boolean, auto?: boolean }): Promise<{ key: string }>`
- **参数**：`p.runDir: string`（必填，run 目录绝对路径；取 basename 作 run_id，dirname 作 output-dir）；`mock?`/`auto?` 同上。固定追加 `--no-live --output-dir <dirname(runDir)>`。
- **同步/异步**：异步
- **IPC channel**：`run:resume`（main.js:546）
- **主进程处理**：args = `['resume', basename]` → `spawnCli('resume', ...)`，main.js:546–559
- **返回**：`{ key }`；主进程立即把 `runDir/runId` 写进进程记录并推送 `proc:run-dir`。
- **错误处理**：`r.error` 时 reject。
- **示例**：`await window.studio.resumeRun({ runDir: 'E:/agentcrew/output/20261006-xxx', mock: true, auto: true });`

### `cancelRun(key)`
- **签名**：`cancelRun(key: string): Promise<{ killed: boolean }>`
- **参数**：`key: string`（startRun/resumeRun 返回的进程 key；preload 包成 `{ key }` 发送，preload.js:19）
- **同步/异步**：异步
- **IPC channel**：`run:cancel`（main.js:561）
- **主进程处理**：`killProc(key)`，main.js:363–383（硬 kill + Windows taskkill 进程树 + 离线 cancel 收尾）
- **返回**：`{ killed: true/false }`（进程不存在或已退出返回 false）。
- **错误处理**：不 reject。
- **示例**：`await window.studio.cancelRun('p1');`

### `cancelOffline(runDir)`
- **签名**：`cancelOffline(runDir: string): Promise<{ ok: true }>`
- **参数**：`runDir: string`（绝对路径；preload 包成 `{ runDir }`）
- **同步/异步**：异步（fire-and-forget 式 spawn CLI `cancel`，最多重试 3 次，main.js:385–393）
- **IPC channel**：`run:cancelOffline`（main.js:562）
- **主进程处理**：`offlineCancel()`，main.js:385–393（等价 `python main.py cancel <basename> --output-dir <dirname>`）
- **返回**：恒为 `{ ok: true }`（不等待 CLI 实际完成）。
- **错误处理**：不 reject（收尾失败仅内部重试/忽略）。
- **示例**：`await window.studio.cancelOffline(runDir);`

### `deleteRun(runDir)`
- **签名**：`deleteRun(runDir: string): Promise<{ ok: true }>`
- **参数**：`runDir: string`（绝对路径；preload 包成 `{ runDir }`）
- **同步/异步**：异步（内部 `spawnSync` 同步等 CLI，最长 15s）
- **IPC channel**：`run:delete`（main.js:563）
- **主进程处理**：`deleteRunSync()`，main.js:395–410（等价 `python main.py delete <basename> --force --output-dir <dirname>`）
- **返回**：成功 `{ ok: true }`。
- **错误处理**：CLI 退出码非 0 时 **reject**（抛 stderr/stdout 内容）。
- **示例**：`await window.studio.deleteRun(runDir);`

### `listProcs()`
- **签名**：`listProcs(): Promise<Array<{ key: string, kind: 'run'|'resume', runDir: string|null, runId: string|null }>>`
- **参数**：无
- **同步/异步**：异步
- **IPC channel**：`procs:list`（main.js:564）
- **主进程处理**：内联（仅返回仍在运行、`exitCode === null` 的进程），main.js:564–566
- **返回**：存活进程数组；已退出的 60s 后自动清除（main.js:320）。
- **错误处理**：不 reject。
- **示例**：`const alive = await window.studio.listProcs();`

---

## 四、监视（ledger 增量 + 快照轮询）

### `watchRun(dir, tail)`
- **签名**：`watchRun(dir: string, tail?: boolean): Promise<{ ok: true }>`
- **参数**：`dir: string`（run 目录）；`tail?: boolean`（true 时先回放最近 400 行历史 ledger，main.js:424–430）
- **同步/异步**：异步
- **IPC channel**：`watch:start`（main.js:568）
- **主进程处理**：`watchRun()`，main.js:418–451（500ms 轮询 ledger 字节增量 + run.json/tasks.json mtime 变化）
- **返回**：`{ ok: true }`。已在监视的 run 会先重建监视器（保证 tail 重发）。
- **错误处理**：不 reject。增量事件通过推送 `watch:ledger` / `watch:snapshot` 下发。
- **示例**：`await window.studio.watchRun(runDir, true);`

### `unwatchRun(dir)`
- **签名**：`unwatchRun(dir: string): Promise<{ ok: true }>`
- **参数**：`dir: string`（run 目录）
- **同步/异步**：异步
- **IPC channel**：`watch:stop`（main.js:569）
- **主进程处理**：`unwatchRun()`，main.js:453–456
- **返回**：恒 `{ ok: true }`（未在监视也是 ok）。
- **错误处理**：不 reject。
- **示例**：`await window.studio.unwatchRun(runDir);`

---

## 五、产物

### `listArtifacts(dir)`
- **签名**：`listArtifacts(dir: string): Promise<{ entries: ArtifactEntry[], manifestError: string|null }>`
- **参数**：`dir: string`（run 目录，读其 `artifacts/` 子目录）
- **同步/异步**：异步
- **IPC channel**：`artifacts:list`（main.js:571）
- **主进程处理**：`listArtifacts()`，main.js:460–492（优先解析 `artifacts/MANIFEST.json`，无 manifest 退化为目录遍历）
- **返回 `ArtifactEntry`**：`{ path: string, kind: string, sizeBytes: number, exists: boolean, role: string, task: string }`
- **错误处理**：MANIFEST 坏不抛，记入 `manifestError`。
- **示例**：`const { entries } = await window.studio.listArtifacts(runDir);`

### `readArtifact(dir, relPath)`
- **签名**：`readArtifact(dir: string, relPath: string): Promise<{ binary: true, sizeBytes: number, path: string } | { binary: false, content: string, sizeBytes: number, truncated: boolean, path: string }>`
- **参数**：`dir: string`（run 目录）；`relPath: string`（相对 `artifacts/` 的路径）
- **同步/异步**：异步
- **IPC channel**：`artifact:read`（main.js:572）
- **主进程处理**：`readArtifact()`，main.js:494–512（防目录穿越：resolve 后必须仍在 artifacts/ 下；文本类型按扩展名/基名判断；文本最多读 2MB，超出 `truncated=true`）
- **返回**：非文本 → `{ binary: true, ... }`（不返回内容）；文本 → `{ binary: false, content, ... }`。
- **错误处理**：路径越界 **reject**（`非法的产物路径`）；文件不存在 **reject**（statSync 抛出）。
- **示例**：`const f = await window.studio.readArtifact(runDir, 'README.md');`

### `exportArtifacts(dir)`
- **签名**：`exportArtifacts(dir: string): Promise<{ canceled: true } | { saved: string, size: number }>`
- **参数**：`dir: string`（run 目录）
- **同步/异步**：异步（弹原生保存对话框）
- **IPC channel**：`artifacts:export`（main.js:574）
- **主进程处理**：`dialog.showSaveDialog` → `buildZipFromDir(artDir,'artifacts')`（来自 `./zip.js`）→ 写盘并 `shell.showItemInFolder`，main.js:574–588
- **返回**：用户取消 → `{ canceled: true }`；成功 → `{ saved: 保存路径, size: zip 字节数 }`。
- **错误处理**：写盘 IO 异常会 reject。
- **示例**：`const r = await window.studio.exportArtifacts(runDir);`

---

## 六、系统 / Shell

### `revealFile(fullPath)`
- **签名**：`revealFile(fullPath: string): Promise<{ ok: true }>`
- **参数**：`fullPath: string`（preload 包成 `{ fullPath }`，preload.js:31）
- **同步/异步**：异步
- **IPC channel**：`shell:showItemInFolder`（main.js:590）
- **主进程处理**：`shell.showItemInFolder(fullPath)`，main.js:590
- **返回**：`{ ok: true }`。
- **错误处理**：不抛（shell 自身错误静默）。
- **示例**：`window.studio.revealFile('E:/agentcrew/output/run.json');`

### `openPath(fullPath)`
- **签名**：`openPath(fullPath: string): Promise<string>`
- **参数**：`fullPath: string`（preload 包成 `{ fullPath }`）
- **同步/异步**：异步
- **IPC channel**：`shell:openPath`（main.js:591）
- **主进程处理**：`shell.openPath(fullPath)`，main.js:591
- **返回**：Electron `shell.openPath` 的结果——成功为 `''`，失败为错误字符串。
- **错误处理**：不 throw，失败信息在返回字符串里。
- **示例**：`await window.studio.openPath(runDir);`

---

## 七、事件订阅（主进程推送）

### `on(channel, cb)`
- **签名**：`on(channel: string, cb: (payload: any) => void): () => void`
- **参数**：`channel: string`（须是主进程 `send()` 过的通道）；`cb(payload)`（异常被 preload 吞掉并 console.error，preload.js:35）
- **同步/异步**：**同步注册**；返回一个**取消订阅函数**（调用即 `removeListener`）。
- **IPC channel**：直接 `ipcRenderer.on(channel, wrapped)`（非 invoke，preload.js:34–38）
- **主进程处理**：`send(channel, payload)`（main.js:258–260），在各子进程/监视器内调用
- **返回**：unsubscribe 函数。
- **错误处理**：回调内异常不外抛。
- **示例**：`const off = window.studio.on('proc:exit', p => console.log(p));`

### 主进程 → 渲染进程推送通道（main.js `send()` 实际使用）

| channel | payload | 触发点（main.js） |
|---|---|---|
| `proc:log` | `{ key: string, stream: 'out'\|'err', text: string }` | 子进程 stdout/stderr 数据（main.js:304,310） |
| `proc:exit` | `{ key: string, code: number, meaning: string }` | 子进程退出；meaning 见 EXIT_MEANINGS（main.js:15–22, 319） |
| `proc:run-dir` | `{ key: string, runDir: string, runId: string }` | 发现/指定新 run 目录（main.js:351,557） |
| `proc:error` | `{ key: string, message: string }` | Python 候选链全部启动失败（main.js:281） |
| `watch:ledger` | `{ dir: string, offset: number, events: object[], skipped?: number, initial?: boolean }` | ledger 增量 / 初始回放（main.js:428,439） |
| `watch:snapshot` | `{ dir: string, run, tasks, errors }` | run.json/tasks.json mtime 变化（main.js:445） |

渲染进程实际订阅了：`watch:ledger`、`watch:snapshot`、`proc:log`、`proc:run-dir`、`proc:exit`、`proc:error`（renderer.js:539,548,1125,1135,1145,1158）。

---

## 附：`window.studio` 方法总览（20 个）

`getSettings` · `setSettings` · `detectPython` · `listRuns` · `loadRun` · `ledgerHistory` · `startRun` · `resumeRun` · `cancelRun` · `cancelOffline` · `deleteRun` · `listProcs` · `watchRun` · `unwatchRun` · `listArtifacts` · `readArtifact` · `exportArtifacts` · `revealFile` · `openPath` · `on`
