# 检索

关键词与向量两路索引, 融合后交给列表查询与助理使用. 代码在 `src/amane/search/`.

## 索引端与查询端必须共用分词

`search.tokenizer` 同时服务索引写入与查询构造. 两侧切分不一致时结果**恒为空**, 且不报错.
改分词规则必须同时想清楚已经落库的索引 —— 旧 token 与新查询不再匹配, 需要重建.

`SearchField` (值即 FTS5 列名) 定义在 `amane.enums`, 与 `SearchMode` 同因: 它进了 API 契约,
只用枚举的调用方不该连带引入检索栈.

字段分两类, 差别在**查询侧**而非索引侧:

- `number` 是标识符: 索引存归一原子 (`ssis001`) 与分段词; 查询先试原子前缀, 无命中才退到分段.
  归一化无条件施加, 不做"看起来像不像番号"的判定 —— 那种判定在多段前缀 (`FC2-PPV-`) 上会失手.
- 其余字段是自然语言: CJK 段出 bigram, ASCII 段出整词. 查询用 **bigram 短语**表达,
  相邻性由 FTS5 保证, 因此短语查询等价于子串语义且走倒排索引.

单字查询无法用 bigram 表达, 固定回退到业务表子串扫描 (`store._like_fallback`).
不建单字列: 实测索引体积翻倍而只服务极少数查询.

FTS5 的 `tokenize` 是表级选项, **不支持逐列**, 逐字段策略只能靠应用侧预分词后写入各列.

## 存储布局

`metadata_fts` 与 `metadata_embeddings` 在业务库; `metadata_vec` (vec0) 在独立的 `search_vec.db`.

分开的原因是硬约束: `alembic check` 与 autogenerate 会反射业务库的全部表, 而迁移连接不加载原生扩展,
反射到 vec0 表时直接抛 `no such module: vec0`; `include_object` 在这之后才生效, 拦不住.
因此向量库按**派生缓存**处理, 删文件即重建, 不进迁移.

`db/migrations/env.py::_RUNTIME_TABLES` 排除业务库里的检索表, 否则 autogenerate 会提议删除它们.

## 开关与模式

`config.SearchConfig` 的三个开关正交, 全关时 `SearchService.mode` 为 `LIKE`:
调用方据此走原有子串查询, 不要拿空结果当"没有匹配".

`hybrid_enabled` 记的是**用户意图**, 不会被"当前没有向量路"改写成 `False`. 是否真融合看
`SearchConfig.fused` (`hybrid_enabled and keyword_enabled and vector_enabled`). 早先的 `after`
校验器会在 `vector_enabled=False` 时把开关改成 `False` 并落盘, 于是用户后来打开向量也拿不到融合 ——
这类"帮用户做决定"的归一化会永久吃掉配置, 不要再加回来.

`HYBRID` 表示两路都跑了, 不区分"融合"与"拼接": 融合关闭时关键词结果在前、语义结果补齐, `mode` 仍是
`HYBRID`. 需要区分请读配置, 不要从 `mode` 推断.

`SearchResult.total` 的语义随模式变化: `KEYWORD` 是索引命中数, `VECTOR` / `HYBRID` 是候选窗口内的
命中数, 不是全库精确计数. 对外响应必须带 `mode`, 否则调用方无法解释翻页总数.

向量路任何异常 (模型未装、端点不通、扩展缺失) 都降级为关键词并置 `degraded`, 不让搜索整体失败.
`vector_enabled` 为真但 `provider` 缺席同样算降级 —— 配了没接上不能表现成"关键词就是全部结果".

`SearchMode` 定义在 `amane.enums`, 不在 `search` 包内: 它是响应契约的一部分, 只用枚举的调用方
(路由与响应模型) 不该连带引入检索栈.

调用方还能按次收窄: `SearchService.search(mode=SearchType, fields=[SearchField, ...])`.
`SearchService._resolve` 取**请求与可用能力的交集** —— 请求只收窄不扩张, 配置关掉的路不因请求
而打开, 否则"关掉语义检索"这个开关能被任意调用方绕过. 请求的路走不通时退回配置模式并置
`degraded`, 让调用方分得清"这就是关键字结果"与"想要语义但没走成".

## 字段范围

`fields` 收窄**关键词路**: 未选中的列既不进 FTS5 列过滤 (`tokenizer.column_scope`), 也不进单字
的子串回退 (`tokenizer.like_columns`); 排除 `number` 时精确番号层一并跳过
(`SearchService._exact_ids`). 什么都不剩时返回空计划 —— 那是 0 命中, 不是"退回全字段扫描".

未传 `fields` (全选) 时, 子串回退仍只扫 `title / plot / actors / tags` 这四个自由文本列
(`tokenizer.DEFAULT_LIKE_COLUMNS`), 与接入字段范围之前的口径一致; 一旦显式给了范围, 回退就严格
按所选扫到底 (含 `series / studio / publisher`), 否则"只勾厂牌 + 单字"会静默 0 命中.

