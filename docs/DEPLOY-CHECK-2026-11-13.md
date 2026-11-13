# 上线自检记录（2026-11-13 · 本机）

> 对应 `docs/OPERATIONS.md` §8「上线前检查清单」。本文只记**本机真跑过**的项，
> 以及明确需要目标机/部署侧才能验的项——不把「代码里看着没问题」当成通过。

## 一、结论

- 本机可验的 9 项**全部通过**（含一次 `doctor` 误报修复）。
- 剩余 9 项依赖目标机 / 部署链路（TLS 反代、监控告警、数据盘、日志采集、
  密钥管理、webhook 接收端、成本上限、docker 执行校验、备份演练），清单见第四节。
- 引擎侧无 P0/P1 阻断项：全量 **3241 passed / 2 skipped / 0 failed**，
  C11 真实模型评测 23 条 **22 通过**（`bench/eval-live-2026-11-13.md`）。

## 二、本机已实测通过

| # | 检查项 | 命令 | 实测结果 |
| --- | --- | --- | --- |
| 1 | 环境诊断 | `python main.py doctor --json` | `ok=true`，6 项全 pass（python 3.12.16 / dependencies / api_keys / config / output_dir / `provider:deepseek:deepseek-chat` healthy 401ms） |
| 2 | 离线冒烟 | `python main.py run --sample todo --no-live --auto` | 9 任务 9 成功 / 0 失败 / 0 阻塞，返工 1 次，4s，产物落 `output/<run_id>/artifacts` |
| 3 | REST 认证（带 key） | `serve --host 127.0.0.1 --port 18080 --api-key <临时>` → `GET /health` | **200** |
| 4 | REST 认证（不带 key） | 同上 → `GET /health`（无 Authorization） | **401**（不泄露内容，符合「认证不变量」） |
| 5 | 可观测入口 | 同上 → `GET /metrics`（带 key） | **200**，首行 `agentcrew_up 1` + `agentcrew_build_info` |
| 6 | 无密钥拒绝起服 | `serve --host 127.0.0.1 --port 18081`（`AGENTCREW_API_KEYS` 置空） | **退出码 3**，stderr 给出三条修法（keys add / yaml / `--api-key`），与文档一致 |
| 7 | 清理干跑 | `python main.py clean --dry-run --protect-running` | 列出「将删除 225 个 run（保留最近 5 个）」，**未真删** |
| 8 | Studio 链路 | `cd studio && npm run check` / `npm run test:unit` / `npm run e2e` | 架构检查 73 模块 / 42 分片通过；单测 **18/18**；e2e **32/32**（截图 20 张 → `studio/e2e-out`） |
| 9 | 静态检查 | `ruff check agentcrew/ main.py tests/`；`mypy agentcrew/` | ruff 全绿；mypy 139 文件零错误 |

日志留证（未入版本库）：`output/_eval/doctor.json`、`smoke-todo.log`、
`serve-smoke.log`、`serve-nokey.err.log`、`studio-e2e.log`。

## 三、默认安全姿态核实（读 `agentcrew/config.py` 默认值）

| 配置 | 默认 | 含义 |
| --- | --- | --- |
| `api_auth_enabled` | `True` | REST 默认就要 key；服务不会「默认裸奔」 |
| `tools_enabled` | `False` | 内置工具链默认不接进 run |
| `code_exec_enabled` | `False` | 模型不能执行 Python |
| `sandbox_backend` | `local` | 默认后端无网络/资源隔离；要隔离必须显式切 `docker` |
| `search_provider` | `""` | 联网搜索关闭，`web_search` 不注册 |
| `gate_config.exec_verify` | 缺省关 | 执行校验（真跑 pytest）不生效 |
| `max_cost_usd` / `max_llm_calls` | `None` | **不限制**——部署必须显式设上限，否则一次多 Agent run 可能烧掉几十美元 |

## 四、必须由目标机 / 部署侧完成（本机无法证）

| # | 事项 | 判据 |
| --- | --- | --- |
| 1 | TLS 反代（Nginx/Caddy） | `client_max_body_size 4m`、SSE 关缓冲并放长读超时、`8765` 只监听内网 |
| 2 | 监控告警 | `/metrics` 已抓取，配「错误率」与「在跑 run 数长期不降」两条告警（`docs/OPERATIONS.md` §5.1） |
| 3 | 数据盘 | `AGENTCREW_OUTPUT_DIR` 指向独立盘、仅服务账号可写、容量告警 |
| 4 | 日志 | `AGENTCREW_LOG_JSON=1`，stderr 进采集，确认脱敏生效 |
| 5 | 密钥管理 | `.env` / `agentcrew.yaml` 不入库，密钥走密钥管理，备份加密限权 |
| 6 | 成本上限 | `AGENTCREW_MAX_COST_USD` / `MAX_LLM_CALLS` 按预算设死 |
| 7 | Webhook 接收端 | 按 `X-AgentCrew-Delivery` 幂等去重；明确 `_webhook_spool/` 位置 |
| 8 | 容器化（如用） | `output/` 挂卷、healthcheck 用 TCP 探活（`/health` 需认证） |
| 9 | 备份与恢复演练 | 配置快照 + `output/` 恢复一次；停服用 SIGTERM / `systemctl stop`，不用 `kill -9` |
| 10 | docker 执行后端（若开 `exec_verify`） | 镜像内有 pytest，跑过一次真实任务确认 `g1_exec_check` 为 `passed` 而非一直 `skipped` |

## 五、已知能力边界（不影响上线，但要写进交付说明）

- **docker 逃逸用例未真跑**：本机无 docker（`sandbox_backend=docker` 的容器从未启动过）。
- **vision 端到端缺多模态模型**：deepseek-chat 不支持图像，`supports_vision` 通道已实现待真机。
- **Claude Desktop / Playwright MCP 互通未验**：协议层 13 例已测，外部客户端未接。
- **RAG 命中率无对照数据**：需真实文档集 + embedding 端点。
- **评测两条口径待产品决策**（`docs/REMAINING-2026-11-13.md` D5）：文档类 `must_contain`
  逐字匹配；驳回类用例如何区分「门禁拒绝」与「规划期失败」。

## 六、本轮顺带修掉的一处误报

`doctor` 的可选依赖清单里列着 `aiohttp`，但**全仓没有任何 aiohttp 导入**（API/SSE
是自研实现，见 `agentcrew/api/server.py` 模块注释），于是每个新环境都会稳定收到
一条「可选依赖缺失」warn。误报会让运维习惯性忽略 doctor 的黄色输出，比漏报更坏——
已清空该清单并把原因写进代码注释（`agentcrew/cli/doctor.py::_OPTIONAL_DEPS`）。
修后 `doctor --json` 的 `dependencies` 由 `warn` 变 `pass`，`tests/test_doctor.py`
与 `tests/test_cli.py` 共 55 例全绿。
