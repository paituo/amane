# 助理 Agent

> 入口: `src/amane/agent/`. 本文只记录边界与契约; 字段与签名去源码.
> 上游限流 / 超时时的实际行为见 [llm-resilience.md](llm-resilience.md); 配置见 [config.md](config.md), 列表接入见 [api.md](api.md), 翻译 LLM 见 [llm.md](llm.md), 前端 IA 见 [frontend.md](frontend.md).

## 定位

**助理 Agent** 是内部对会话式读写助手的称呼; 产品面只称 **Amane** (首页对话), 不向用户暴露 Agent / SQL 等实现词.

能力: 只读 SQL 探查库; 交付结果写入可引用的 **Saved Query** 再被 Browse / 下载消费; 写变更只经由封装领域工具 (Repository / Handler 语义), **禁止**裸 DML / DDL. 与 `llm/` 翻译管线并行, **不共用** Hot section.

## 工具

只读工具始终可用:

| 工具 | 契约 |
|------|------|
| `search_media` | 按语义/剧情/关键词检索, 回**按相关度排序**的 metadata id + 命中卡片 (`number` / `title` / `plot` 摘要 / `directors` / `actors` / `tags` / `series` / `studio` / `publisher` / `release` / `runtime` / `content_type` / `score` / `matched_queries`) + `mode` + `degraded` + 翻页三件套 (`total` / `has_more` / `next_offset`). 可选 `queries` 多串, `fields` 限定检索列, `limit` 页大小, `threshold` 相对分下限; `name` 不为空时顺带交付 metadata 预设 (行序即相关度顺序) |
| `sql_explore` | 中间推理; 默认只回样例. `create_view=true` 物化会话内 SavedQuery 供 `inspect_result` 翻页 — 视图是**纯行数组**, 无 entity / `id` 列要求, **不**纳入交付结果 |
| `sql_deliver` | 面向用户的结果 → `saved_query` + 内存结果缓存. `entity=metadata\|actor` 交付须含主键列 `id`, 可作片库 / 演员筛选并深链; 省略 entity (或 `data`) 交付任意只读结果, 只进入数据页 |
| `inspect_result` | 按 `saved_query_id` 取行 (交付或探查视图) |

**语义查询一律先走 `search_media`**, 不要用 SQL `LIKE` 模拟: 库内标题与剧情存的是**译文** (`plot` 已是中文, 见 [search.md](search.md)), 所以 `LIKE` 并非恒不命中, 但它只认字面 —— 同义改写会漏, 前导 `%` 走不了索引, 全表扫在万级库上必然撞超时, 且覆盖不到演员 / 标签 / 系列. 空结果只说明"没有这个字面片段", 不能据此断言库里没有. 检索栈经 `AgentRuntimeBridge.search` 取用 (返回当前实例, 不是构造时的那个 —— 检索随 `hot.search` 热重建), 未装配 / 开关全关时工具回 `error` 点明原因而不是空命中. `degraded` 为真表示语义路本该参与但没走成, 结果是关键词的 —— 模型须如实说明, 不能当作语义匹配. 交付保序由 `tools.ranked_metadata_sql` 把顺序写进 SQL 实现: Saved Query 的权威是 SQL, 只留在内存缓存里的话列表页重跑就丢了相关度顺序.

语言: 库内标题与剧情是**译文** (本库中文), 既不是原始日文/英文措辞, 也不保证复用用户的说法. 不要把用户的问句先"翻译"成日文/英文关键词再检索, 直接把用户原话交给 `search_media`, 语义路负责抹平措辞差; 命中后按库内原文引号番号与标题, 用户要求时才译.