**向量路不受 `fields` 约束.** 一条影片只有一个向量, 它是 `indexer.entry_text` (全部字段拼接) 的
嵌入, 入库时就定死了; 查询侧没有按字段切分的余地. 要逐字段语义只能每个字段各存一个向量 (索引
体积翻若干倍), 不做.

无索引时的兜底 (`mode=LIKE`, 业务库子串扫描) 同样不读 `fields`: 那条路径本来就只扫 number + title.

## 可选依赖

内嵌嵌入 (fastembed) 是 extra `vector`, 不在主依赖里; `sqlite-vec` 在, 因为向量表默认启用.
缺 fastembed 时分发仍须可用: 模块导入不得失败, `build_embedding_provider` 返回 `None` 并打一条
带 hint 的 warning, 搜索按 `degraded` 走关键词. 直接构造 `FastEmbedProvider` 再嵌入则抛
`EmbeddingError` 并在消息里指明装哪个 extra.

Docker 镜像 (`Dockerfile`) 只装主依赖, 因此**不含**内嵌实现: 镜像内要用语义检索需自建镜像加
`--extra vector`, 或把 `search.embedding.provider` 指向 HTTP 端点.

## 融合

候选集 = 向量召回 ∪ 关键词命中. 余弦相似度只在**本次查询的向量候选内**做 z 分数, `fusion.zgate`
以下且没有词命中的候选被丢弃 —— 不达标就是没搜到, 不硬塞低分.
关键词独有项没有语义分数, 靠 `fusion.word_weight` 进入排名, 否则番号、人名会被语义路整个丢掉.
候选全同分时 z 分数全为 0, 此时不做门控.

**精确番号命中自成一层, 排在所有融合分数之前.** z 分数是样本内标准化, 上界为 `sqrt(n-1)`,
随候选数增长; `word_weight` 是常数. 两者相加意味着任何固定权重在语料足够大时都会失守 ——
实测 300 条语料搜 `SSIS-001` 时, 排第一的已是同厂牌的语义近邻. 番号是库的主键级标识,
用户输入它就是"要那一条", 不能参与数值比大小.

这一层由 `store.exact_number_ids` (一次归一原子前缀探针) 供数, **由 `SearchService` 统一加在最前**,
因此与 `keyword_enabled` 无关: 关掉关键词只留向量时, 词索引仍照常维护, 探针照样生效,
纯向量模式搜 `SSIS-001` 仍然先给真番号而不是 `SSIS-002`. 放在服务层而不是 `search_keyword` 里,
是因为 FTS5 不回报命中列、而纯向量路同样需要这条信息.

小语料注意: 5 条语料时 z 上界只有 `sqrt(4)=2.0`, 恰好等于默认 `zgate`, 语义路会被整体门掉.
这是样本内标准化的数学边界, 不是缺陷; 调参或做验收时不要用个位数条目的语料下结论.

## 原生扩展的边界

`load_extension` 只在向量库那条连接上开启, 加载窗口是一条语句, 随后立即关闭且不外传连接;
该连接只执行 `store` 内硬编码的 SQL 与参数绑定.

不要试图用 `set_authorizer` 兜底: 实测**全 DENY 仍能 `load_extension`**, 该操作不触发任何授权动作码.
Python 也不导出 `SQLITE_LOAD_EXTENSION`. 削弱防线的写法在 `store` 模块说明里有记录.

## 接入写入路径

`SearchStore.upsert` / `remove` 由 `MetadataIndexer` 驱动, 组合根 (`app.bootstrap`) 把它注入
`Repository.index_sink`, 类型契约在 `db/indexing.py::MetadataIndexSink`. **`db` 不认识 `search`**:
`db` 只依赖那个 Protocol, 由组合根决定谁来听. 关闭时先摘 sink 再关检索栈, 不留没人听的窗口.

`MetadataRepoMixin` 只在**提交之后**通知: `upsert_metadata` 两条分支、`update_metadata`、
`delete_metadata`、`batch_delete_metadata`. 通知是同步入队, 不 await、不抛 —— 写入路径不允许
因索引失败而失败. 索引是派生数据, 落后就落后, `backfill` 修得回来.

写入与删除走**同一条队列** (`IndexJob(metadata_id, drop)`). 拆成"队列 + 待删集合"会让删除永远
等不到被处理: 消费者阻塞在 `queue.get()`, 而删除只往集合里塞 —— `drain()` 直接挂死 (实测).

搜索侧为"读己之写"做**有界等待**: 路由先 `indexer.wait_idle(1.5s)` 再查. 积压太深就按现有索引
返回, 不能让一次搜索陪着批量刮削跑完嵌入. 因此**刚写的内容可能搜不到**, 这是刻意取舍.
等待的判据是"已落盘"而不是"队列为空": 任务被取出后到 `task_done` 之间队列同样是空的, 按空判
空闲会让等待白等.

