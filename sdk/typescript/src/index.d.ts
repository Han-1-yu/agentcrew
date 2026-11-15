/** AgentCrew Node / TypeScript SDK 的类型声明（与 src/index.js 一一对应）。 */

export interface ClientOptions {
  /** 服务根地址，如 `http://127.0.0.1:8765`（末尾斜杠会被去掉）。 */
  baseUrl: string;
  /** API Key；服务端开启认证时必填。 */
  apiKey?: string;
  /** 自定义 fetch（默认用全局 fetch，Node 18+ 自带）。 */
  fetchImpl?: (input: any, init?: any) => Promise<any>;
  /** 单次请求超时（毫秒），默认 30000；SSE 长连接单独关掉。 */
  timeoutMs?: number;
}

export interface RunStatus {
  run_id: string;
  goal?: string;
  status: string;
  task_count?: number;
  tasks?: Record<string, unknown>;
  timedOut?: boolean;
  [key: string]: unknown;
}

export interface Artifact {
  path: string;
  kind: string;
  size_bytes: number;
  task_id?: string;
  role?: string;
}

export interface ArtifactList {
  run_id: string;
  count: number;
  artifacts: Artifact[];
}

export interface ArtifactContent {
  run_id: string;
  path: string;
  content: string;
  truncated: boolean;
  max_bytes: number;
}

export interface GuidanceItem {
  id: string;
  text: string;
  status: string;
  target?: string | null;
  expires_at?: string | null;
  consumed_at?: string | null;
  withdrawn_at?: string | null;
}

export interface GuidanceList {
  run_id: string;
  items: GuidanceItem[];
  pending?: number;
}

export interface Approval {
  request_id: string;
  [key: string]: unknown;
}

export interface LedgerPage {
  events: unknown[];
  total: number;
}

export interface SseEvent {
  event: string;
  data: unknown;
}

export interface RewindOptions {
  task: string;
  instruction?: string;
  dryRun?: boolean;
}

export interface RewindResult {
  run_id: string;
  target: string;
  affected: string[];
  dry_run: boolean;
  snapshot_dir?: string | null;
  discarded_artifacts?: number;
  [key: string]: unknown;
}

export interface WaitOptions {
  timeoutMs?: number;
  intervalMs?: number;
  signal?: AbortSignal;
}

export class AgentCrewError extends Error {
  status: number;
  code: string;
  body: unknown;
  constructor(
    message: string,
    meta?: { status?: number; code?: string; body?: unknown; cause?: unknown },
  );
}

export function parseSseFrame(frame: string): SseEvent | null;

export class AgentCrewClient {
  constructor(options: ClientOptions);
  baseUrl: string;
  apiKey: string;
  timeoutMs: number;

  health(): Promise<{ status: string }>;
  listRuns(): Promise<unknown>;
  createRun(goal: string, options?: Record<string, unknown>): Promise<{ run_id: string; status: string }>;
  getStatus(runId: string): Promise<RunStatus>;
  getStats(runId: string): Promise<Record<string, unknown>>;
  getLedger(runId: string, query?: { event?: string; limit?: number; offset?: number }): Promise<LedgerPage>;
  cancelRun(runId: string): Promise<unknown>;
  resumeRun(runId: string): Promise<unknown>;
  rewindRun(runId: string, options: RewindOptions): Promise<RewindResult>;
  waitForRun(runId: string, options?: WaitOptions): Promise<RunStatus>;

  listApprovals(runId: string): Promise<{ approvals?: Approval[]; [key: string]: unknown }>;
  decideApproval(
    runId: string,
    requestId: string,
    decision: Record<string, unknown>,
    options?: { decidedBy?: string },
  ): Promise<unknown>;

  getGuidance(runId: string): Promise<GuidanceList>;
  addGuidance(
    runId: string,
    text: string | { text: string; target?: string; expires_in_seconds?: number; author?: string },
    options?: { target?: string; expiresInSeconds?: number; author?: string },
  ): Promise<GuidanceItem>;
  withdrawGuidance(runId: string, guidanceId: string): Promise<unknown>;
  compactGuidance(runId: string): Promise<{ before_lines: number; after_lines: number; kept: number; removed: number }>;

  listArtifacts(runId: string): Promise<ArtifactList>;
  readArtifact(runId: string, path: string, options?: { maxBytes?: number }): Promise<ArtifactContent>;

  iterEvents(runId: string, options?: { signal?: AbortSignal }): AsyncGenerator<SseEvent>;
}

export default AgentCrewClient;