**命中卡片必须带剧情摘要**: 只回番号+标题时, 模型没有任何依据判断"这条是不是用户说的那部" —— 标题是译者剪出来的短句, 剧情转折 (比如"被带回家辅导功课考上大学") 根本不在标题里. 实测 (session 8, 2026-10-26) 模型拿到的 10 条全是成人片标题, 它据此认定"搜错了", 然后在 170 秒里从训练数据枚举华语片名逐个否定, 最后 18450 字符思考只换来 183 字符回答. 所以 `plot` 按 `PLOT_PREVIEW_CHARS` (240 字) 压平空白后截断, 演员/标签各截前 6/8 项 —— 够判断相关性, 又不至于 20 条撑爆上下文.

**翻页共享同一个排序**: 融合分数是候选集内的 z 标准化, 候选集一变排序就变, 所以不能"按 limit 取"然后"按 offset 再取". `search_media` 每次都以固定深度 `SEARCH_DEPTH` (50) 检索, 再用 `offset` 在**同一个**累积 `ranked` 上切片, 页间不重叠也不漏项. `total` 是这个排序深度 (= `len(ranked)`), **不是**"库里共有多少条相关"; 检索栈只保证召回窗口内的顺序 (见 [search.md](search.md)), 更深的排名没有意义, 所以 `total` 有上限. 模型必须读完 `has_more` 的每一页才能说"库里没有", 且必须说明实际看了多少条.

**长句要拆, 拆哪几个串由模型决定**: 一句剧情描述同时含多个条件 ("离家出走" + "辅导功课" + "考上大学"), 而关键词路是**词项 AND** (`tokenizer.plan_keyword`), 所以整句几乎必然 0 命中, 只剩语义路决定排序. 实测 (2026-10-26, 真实 FTS 索引):

| 查询 | 命中 |
|------|------|
| 整句 `在当前的片库中搜素…带回家辅导她功课使她考上大学` | **0** |
| `离家出走` | 50 |
| `家庭教师 辅导` | 1 |
| `考上大学` | 5 (第一条正是 `SCOP-193 / id=5558`) |
| `带回家 辅导` | 0 |

工具**不做分词、不替模型拆句**: 真正决定"拆成哪几个串"的是模型. 工具只拒收**已知必然 0 命中**的形状 (`QUERY_WORDS_MAX`=3: 一个串超过 3 个空白分词, 在词项 AND 下要求全部同时出现), 拒绝时给出正确拆法示范. 起因见 2026-10-26 实测: 模型前 3 次调用都把整句用空格拼成一个串, 全 0 命中, 白烧两轮才自己改对; 而纯提示词约束挡不住这种写法.

工具接的是模型做不稳的四件事:

- **融合**: `_rrf` (加权倒数排名, k=60, 权重 `1/sqrt(命中数)`) 把多个串的顺序合成一个. 不用硬交集, 因为串之间常不同义 ("辅导功课" vs "家庭教师"), 交集会把只在一路出现的正确条目删掉; 不加权则宽泛串 (50 条) 的名次分会把精确串的第 1 名挤出页外.
- **跨调用累积**: 模型一次只发一个 tool_call, 所以"混合查询"在运行时表现为多轮调用, 每轮带一个分割串. `AgentDeps.search_rankings` 累积本回合每个串的有序命中, 每次回执都回**累积后的融合排序** —— 否则第 2 轮只看到第 2 个串的结果, 等于把上一轮的证据丢了.
- **翻页不重检**: `offset>0` 只是"在已有累积排序上切片", 不再跑检索. 旧实现对 `offset>0` 豁免判重, 同一个串被再检索一次又进累积 —— 重复投票把排名整体扰动 (实测翻页第 21 条竟是原第 1 页的末位), 而检索本身幂等, 重跑纯属浪费.
- **判重与预算**: 见下.

**可调三件事** (`search_media` 的参数, 都不改变工具契约的形状):

