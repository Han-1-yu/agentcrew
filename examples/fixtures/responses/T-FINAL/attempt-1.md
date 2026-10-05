汇总本次全部任务的交付成果，输出 DELIVERY.md：完成清单、验收结论、遗留问题与运行方式。

````markdown file=DELIVERY.md
# 交付总结：Todo List Web 应用

## 完成清单

| 任务 | 角色 | 交付物 |
|---|---|---|
| T-001 产品需求与验收标准 | 产品经理 | `docs/PRD.md`、`docs/acceptance.md` |
| T-002 架构设计与接口契约 | 架构师 | `docs/architecture.md`、`docs/api-spec.yaml` |
| T-003 后端 API 实现 | 后端开发 | `backend/app/` 下 6 个模块 + `backend/requirements.txt` |
| T-004 前端单页实现 | 前端开发 | `frontend/index.html`、`frontend/app.js`、`frontend/styles.css` |
| T-005 API 测试与报告 | 测试工程师 | `tests/test_api.py`、`docs/test-report.md` |
| T-006 容器化与部署配置 | 运维工程师 | `Dockerfile`、`docker-compose.yml`、`docs/deploy.md` |
| T-007 README 与使用文档 | 文档工程师 | `README.md`、`docs/usage.md` |
| T-008 前端静态检查用例 | 测试工程师 | `tests/test_ui.py` |

共 22 个计划产物全部落盘，另有本文件与 `MANIFEST.json` 作为收尾记录。

## 验收结论

- 全部 9 个任务（含本收尾任务）通过 G1 静态校验与 G2 评审，无失败、无阻塞。
- T-005 首次交付因用例正文未展开被 G1 驳回，返工一次后通过——完整的门禁闭环已在本次运行中演示。
- 后端可独立启动并通过接口用例；前端页面可完成新增、勾选、编辑与删除的完整操作。

## 遗留问题

- 数据保存在服务端进程内，重启服务后清空；持久化留待后续迭代。
- 前端交互仅做静态结构检查，未引入浏览器自动化测试。
- 未做并发压力与安全加固（鉴权、限流均不在本期范围）。

## 运行方式

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload
```

浏览器打开 http://127.0.0.1:8000 使用页面，http://127.0.0.1:8000/docs 查看接口文档；
容器方式见 `docs/deploy.md`，接口用法见 `docs/usage.md`。
````

DELIVERY.md 已按收尾契约产出：完成清单逐项列出角色与产物，验收结论写明门禁结果与返工记录，遗留问题与运行方式均可直接核对。
