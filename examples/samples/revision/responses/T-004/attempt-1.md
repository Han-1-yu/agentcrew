按实现写完了 README：安装、用法、输出示例与 skipped 的含义，命令均可直接执行。

````markdown file=README.md
# logstat

把一段访问日志压成几行统计的命令行工具。用于值班时快速判断 5xx 是否突增、
慢请求集中在哪个时间段。

## 安装

无需第三方依赖，Python 3.10 以上即可：

```
git clone <repo> && cd logstat
python -m logstat --help
```

## 用法

```
python -m logstat analyze access.log
python -m logstat analyze access.log --window 5
python -m logstat analyze access.log --json
```

输入要求每行一条，前三个字段依次为 ISO 时间戳、状态码、耗时毫秒：

```
2026-10-01T10:00:01 200 12
2026-10-01T10:00:02 502 1830
```

输出示例：

```
total    2
skipped  0
status   200=1  502=1
p50      12 ms
p95      1830 ms

window   10:00  count=2  errors=1
```

## 输出字段

| 字段 | 含义 |
|---|---|
| `total` | 成功解析的行数 |
| `skipped` | 无法解析而被跳过的行数 |
| `status` | 状态码到条数，按状态码升序 |
| `p50` / `p95` | 耗时毫秒的 50 / 95 分位（最近秩） |
| `window` | 按窗口聚合的请求数与 5xx 数 |

## skipped 是什么

日志是流式追加的，最后一行常常只写了一半；启动横幅、健康检查的裸文本也会
混进同一个文件。这些行解析不出三个字段，会被计进 `skipped` 而不是让命令
报错退出 —— 值班时少算一行远好过一条命令直接失败。

`skipped` 大于 0 时命令会在表格下方追加一行提示。若这个数字明显偏大，
先检查日志格式是否与上面示例一致。
````

README 已按契约路径产出，示例输出与 `analyze` 返回的键一一对应。