| 参数 | 作用 | 落点 |
|------|------|------|
| `fields` | 把**关键词路**收窄到指定列 (`number/title/plot/actors/tags/series/studio/publisher`), 未知字段直接拒绝并列出合法值 | `SearchService.search(fields=...)` → `plan_keyword` 只在这些列上 MATCH |
| `limit` / `offset` | 页大小 (1..50, 默认 20) 与起点; 检索深度恒定 `SEARCH_DEPTH`=50, 切片在深度内做 | 工具层切片 |
| `threshold` | 相对相关度下限 (0..1), 只保留**该串自己的相对分** ≥ 阈值的候选, 砍掉的条数由 `filtered_out` 报出 | `SearchResult.scores` (见下) |

`queries` 同时接受 `list[str]` 与单个 `str`: 兼容端点常把数组参数写成字符串 (实测出现过), 若只声明 `list[str]` 会被 schema 校验拒掉, 白烧一次往返.

**分数是相对量, 不是概率**: `SearchResult.scores` 与 `ids` 等长, 首名恒 1.0, 其余是它相对首名的比例. 绝对分跨路径不可比 —— 关键词路是 bm25 负值, 向量路是余弦, 融合路是 z 分加常数. 归一化在 `search.service.relative_scores` 一处完成, 阈值因此有一个不随 mode 漂移的语义 (0.5 = 首名的半分). 精确番号命中被 `_exact_first` 强制前置时给 ceiling 分, 避免"排第一但相对分 0.2"的自相矛盾.

**检索有预算**: `SEARCH_CALL_BUDGET`=6 次/回合, 同一串 (按 `_query_key` 归一化, 忽略空白标点大小写) 重复检索**不消耗**预算, 也不报错 —— 直接把累积排序的当前页还回去, 附一句 `note`. 起因是一次真实失败: 模型把同一句换成 23 个近似串, 检索 23 次、耗时 13 分钟, 结果集几乎没变, 最后仍未收敛. 报错会让它空转一轮, 累积页能直接支撑作答. 每串回执里的 `queries[].hint` 给出可行动的下一步 (0 命中 → 换更具体的剧情词 / 多词串 → 拆成单词; 全被阈值砍 → 降阈值), 而不是只给一个 0. 预算用完时工具回"停止检索, 用已有命作答, 要再筛就用 SQL 在 `id IN (...)` 上过滤".

写操作工具按域分组为 `Capability`, **不做延迟载入**: 每个请求都声明全部工具. 兼容端点 (DeepSeek 等) 没有「声明了但不开放」的通道, 逐次载入只能通过修改 `tools` 实现, 而工具定义渲染在 system 之后、对话之前, 每载入一次即重读整段对话前缀; 全量声明的固定前缀恒定不变, 首轮即命中缓存. 不经由 HTTP 自调用. `AgentDeps.bridge` 提供库路径边界 / watcher / 取消运行中任务.

| `id` | 覆盖 |
|------|------|
| `metadata-ops` | 修改字段、合并、user tag、刮削入队、删除 |
| `actor-ops` | 演员人物字段、别名行 (查询 / 解析 / 增删)、展示名切换、演员刮削入队 |
| `facet-identity` | rename / merge / delete / 规则列表与删除 |
| `library-ops` | 库 CRUD、REFRESH 入队 |
| `feed-ops` | 源 CRUD、立即拉取、FeedItem 历史批量操作 |
| `schedule-ops` | CLEANUP / UPSCALE / R18_IMPORT / RESCRAPE 定时 CRUD 与触发 |
| `task-ops` | 统一提交 / 取消 / 重试 (入队, 不代为运行) |

任务与定时任务的入参 (即 `submit_task` / `create_schedule` 的 `submission`) **不随工具签名内联**: 联合体大小远大于其余工具定义, 改为 `get_task_submission_schema` / `get_routine_submission_schema` 按需返回, 与提交时的校验共用同一个 `TypeAdapter`; 校验失败返回出错字段 (最多三条)、可用类型与该类型的字段定义, 模型无需再次试探形状.

指令只保留域特有约束. 全局约定 (id 一律取自 `sql_explore` / `sql_deliver`、破坏性操作批准、失败返回 `{error}`、入参先取 schema) 集中在系统提示, 不逐工具重复; capability id 不再对模型可见, 指令中不得引用.

