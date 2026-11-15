/**
 * AgentCrew Node / TypeScript SDK —— 零依赖、薄封装。
 *
 * 契约来源是 `artifacts/contracts/openapi.yaml`（以 `agentcrew/api/server.py`
 * 为准）。设计取舍：
 *
 * 1. **零运行时依赖**：只用全局 `fetch`（Node 18+）与标准 `URL` / `AbortController`，
 *    不把服务端的 openai / pydantic 依赖拖进调用方；`package.json` 没有任何
 *    `dependencies`，也不需要构建步骤。
 * 2. **薄**：每个方法就是一次 REST 调用，不缓存、不重试（`waitForRun` 的轮询除外），
 *    不替调用方决定 run 的生命周期。
 * 3. **错误可分支**：非 2xx 一律抛 `AgentCrewError`，带 `status` / `code` / `body`；
 *    网络错误 `status=0`，与「服务端返回 4xx」区分得开。
 * 4. **超时默认 30s**：与 MCP 客户端、Studio 的代理口径一致；SSE 长连接单独关掉。
 */

export class AgentCrewError extends Error {
  /**
   * @param {string} message
   * @param {{status?: number, code?: string, body?: unknown, cause?: unknown}} [meta]
   */
  constructor(message, { status = 0, code = "", body = null, cause } = {}) {
    super(message, cause === undefined ? undefined : { cause });
    this.name = "AgentCrewError";
    this.status = status;
    this.code = code;
    this.body = body;
  }
}

const TERMINAL = new Set(["completed", "failed", "cancelled", "interrupted"]);

/** SSE 帧 → `{event, data}`；注释行（`: ping`）与空帧返回 null。 */
export function parseSseFrame(frame) {
  let event = "message";
  const dataLines = [];
  for (const line of String(frame).split("\n")) {
    if (!line || line.startsWith(":")) continue;
    const index = line.indexOf(":");
    const field = index === -1 ? line : line.slice(0, index);
    const value = index === -1 ? "" : line.slice(index + 1).replace(/^ /, "");
    if (field === "event") event = value;
    else if (field === "data") dataLines.push(value);
  }
  if (dataLines.length === 0) return null;
  const raw = dataLines.join("\n");
  let data = raw;
  try {
    data = JSON.parse(raw);
  } catch {
    // 非 JSON 的 data 原样返回：SSE 规范允许任意文本。
  }
  return { event, data };
}

function sleep(ms, signal) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    if (signal) {
      signal.addEventListener("abort", () => {
        clearTimeout(timer);
        reject(new AgentCrewError("等待被取消", { code: "aborted" }));
      }, { once: true });
    }
  });
}

export class AgentCrewClient {
  /**
   * @param {{baseUrl: string, apiKey?: string, fetchImpl?: typeof fetch,
   *          timeoutMs?: number}} options
   */
  constructor({ baseUrl, apiKey = "", fetchImpl = globalThis.fetch, timeoutMs = 30000 } = {}) {
    if (!baseUrl) throw new Error("AgentCrewClient 需要 baseUrl");
    if (typeof fetchImpl !== "function") {
      throw new Error("当前运行时没有 fetch（Node 18+ 自带），请传 fetchImpl");
    }
    this.baseUrl = String(baseUrl).replace(/\/+$/, "");
    this.apiKey = apiKey || "";
    this.timeoutMs = timeoutMs;
    this._fetch = fetchImpl;
  }

  // ── 底层 ────────────────────────────────────────────────────────────────

