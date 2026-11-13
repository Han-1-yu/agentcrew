# 云主机真实部署验收（2026-11-13）

**结论：AgentCrew 已在云主机以「回环 serve + TLS 反代」实际运行；原上线清单 10 项中
7 项闭环，剩余 3 项是这台机器的硬约束，不能用软件绕过。** 本实例只监听
`127.0.0.1`，使用本地测试 CA，不是公网生产入口。

## 1. 实际部署形态

| 组件 | 地址 / 路径 | 状态 |
| --- | --- | --- |
| 仓库 | `/home/user/agentcrew`，`main` | 验收时 `af3336d` |
| AgentCrew serve | `127.0.0.1:8765` | 运行中，API 认证开启 |
| nginx TLS 反代 | `https://localhost:18443` | 运行中，SSE 不缓冲 |
| 运行数据 | `/home/user/agentcrew-runtime/output` | 与仓库同一挂载点，故独立数据盘 FAIL |
| 密钥环境文件 | `/home/user/agentcrew-runtime/agentcrew.env` | `0600`，在仓库外，不包含在本文 |
| 日志 | `/home/user/agentcrew-runtime/logs/` | serve / nginx / 验收输出 |

serve 通过 `AGENTCREW_API_KEYS` 环境变量取密钥，命令行不含 key；
`AGENTCREW_MAX_COST_USD=5.0`、`AGENTCREW_LOG_JSON=true` 已配置。

## 2. 上线清单逐项结论

| # | 验收项 | 结论 | 实测证据 |
| --- | --- | --- | --- |
| 1 | TLS 反代 + SSE | ✅ 闭环 | 本地 CA 签发 `localhost` 证书；HTTPS 带 key 200、无 key 401。SSE 经反代收到 4,567 字节，响应 `Content-Type: text/event-stream`、`Transfer-Encoding: chunked`、`X-Accel-Buffering: no`，配置含 `proxy_buffering off` 与 3600s 读超时 |
| 2 | 监控告警 | ✅ 规则闭环 | `deploy/prometheus/agentcrew-rules.yml` 两条规则经真正 promtool 2.55.1 `check` + `test rules`：`AgentCrewDown` 与 `AgentCrewRunFailureRateHigh` 的触发/不触发用例均 `SUCCESS`。尚未接外部 Alertmanager |
| 3 | 独立数据盘 | ⛔ 硬阻塞 | 数据目录与仓库在同一挂载点，`deploy_check` 真实报 FAIL；没有用 tmpfs/loopback 冒充独立数据盘 |
| 4 | 日志采集与脱敏 | ✅ 本机闭环 | JSON serve 日志与 nginx access/error 日志落到运行目录；`deploy_check` 日志扫描 PASS。尚未接外部日志聚合平台 |
| 5 | 密钥托管 | ✅ 闭环 | 环境文件 `0600` 且在仓库外；key 不在进程参数、run 快照和 97 个运行文件中。初次检查抓到快照泄露，已修复并轮换临时 key（见 §4） |
| 6 | 成本上限 | ✅ 闭环 | `max_cost_usd=5.0`，自检 PASS |
| 7 | webhook 接收端幂等 | ✅ 演练闭环 | 一个真实 mock run 向本地接收端投递 2 个 AgentCrew 事件；取真实 `X-AgentCrew-Delivery` 原样重投一次，接收端最终 `received=3 / processed=2 / duplicates=1`，重复事件未再次处理 |
| 8 | 容器挂卷 + healthcheck | ⛔ 硬阻塞 | 本机/该 VM 均无法运行 Docker（此前已取证 `unshare` / `cap_sys_admin` / `newuidmap` 限制），未提供 `--container`，自检明确 SKIP |
| 9 | 备份恢复演练 | ✅ 闭环 | 最新自检 **3 个 run / 6 个文件**备份到异目录、还原、sha256 一致 |
| 10 | Docker 下 `g1_exec_check` | ⛔ 硬阻塞 | 同 #8；CI 的 `Sandbox · docker 真隔离` 已覆盖后端隔离，但不冒充目标机上的 g1 验收 |

因此是 **7 项闭环、3 项硬阻塞**；剩余 3 项需要一块真实独立数据盘和一台允许运行
Docker 的目标机。

## 3. 最终自动自检

执行入口：

```bash
SSL_CERT_FILE=/home/user/agentcrew-runtime/tls/ca.crt \
python3 scripts/deploy_check.py \
  --data-dir /home/user/agentcrew-runtime/output \
  --base-url https://localhost:18443 \
  --api-key "$AGENTCREW_API_KEY" \
  --log-file /home/user/agentcrew-runtime/logs/api.log
```

结果：**6 PASS / 1 FAIL / 5 SKIP**，退出码 1 的唯一 FAIL 是独立数据盘：

- PASS：REST 认证、metrics、TLS、成本上限、日志脱敏、备份恢复；
- FAIL：独立数据盘；
- SKIP：容器挂卷/healthcheck，以及脚本设计上需要人工判的反代细节、告警规则、
  密钥托管、webhook 幂等。后 4 项已经由 §2 的专项实测补齐，不因为脚本 SKIP 而
  算作未做。

原始输出：`/home/user/agentcrew-runtime/logs/deploy-check-final.txt`。

## 4. 验收暴露并修复的两个真实缺陷

1. **TLS 自检端口写死 443**（`19a55c5`）：目标环境禁止绑定 443，改用 18443 后发现
   脚本仍连接 443。已改为从 `base-url` 解析显式端口，补默认 443 / 自定义端口 /
   IPv6 用例。
2. **REST API key 明文写入 `run.json`**（`af3336d`）：密钥托管专项扫描发现
   `config_snapshot.api_keys[0]` 是明文。`Settings.config_snapshot()` 现保留条目数但
   把每个值替换为 `***`，并新增落盘回归用例。云主机已轮换本次临时 key、清除旧
   副本，再用修复后的 run `20261113-202149-4fe3` 验证快照只剩 `['***']`。

两项修复合计相关测试 **40 passed**，ruff / mypy 通过；完整回归由 CI 执行。

## 5. 仓库内可复用资产

- `deploy/agentcrew.env.example`：密钥、数据目录、成本上限、JSON 日志环境文件模板；
- `deploy/nginx.agentcrew.conf.example`：与实测同源的 TLS + SSE 反代配置；
- `deploy/prometheus/agentcrew-rules.yml` 与 `.test.yml`：两条告警及触发测试；
- `deploy/agentcrew.service.example`：生产 systemd 单元模板（**未在本验收实例启用**）。

promtool 复跑：

```bash
cd deploy/prometheus
promtool check rules agentcrew-rules.yml
promtool test rules agentcrew-rules.test.yml
```

## 6. 从验收实例到公网生产仍需做的事

本实例没有公网域名、正式 CA、Alertmanager、外部日志平台，也没有启用 systemd；
这些不能写成已完成。生产切换时应使用正式域名证书、把告警交给 Alertmanager、
接入日志聚合、启用并验证 systemd 单元，再解决 §2 的独立数据盘与 Docker 主机。
webhook 测试接收端已停止；serve 与 nginx 留在云主机回环地址供复核。