### 返回值

工具返回值是模型上下文的一部分, 每个 token 都要付钱, 因此只回**下一步需要的观测**:

- 成功且无后续依赖 → `tools.py::TOOL_OK` (纯回执); 创建 → 新对象 id (键名用入参口径, 如 `feed_id`); 批量 → 计数 (`affected` / `missing` / `submitted` / `skipped`).
- 不回入参与工具名回显、时间戳、耗时 (`elapsed_ms`)、以及能由返回值本身算出的字段 — `total` 只在分页列表里回, 因为它是翻页的依据.
- 列表工具只回识别与筛选所需字段 (id / 名称 / 归属 / 状态 / 计数), 细节留给 `get_*` 或 `sql_explore`; `get_*` 回该行当前状态, 是详情视图.
- 失败只回 `{"error": ...}` (多字段出错另附 `errors`, 最多三条), 文案须自足且可行动: 点名出错输入, 并给出下一步. 创建后立即触发的外呼失败属部分成功: 仍回新 id, 原因单列 `poll_error`, 不并入 `error`.
- 大块结果保持「列名 + 行数组」结构, 列名只回一次, 不按行重复键名.

执行前只修改实际调用与落入 `messages.json` 的 `tool_name`: `__` 最后一段恰好是当前可调用名时裁成该段; 名字已在可调用集合里或后缀对不上则原样交给框架 (未知工具仍 `ModelRetry`). 工具卡片仍可能显示模型原始名.

`actor-ops` 的别名工具对应别名模型 (见 [data-model.md](data-model.md) 演员身份): 别名是一对多行, `resolve_actor_name` 多命中即歧义, 应交由用户决定; `set_actor_display_name` 与 `facet-identity.rename_facet(kind=actor)` 等价, 二者任一即可, 不允许重复调用. `PATCH /config` **不**暴露为工具.

`feed-ops` 只通过 `FeedService.poll_one` 触发远程拉取; 新条目是否入队 SCRAPE 由 Feed 的 `auto_enqueue` 决定, Agent 不在工具内解析 RSS 或直接运行刮削. 删除源或删除条目历史须批准. 条目 `scrape` 沿用该 Feed 当前配置, 语义见 [feeds.md](feeds.md).

`schedule-ops` 创建时只接受 `RoutineSubmission`; 更新只允许 `name` / `cron` / `enabled`, 任务类型或 payload 变化须删除后重建. `trigger_schedule` 只把 `next_run` 设为当前时间, 实际 Task 由 `CronScheduler` 下一次 tick 创建, 不是同步执行.

**批准流**: SQL 非法或 SQLite 运行时错误 → 工具返回 `error` 字符串, **不**升格为回合错误打断整轮. 慢查询 (`allow_slow`) 与破坏性写 (metadata / facet merge·delete·删规则 / 删库) 在工具体内 `raise ApprovalRequired`; 适配器把 `DeferredToolRequests` 映射成 AG-UI 中断 (`RUN_FINISHED.outcome.interrupts`), 中断 `id` 为 `int-{tool_call_id}`, 文案取自工具给的 metadata. 一次 `resume[]` 必须回答**全部**打开的中断, 故前端逐项暂存决定、集齐后整批提交, 批量批准即全部置 approve. 批准 → 工具体再次进入且 `tool_call_approved=True`; 拒绝 → `ToolDenied`. 模型只看见普通 tool return, **无**「用户已批准…」类附加说明; 刷新后用回放行末尾的中断还原待批状态. 

**批准必须真的放宽预算**: 默认 `agent.sql_timeout_ms` (5 s) 与批准后的 `tools.APPROVED_SQL_TIMEOUT_MS` (30 s) 是两档, 由 `AgentDeps.timeout_for(allow_slow=...)` 选. 早先批准后仍按默认值执行, 慢查询在同一个上限处照样被打断 —— 那样批准就只是白点一次, 不是"我愿意等". 预算只对 `allow_slow` 生效: 普通探查不该拿到 30 s, 否则一次误写的全表扫描会白占线程半分钟.