  async _request(method, path, { query, body, timeoutMs = this.timeoutMs, signal, raw = false } = {}) {
    const url = new URL(this.baseUrl + path);
    for (const [key, value] of Object.entries(query || {})) {
      if (value !== undefined && value !== null && value !== "") {
        url.searchParams.set(key, String(value));
      }
    }
    const headers = { Accept: "application/json" };
    if (this.apiKey) headers.Authorization = `Bearer ${this.apiKey}`;
    let payload;
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      payload = JSON.stringify(body);
    }
    const controller = new AbortController();
    const onAbort = () => controller.abort(signal.reason);
    if (signal) signal.addEventListener("abort", onAbort, { once: true });
    const timer = timeoutMs > 0
      ? setTimeout(() => controller.abort(new Error("timeout")), timeoutMs)
      : null;
    let response;
    try {
      response = await this._fetch(url, { method, headers, body: payload, signal: controller.signal });
    } catch (error) {
      throw new AgentCrewError(`请求失败：${method} ${path}`, {
        status: 0, code: "network_error", cause: error,
      });
    } finally {
      if (timer) clearTimeout(timer);
      if (signal) signal.removeEventListener("abort", onAbort);
    }
    if (raw) return response;
    if (response.status === 204) return null;
    const text = await response.text();
    let data = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        data = text;
      }
    }
    if (!response.ok) {
      const bodyError = data && typeof data === "object" ? (data.error || data.message) : "";
      throw new AgentCrewError(String(bodyError || `HTTP ${response.status}`), {
        status: response.status,
        code: (data && typeof data === "object" && data.code) || "",
        body: data,
      });
    }
    return data;
  }

  // ── 运行 ────────────────────────────────────────────────────────────────

  health() {
    return this._request("GET", "/health");
  }

  listRuns() {
    return this._request("GET", "/runs");
  }

  /** @returns {Promise<{run_id: string, status: string}>} 202 */
  createRun(goal, options = {}) {
    return this._request("POST", "/runs", { body: { goal, options } });
  }

  getStatus(runId) {
    return this._request("GET", `/runs/${encodeURIComponent(runId)}/status`);
  }

  getStats(runId) {
    return this._request("GET", `/runs/${encodeURIComponent(runId)}/stats`);
  }

  getLedger(runId, { event, limit, offset } = {}) {
    return this._request("GET", `/runs/${encodeURIComponent(runId)}/ledger`, {
      query: { event, limit, offset },
    });
  }

  cancelRun(runId) {
    return this._request("POST", `/runs/${encodeURIComponent(runId)}/cancel`, { body: {} });
  }

  resumeRun(runId) {
    return this._request("POST", `/runs/${encodeURIComponent(runId)}/resume`, { body: {} });
  }

  /** R62：回滚到任务起点；`dryRun` 只算影响集、不写盘。 */
  rewindRun(runId, { task, instruction = "", dryRun = false } = {}) {
    if (!task) throw new Error("rewindRun 需要 task");
    return this._request("POST", `/runs/${encodeURIComponent(runId)}/rewind`, {
      body: { task, instruction, dry_run: Boolean(dryRun) },
    });
  }

  /** 轮询到终态（或 `awaiting_approval`）；超时返回最后一次状态 + `timedOut`。 */
  async waitForRun(runId, { timeoutMs = 600000, intervalMs = 2000, signal } = {}) {
    const deadline = Date.now() + timeoutMs;
    let last = null;
    while (true) {
      last = await this.getStatus(runId);
      const status = String((last && last.status) || "").toLowerCase();
      if (TERMINAL.has(status) || status === "awaiting_approval") return last;
      if (Date.now() >= deadline) return { ...last, timedOut: true };
      if (signal && signal.aborted) {
        throw new AgentCrewError("等待被取消", { code: "aborted" });
      }
      await sleep(intervalMs, signal);
    }
  }

  // ── 审批 ────────────────────────────────────────────────────────────────

  listApprovals(runId) {
    return this._request("GET", `/runs/${encodeURIComponent(runId)}/approvals`);
  }

  decideApproval(runId, requestId, decision, { decidedBy } = {}) {
    if (!requestId || !decision) {
      throw new Error("decideApproval 需要 requestId 与 decision");
    }
    const body = { request_id: requestId, decision };
    if (decidedBy) body.decided_by = decidedBy;
    return this._request("POST", `/runs/${encodeURIComponent(runId)}/approvals`, { body });
  }

  // ── 指引（C10）──────────────────────────────────────────────────────────

  getGuidance(runId) {
    return this._request("GET", `/runs/${encodeURIComponent(runId)}/guidance`);
  }

  /** `text` 可传字符串，或 `{text, target, expiresInSeconds, author}`。 */
  addGuidance(runId, text, { target, expiresInSeconds, author } = {}) {
    const body = typeof text === "string" ? { text } : { ...text };
    if (body.target === undefined && target !== undefined) body.target = target;
    if (body.expires_in_seconds === undefined && expiresInSeconds !== undefined) {
      body.expires_in_seconds = expiresInSeconds;
    }
    if (body.author === undefined && author !== undefined) body.author = author;
    return this._request("POST", `/runs/${encodeURIComponent(runId)}/guidance`, { body });
  }

  withdrawGuidance(runId, guidanceId) {
    return this._request(
      "DELETE",
      `/runs/${encodeURIComponent(runId)}/guidance/${encodeURIComponent(guidanceId)}`,
    );
  }

  compactGuidance(runId) {
    return this._request(
      "POST", `/runs/${encodeURIComponent(runId)}/guidance/compact`, { body: {} },
    );
  }

  // ── 产物（C9）───────────────────────────────────────────────────────────

  listArtifacts(runId) {
    return this._request("GET", `/runs/${encodeURIComponent(runId)}/artifacts`);
  }

  readArtifact(runId, path, { maxBytes } = {}) {
    if (!path) throw new Error("readArtifact 需要 path");
    return this._request("GET", `/runs/${encodeURIComponent(runId)}/artifacts`, {
      query: { path, max_bytes: maxBytes },
    });
  }

  // ── 事件流 ──────────────────────────────────────────────────────────────

  /** 逐帧产出 SSE 事件（`{event, data}`）；调用方用 `signal` 或 `for await ... break` 收尾。 */
  async *iterEvents(runId, { signal } = {}) {
    const response = await this._request(
      "GET",
      `/runs/${encodeURIComponent(runId)}/events`,
      { raw: true, timeoutMs: 0, signal },
    );
    if (!response.ok || !response.body) {
      throw new AgentCrewError(`事件流不可读：HTTP ${response.status}`, {
        status: response.status,
      });
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) return;
        buffer += decoder.decode(value, { stream: true });
        let index = buffer.indexOf("\n\n");
        while (index >= 0) {
          const frame = buffer.slice(0, index);
          buffer = buffer.slice(index + 2);
          const parsed = parseSseFrame(frame);
          if (parsed) yield parsed;
          index = buffer.indexOf("\n\n");
        }
      }
    } finally {
      reader.releaseLock();
    }
  }
}

export default AgentCrewClient;
