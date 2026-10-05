"""``--demo`` 的演示目标定义（spec §1.2 S2 / §16.2）。

``PROJECT_GOAL`` 是 CLI ``run --demo`` 使用的完整项目目标；``EXPECTED_TASK_IDS``
是 mock 回放应当产出的任务 id 全集（PM 拆解的 8 个任务 + 平台追加的收尾任务
``T-FINAL``），供测试断言演示流程没有漏跑或多跑任务。
"""

PROJECT_GOAL = (
    "开发一个 Todo List Web 应用：用户可在浏览器中新增、查看、编辑、删除待办事项，"
    "数据通过 REST API 持久化在服务端，前端为单页应用，附完整文档、测试与 Docker 部署配置。"
)

EXPECTED_TASK_IDS: tuple[str, ...] = (
    "T-001",
    "T-002",
    "T-003",
    "T-004",
    "T-005",
    "T-006",
    "T-007",
    "T-008",
    "T-FINAL",
)