**子串扫描的例外**: 检索栈可用时, 含 `LIKE '%…%'` 的慢查询与超时**不进批准流**, 直接回 `error` 并指向 `search_media` (`tools.sql_failure_receipt`). 批准它只会换来一次用户交互加一次必然超时、且只认字面的全表扫描; 检索缺席时全表扫描是唯一手段, 此时保持原路径. 该判断只用于文案与这个分支, 不作安全校验 —— 沙箱的只读边界仍由连接态与 authorizer 构成.

**SQL 失败回执要能一轮改对**: `no such column` / `no such table` 是模型自己写错了标识符, 正确答案是确定的, 因此 `tools.identifier_hint` 就地补一句真实落点 —— 概念冲突按 `_CONCEPT_COLUMNS` 表 (`description`/`synopsis`/`overview` → `metadata.plot`), 其余按 `schema_docs.tables_with_column` 指出列在哪个表, 完全不存在的列则让它回到 schema 摘要. 三段 (原始原因 / 纠错 / 换工具指引) 合成一条 `error`, 因为模型一轮只读一次回执, 拆字段会被只看到第一条. 背景: 线上日志里模型把剧情列写成 `description`, 沙箱只回 `no such column: description`, 模型无从知道该写 `plot`. 同一份日志里还有一个**不报错的错**: `content_type LIKE '%movie%'` —— 枚举列存的是成员名 (`CENSORED`), 而 `content_type` 根本没有 `movie` 这个取值, SQLite 又对 ASCII 大小写不敏感地 `LIKE`, 于是条件恒不命中却一声不响. 故 `schema_docs` 把片库相关表 (`metadata` / `media_files` / `actors` / `saved_queries`) 的枚举取值一并注入摘要, 并点明是**大写成员名**.

## 会话数据

会话是**用户数据**, 写入 Cold `data_dir`, **不**纳入 `log_dir`: `{data_dir}/agent/sessions/{session_id}/`

| 文件 | 角色 |
|------|------|
| `messages.json` | **权威** LLM `message_history`; 供 prompt cache 前缀一致 |
| `events.jsonl` | 回放行 (单调 `seq`), 契约见 `src/amane/agent/rows.py`; 页面契约是其中的 `UiRow`, AG-UI 事件仅透传 |
| `meta.json` | 附属文件 (`turn_running`、会话 `thinking` 覆盖等) |

`agent_sessions` 表只做索引. 删会话清理目录与未 persist 的 Saved Query. 标题由 `POST /agent/sessions/{id}/title` 按首条用户输入生成, 与回合并行执行 (前端在发出首条消息时另行请求, 不等回合结束); 未配置模型、请求失败或超时回退首条输入截断, 标题不进模型上下文. 进行中的回合任务登记在 `AgentService`, 取消与删会话据此终止; `messages.json` 是唯一的模型上下文来源, `ResultCache` 独立 TTL. 中断回合的历史同样在回合收尾时落盘: 重启后的续批据此重建 `DeferredToolResults`, 中断 id 由 tool_call_id 派生, 续批不依赖进程内状态.

## 对话通道

| 通道 | 用途 |
|------|------|
| REST | 会话 CRUD、标题生成、`trace` |
| **AG-UI** | `POST /agent/sessions/{id}/agui` 启动后台回合并订阅; `GET .../agui/events` 只跟随回放行; `POST .../agui/cancel` 终止 |
| `/ws` | 任务日志等广播 — **不**承载对话 |

