按实现写了接入文档：建索引、起服务、调用检索三条路径都有可直接执行的示例，并说明返回字段。

````markdown file=README.md
# kb · 内部知识库检索

给一个关键词，按相关度返回文档片段，并说明每条命中了几个关键词。
纯内存实现，无第三方依赖，Python 3.10 以上可跑。

## 快速开始

三步把一份语料变成可检索的服务：

```bash
# 1) 建索引
python -c "
from kb.index import InvertedIndex
idx = InvertedIndex()
idx.add('d1', '消息队列 选型 复盘')
idx.add('d2', '值班 手册 故障 处理')
print(idx.query(['消息队列']))
"

# 2) 起服务（宿主自己选，lambda 之外不引入框架）
python -c "
from kb.api import create_app
app = create_app()
print(app['routes']().keys())
"

# 3) 调用检索
python -c "
from kb.api import create_app
from kb.index import InvertedIndex
idx = InvertedIndex(); idx.add('d1', '消息队列 选型 复盘')
print(create_app(idx)['search']('消息队列'))
"
```

## 检索语义

关键词按空白切分，多个关键词之间是**与**语义：只返回全部命中的文档。
中文按双字切分，英文按小写整词；索引与查询共用同一个分词函数，
因此不会出现「索引里有、搜不到」的情况。

空查询返回空列表 —— 不是「返回全部」，避免调用方误以为检索到了内容。

## 返回字段

| 字段 | 含义 |
|---|---|
| `doc_id` | 文档标识，建索引时由调用方指定 |
| `hits` | **命中词数**，等于该文档命中的关键词个数 |
| `title` | 正文首行 |
| `snippet` | 正文前 120 个字符 |

排序看 `hits`：命中词数多的排前面；词数相同时按 `doc_id` 升序，
所以同一查询两次返回的顺序完全一致，输出可以直接 diff。

## 用例

```bash
python -m pytest tests/test_index.py -q
```

覆盖单关键词、多关键词交集、空查询、删除后一致性、重复索引与两层顺序一致性。
````

README 已按契约路径产出，三段示例均可直接执行，返回字段与 `_serialize` 逐项对应。