`backfill` 的顺序: 模型/维度变了先 `drop_vectors` 整表重建 → 补缺词索引 → 删孤儿
(`known - all_ids`) → 补缺向量. 两步写 (`metadata_embeddings` 再 vec0) 不在同一事务,
vec0 缺条目由重建修复.

开关首次开启时的回填由启动阶段触发 (`indexer.start_backfill()`), 不阻塞就绪.

## 对外契约

`GET /api/metadata?search=`, 另有 `search_type` (keyword / vector / hybrid) 与 `search_fields`
(可重复, 省略为全选) 两个可选参数; 非法枚举值走 422. 走检索时响应带两个字段:

- `search_mode`: 本次实际走的路径 (`like` / `keyword` / `vector` / `hybrid`). 前端与调用方靠它
  解释 `total` 语义, 不要自己猜.
- `search_degraded`: 要求走向量但走不了. 此时结果是关键词的, 不是语义的 —— 必须能区分.

翻页深度受 `SearchService.recall_window` 约束: 向量路开启时是 `fusion.vector_k` (默认 200),
只开关键词时是 `KEYWORD_WINDOW=1000`. 路由**一次取满整个窗口**再在本地分页, 因此窗口内
任何一页都能翻到; 窗口之外没有可信顺序, 不再返回候选.

`total` 是**窗口内过其它筛选项求交后**的候选数, 上限就是召回窗口, 不是全库精确计数.
它按去重后的候选计, 因此也包含"只在精确番号层里、没进向量窗口"的那一条 —— 与 `search_mode`
配合读: `keyword` 时是索引命中数, `vector` / `hybrid` 时是召回候选数.

> 这条 `total` 必须大于"本页条数", 否则调用方无从判断还有下一页. 早先路由直接拿
> `len(items)` 当 `total`, 而候选只取到 `offset + limit`: 第一页 (`limit=30`) 就对外报
> `total=30`, 前端 `loaded < total` 立刻不成立, 搜索看起来永远只有一页.

## 实测基线 (40 条语料, 本机)

跑法 `PYTHONPATH=. uv run python temp/verify_live_http.py`, 全程走 `/api/config` 与
`/api/metadata`, 不直接调 service. 三阶段结果:

| 阶段 | 操作 | 结果 |
|---|---|---|
| A 默认 | 直接搜 | `keyword`, 番号三种写法同解, 简介中文可搜, 单字走 LIKE 回退, 13–54 ms |
| B 开启向量 | `PATCH /api/config {search:{vector_enabled:true}}` | PATCH 返回 **39 ms** (不等下载); 预热在**未发任何搜索**的情况下 2.0 s 内自行就绪; 回填后 `hybrid`, 纯语义查询可召回 |
| C 只留向量 | 关 `keyword_enabled` | `vector`, 首查 1.8 s (当时每次重建都重新预热, 见下"provider 跨重建复用"), 之后 19 ms; 番号**排第一** |

### 模型预热放在"配置启用"这一刻

`build_search` 之后立刻 `SearchStack.start_warmup()`, 由 provider 自己实现

```
WarmableProvider: ready / warmup() / ensure_ready(timeout)
```

- **为什么不在首个请求里**: 冷启动要下约 150MB 权重, 实测 **53 s**. 挂在请求上就是一个必然超时的查询,
  而且用户点完开关后完全不知道正在发生什么.
- **为什么不阻塞 PATCH**: 下载不能挡住配置保存. PATCH 只负责重建栈并**发起**预热,
  实测返回 39 ms.
- **未就绪时的查询**: `SearchService` 先 `ensure_ready(READY_WAIT_S=2.0)`; 超时即按关键词返回并置
  `degraded`, 不让请求陪着下载. 加载任务本身不被超时打断, 下个请求即命中.
- **加载只做一次**: 预热、回填、并发查询共享同一个加载任务 (`_loader()`), 不会各下一遍模型.
- **失败可自愈**: 预热失败只记日志 (配置启用这个动作不该因为下载失败而报错), 下一次 `embed`
  会重新加载.
- **HTTP provider**: 没有"下载"这一步, 预热只发一次最小请求, 把"地址填错 / 模型名不存在"提前暴露到
  启用时的日志; 失败不阻断, 首次成功请求即置 `ready`.

**provider 跨重建复用**: 改检索配置仍会重建栈 (service / indexer 绑定配置), 但当 `embedding` 子配置
逐字段未变且向量路仍启用时, 旧 provider 与正在跑的预热任务被移交给新栈 (`ReusedEmbedding`), 不再
重新加载模型. 因此只有"改嵌入本身"(换 provider / 模型 / 维度 / 线程 / 超时)才付加载代价;
改开关、改融合权重是毫秒级. 移交后旧栈 `aclose()` 不再关这个 provider, 所有权随移交转移.

`wait_idle` **不反映回填进度**: 它只看写入队列, 回填是独立任务. 判断"可用了"要读
`store.stats().vec_index_docs`, 不要拿 `wait_idle` 的返回值当回填完成信号 (实测踩过).