回合在服务端运行完毕: **客户端断连不取消**, 只有显式 `cancel` 才终止任务并落 `cancelled` 行. 事件先落盘再分发给订阅者; 每条 AG-UI 事件同时展开为回放行, 页面据此重建气泡 (协议本身没有历史回放), 模型上下文只认 `messages.json`. 行是**已成形的展示事实** (协议里的参数增量、结果配对、未决中断都在后端定形), 页面只按到达顺序折叠; 联合经 OpenAPI 生成前端类型, 改行模型即在前端编译期暴露. 页面重挂载 (刷新 / 切页) 后本地流已断, 由 `GET .../agui/events?after_seq=` 续上: 它只跟随回放行 (**不**启动回合, 故进行中的回合也不 409), 发的是回放行本身而非 POST 通道的 AG-UI 投影, 流关闭即回合已结束; 页面发起的回合同样跟随它 (展示只认回放行), 此时订阅可能赶在服务端登记回合之前打开而空转即返, 由客户端在回合未收尾时重开接上.

审批的待批态由 `approvals` 行给出: 三条可控终止路径 (正常结束 / 取消 / 失败) 各发一条快照, **空列表即已无未决** (后条覆盖前条), 页面把它写进消息 metadata 供 runtime 恢复审批入口. `RUN_FINISHED.usage` 由端点补 — 协议有这个字段而官方适配器不填; 客户端解析器会丢弃该字段, 页面从回放行取同一份数据. 用量分两级成行, 都在其事实发生时写入, 不允许推迟到回合收尾: 逐请求的 `request_usage` 随该次响应完成即写 (位于这次响应的正文与工具调用之后、工具执行之前), 回合的 `turn_usage` 随 `RUN_FINISHED` 写; 耗时为该次请求与其响应的时间戳之差. 客户端重放整段会话时, 服务端按**用户输入**切分并裁掉已入库的部分 — 工具回合在两侧的消息分组不同, 逐条比对全部消息必然错位.

## Saved Query

权威是存下的 **SQL + 实体类型** (`metadata` | `actor` | `data`). Browse / 下载时 **Live 重新运行** (命中内存缓存则跳过); **无**后端 Snapshot, 要留当时行集由前端下载.

预设经 Agent 交付或手动创建 (页面 / API); 手动预设无会话归属、直接已保留, 类型只在创建时选定, 名称 / 描述 / SQL 经 PATCH 更新. 创建与改 SQL 前在只读沙箱试跑 (结果不写缓存), metadata / actor 类型须返回 `id` 列. **缓存条目绑定产生它的 SQL**: 命中要求条目 SQL 与本次读到的预设行一致, 故行 id 复用 (无 AUTOINCREMENT) 或失效窗口内回写的旧条目都不会命中; 删除与改 SQL 路径仍须 `invalidate` (含删会话清理未保留预设的路径), 用于回收与触发重算.

呈现规则: `metadata` / `actor` 交付双呈现 (`/meta|/actors?saved_query_id=` 筛选深链 + 数据页); `data` (含全部探查视图) 只渲染数据页, 作列表筛选会 400. 结果缓存 (`ResultCache`) 只存 `columns + rows`, 不解释列语义; `id` 列抽取仅发生在 metadata / actor 交付时作为契约校验 (缺列报错).

交付先绑定会话, `persisted=true` 后与会话解耦; 删会话清理未 persist 预设, **已 persist 保留**. 列表带 `saved_query_id` 时与其它筛选项 **AND**: 预设 SQL 包成 `id IN (SELECT id FROM (…))` 子查询嵌入, 不预物化 id 列表.

## 运行时

`AgentService` 挂载于 `AppRuntime`: `_rebuild()` 按 `hot.agent` 重建工厂, **不清除** ResultCache; bootstrap 装配 `bridge` (safe_dirs / watcher / 动态 Worker 取消 / `FeedService.poll_one`).

上游协议由 `hot.agent.api_type` 选择, 模型构造与翻译共用 `llm/model.py::build_model`; `base_url` / `api_key` / `model` 原样交给对应 Provider (Anthropic 需填 Anthropic 端点, 无隐式改写). 身份与规则经 agent `instructions` 注入而**不是** `system_prompt`: Responses 协议把 agent instructions 放在 API 顶层 `instructions` 字段, 服务端插在 `input` 之前; `system_prompt` 会变成 `input` 里的 system 消息, 于是各 capability 的注意事项反而排在身份定位之前. 思考强度: 全局 `hot.agent.thinking` 为默认 (`None` = 不传), 会话覆盖在 `meta.json`; 每回合经由 `model_settings` 注入 thinking 与 `hot.agent.max_tokens`. 运行上限由 `agent/runtime.py::AGENT_USAGE_LIMITS` 的 `request_limit` 约束单回合请求次数, 工具与输出校验的重试预算为 `Agent.retries`, 两者相互独立. 整回合墙钟上限取 `hot.agent.request_timeout`; 模型请求的限速 (每秒请求数 / 每分钟 token 预算 / 限流冷却) 由 `agent/limits.py::RateLimitedModel` 包在 model 上, 全部会话与会话标题请求共用一个 `AgentService.request_limiter()`. 传输层重试、超时与失败回合的历史写入见 [llm-resilience.md](llm-resilience.md).

**输出上限**: `hot.agent.max_tokens` 默认 16384, 硬上限 65536. 这个字段不只决定"会不会被 length 截断", 还会改变模型的**调用行为**. 2026-10-26 实测 (SiliconFlow + `XingChenAGI/Xing4.0-29B`, 同一 payload 只改该字段): 4096 / 8192 / 16384 / 32768 / 65536 全部正常返回 `tool_calls`, 思考只有 70~270 字符; 一旦填到 128000, 模型以 `finish_reason=stop` 结束且**零个 tool_call** —— 它把调用写成 markdown 文本塞进 reasoning 就收工, 前端表现为"思考很久却什么都没做". 故默认值从 128000 降到 16384, 并用 `le=65536` 挡住手填回去. 排查同类症状先看 `events.jsonl`: 有没有 `TOOL_CALL_START`, 以及两段 `REASONING_MESSAGE_START`→`END` 的墙钟跨度 (正常应是秒级).

**片库事实**: `runtime.py::_SYSTEM` 的 `Library facts` 段声明三件事 —— 片库以日本成人影片为主 (番号寻址), 检索结果全是成人片是该查询的**正常形态**而非搜错; 数据库是库内内容的唯一事实来源, 检索未命中就如实说"库里没有", **禁止**凭记忆列举《…》当作库内命中; 只搜一次, 拿到排序结果就在其上收敛, 不要换措辞反复重试. 缺这一段时 (session 8, 2026-10-26) 模型看到十条日本成人片结果判为 "This is strange", 转去世界知识里逐个猜华语片名, 第二段思考烧掉 170 秒 / 18450 字符却只产出 183 字符回答.

**代理**: `hot.agent.use_proxy` 是总闸, `hot.agent.proxy` 是专用地址, 都空回退 `hot.network.proxy` (解析在 `AgentConfig.resolved_proxy`, 语义与站点级 `SiteConfig.use_proxy` 一致). 助理**自建**传输客户端 (`runtime.py::build_http_client`) 并显式 `trust_env=False`: pydantic-ai 托管的默认客户端只认环境变量 `HTTP_PROXY` / `HTTPS_PROXY`, 设置页里配的代理对它无效 —— 表现为"同一端点翻译能通、助理恒报 `Connection error.`". 客户端由 `AgentService` 持有: 每次 `rebuild` 换新, 旧的进**退休队列**而不是就地关闭 (保存设置可能发生在另一标签页的回合进行中, 立刻 `aclose` 会让那边的请求失败), 队列超过 8 个才关最老的; 进程退出由 `AppSession.aclose` 全关 (provider 不会关外部传入的 client). 自检 (`probe/llm.py`) 呈现与实际请求同一个生效代理, 否则会出现"探针通过而会话不通".
