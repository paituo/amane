# 配置系统

> 入口: `src/amane/config/`. 本文记录 Cold / Hot 分层、进程内 rebuild 与配置项增补规范.
> 启动编排见 [architecture.md](architecture.md).

## Cold / Hot 分层

`src/amane/config/manager.py` 把配置划分为两份:

- **ColdSettings** (`AMANE_*` 环境变量) — 路径类、安全边界等进程级绑定. 修改后必须重启, 因为 `data_dir` 等会派生出 SQLite 路径、TOML 路径、resources 目录.
- **HotSettings** (TOML, 经 `PATCH /api/config` 写入) — 行为参数. 多数变更经由 `AppRuntime.apply_rebuild()`, 不重启进程.

`data_dir` 不允许放入热配置: 切换目录须迁移 DB、resources 与未完成任务. 判据是 Cold = 派生路径 / 进程级绑定, Hot = 行为参数.

## API token (`AMANE_TOKEN`, Cold)

API 鉴权是冷配置: 中间件在请求路径上, 不能在进程内 rebuild 中替换.

- **未设置 (auto, 默认)**: 启动时生成随机 token 并持久化到 `data_dir/token` (0600, 重启复用). 除 `GET /api/health` 外 `/api/*` 需 `Authorization: Bearer` 或中间件下发的 HttpOnly SameSite=Lax cookie; 浏览器侧全部经由 cookie, token 不落 localStorage, 不出现在 URL 与访问日志. 启动日志打印 token, 容器从 `docker logs` 取.
- **`AMANE_TOKEN=off`**: 显式关闭, 仅当反代已实现等价鉴权时使用.
- **`AMANE_TOKEN=<value>`**: 显式 token.

信任边界: 持有 token 即视为用户本人, `safe_dirs` 只作纵深防御, 同机能读 `data_dir/token` 视为已信任. `AMANE_SAFE_DIRS=ALLOW_ALL` 关闭路径边界 (桌面壳默认); Docker 与无哨兵时仍按目录名单约束. 桌面 argv 取得 token 的方式见 [desktop.md](desktop.md).

## 进程内 rebuild

`AppRuntime._rebuild()` 重建依赖 HotSettings 的对象链:

```
RateLimiters → WebClient → HttpClient → CrawlerFactory
  → Handlers → AsyncWorker
  → PlaybackFactory (独立码流客户端)
  → AgentService.rebuild (仅换 Agent 工厂 / 缓存参数)
```

`logging.level` 在 rebuild 内直接修改 logger, 不依赖对象重建.

**不重建的对象**: `Repository`、`EventBus`、`WatcherService`、`FeedService`、`ResourceStore`、`TranslationCache`、`ProxyFailureCache`、`AgentService` 内的 `ResultCache` — 它们的状态是会话级的 (DB 连接池、WS 客户端、watchdog observer、feed 轮询循环、资源去重表、译文缓存、负缓存、交付结果缓存), 重建会切断现有连接或丢弃缓存句柄. `_rebuild()` 只把新 `WebClient` 交给 `FeedService.set_web_client`.

`watcher.use_polling` / `media_extensions` / `debounce_seconds` 与 `[everything]` 的全部键在 `start_app` 构造 `WatcherService` 时一次性注入, **不随 rebuild 更新**, 修改 TOML 后须重启; Library 级的 `automation` / `ingest` / `cloud_path` / 路径 / `trailer_pattern` 等由 libraries 路由热更新, 与这些项无关. 契约见 [watcher.md](watcher.md).

Worker 替换不取消运行中任务: `_rebuild()` 构建新 worker 后旧 worker `retire()` (停止认领并退出主循环), 新 worker 立即开始认领; 旧 worker 的已认领任务继续运行, 清零后由后台释放其持有的 r18 句柄与浏览器池, 连续变更可同时存在多个退役 worker. 配置 PATCH、插件启用 / 禁用、插件安装 / 卸载 / 重新扫描都经由 `AppRuntime.apply_rebuild()`, 串行化这段替换. 归属以认领开始时刻为准: 变更发生时尚未结算的认领属于旧 worker, 每次退役至多带走一个旧配置任务.

**r18 只读引擎**: 只在 `hot.r18` 实际变化时重建. 旧引擎由 `R18Handle` 标记所有权, 等使用它的退役 worker 排空且无其它 worker 引用后由后台关闭; `AppRuntime.stop_workers()` 在关闭时负责当前句柄. 契约见 [task-system.md](task-system.md).

**浏览器池**: 只在 `network.browser` / `network.proxy` 变化时重建 (见 `src/amane/app/runtime.py::AppRuntime._rebuild`), 其余热重载复用同一实例以保留已解决的挑战会话; 被替换的旧池由 `_release_browser()` 在引用它的退役 worker 排空后关闭.

**检索栈**: 只在 `hot.search` 变化时重建 (`AppRuntime._replace_search_if_needed`). 其中 `embedding` 子配置 (provider / model / dim / batch_size / timeout_s / threads) 逐字段相同、且向量路仍然启用时, 旧 provider 连同一个还在跑的预热任务整体移交给新栈, 不重新加载模型 —— 否则改一个 `fusion.zgate` 也要付一次模型加载. 移交后旧栈 `aclose()` 不再关闭该 provider, 所有权随 `ReusedEmbedding` 转移; 向量路被关掉时不接管, 由旧栈连同模型一起释放. 契约见 [search.md](search.md).

## 配置自检 (`POST /api/config/probe`)

设置页可测分区标题右侧的「测试」按钮对应这个端点. 设计约束:

- **不进任务队列**: 结果要立刻回到点按钮的人手里, 也不该占用 worker 名额 (与 `/api/network/check` 同一取向).
- **必须能测未保存的值**: 请求体 `{section, values}` 的 `values` 经 `ConfigManager.preview()` 合并成一份临时 `HotSettings`, 只校验不落盘、不触发 rebuild. 因此"改完先测、测通再存"可行; 连 Pydantic 都过不了的配置仍以 422 拒绝, 不浪费一次真实请求.
- **流式**: 响应为 NDJSON (`application/x-ndjson`), 一行一条 `{seq, level, message, detail, elapsed_ms, step_ms}`, 末行 `{done: true, ok, elapsed_ms}`. 单步失败只体现在事件里, 流始终以 `done` 收尾 —— 用 5xx 表达"代理不通"会让前端丢掉已经产生的那几步.
- **只读**: 不写库、不建表 (索引规模经 `mode=ro` 连接读取)、不改配置; 自建的 provider 结束时 `aclose()`, 复用运行中的实例则不关.

注册表在 `src/amane/probe/registry.py`, 现有四个分区:

| section | 检查内容 |
| --- | --- |
| `llm` | 按 `auth_mode` 判定凭据 (密钥, 或 guest 档的 deviceId) 与模型是否齐备 → 经 `build_model` + `model_request` 发一次最小请求 (与翻译同一条构造路径, 才能暴露"协议与端点不匹配") → **真实翻译一条标题** (样例取库内日文标题, 库里没有则用内置样例并标注来源), 事件里给出原文与译文 |
| `agent` | 同上, 但用 `agent` 自己的凭据与 `api_type` → **真跑一个回合** (要模型用 `sql_explore` 查真实库), 报告回答与工具调用次数; 一次工具都没调用判 `warn` |
| `search` | 开关组合 → `sqlite-vec` 能否加载 → 索引规模 → 真的算一条向量并比对 `dim` → 索引里的 model/dim 是否与当前配置一致 → **走运行中的检索栈真查一次**, 列出命中条目 (按相关度顺序); 无命中判 `warn` 并说明是索引空还是词不对 |
| `network` | 代理语法 → 直连与经代理各请求一次 `cloudflare/cdn-cgi/trace` → 比对出口 IP (相同即判"代理未生效") → **用真实番号抓一次详情页**, 报告页面标题/字节/耗时 (拿回验证页或空壳即暴露) |

前两步只回答"配置是否成立", 末尾的**真实数据段**回答"功能跑起来是什么效果": 它要求 `ctx.runtime` 在位 (样例取自真实库, 检索与助理回合复用运行中的装配), 没有运行时就整段跳过而不产生事件. 真实数据段失败一律降级为 `warn` —— 站点侧拦截或模型不听话不等于配置写错了, 判死会让用户去改一个本来就对的字段.

新增分区只需在注册表登记一个 `async def(ctx) -> AsyncIterator[ProbeEvent]`; 前端 `PROBE_SECTIONS` 另有一份 section 名单 (按钮按 section 名渲染), 两处需同步.

代理探针的 httpx 客户端显式 `trust_env=False`: 否则 `HTTP(S)_PROXY` 环境变量会让"直连"名不副实, 出口 IP 对比随之失去意义.

## TOML 持久化

写 TOML 必须用临时文件 + `os.replace` 原子化 — 直接覆盖时写入中途进程中止会留下空文件. `tomli_w` 不接受 `None` / `set`, 持久化前须按 JSON 模式导出并剔除 `None` 与默认值.

`PATCH /api/config` 的 `_deep_merge` 只合并两层: 顶层 section 及其直接子对象. 更深的嵌套对象 (如 `network.browser`) 由 patch 整体替换, 未列出的子字段回退默认值. `site_config` / `rate_limits` 等字典依赖整体替换删除 key, 不允许改为递归合并.

## 配置项增补规范

字段加在 `src/amane/config/manager.py` 对应 section model (全部配置 model 集中在该文件), 然后:

1. 需要 UI 展示时添加 `json_schema_extra` 的 `x-*` 扩展 (`x-*` 清单见 `web/src/components/schema-form/schema/types.ts`). 站点列表字段必须用 `site_roles` 的 schema 收窄 `items.enum`, 不允许直接暴露完整 `SiteName`; 站点级字段的 `x-visible-keys` 只列消费该字段的站点.
2. 若新字段影响限速 / HTTP / 爬虫 / LLM / handler 行为, 确认 `_rebuild()` 链能传播变更; 若影响 WatcherService 构造参数, 须标明「重启生效」.
3. `just generate` 同步前端 schema, 并补 `web/src/i18n/` 翻译, 否则构建失败.

`x-frozen-keys` 全量 dict (`site_config` / `content_routes` / `field_language`): `default_factory` 只在整段缺席时生效; 文件里已有该字段但缺 key 时, 校验按代码枚举补默认并丢弃未知 key. UI 不能加 key, 不补则新项无法配置. `GET /api/config` 始终返回全集.

`content_routes`、`field_priority` 与 `field_blacklist` 的值允许第三方 `namespace.local` 来源 ID, 实际可用性由 `PluginManager` 校验; 已禁用或未安装的来源可以留在路由里, 刮削时跳过, 不阻断启动或配置写入. 插件自己的配置放入 `plugins.<source_id>`, 由插件提供的 Pydantic model 校验, 不放入 `site_config`.

`AMANE_SUPERVISED=1` 声明进程外监督者在场 (compose 与桌面壳设置). 不允许在无监督循环的 `amane.server` 内设置该变量, 否则 `exit 3` 会使进程退出且无人再次启动. 为真时 `POST /api/system/restart` 可用. 不探测 cgroup, 以免在 K8s 里误开应用内重启.

`AMANE_UPDATE_URL` 覆盖版本检查的 GitHub 地址 (空 = 官方 API 的发布列表; 指向镜像的 `/releases/latest` 也可以, 单条发布的响应同样接受).

## `scraping` 影片路由 (Hot)

`content_routes` 是按内容类型的**有序站点链**: 资格真值 + 该类型默认字段顺序. 实际请求的站点 ⊆ 此表, 空表则该类型刮削直接失败. 关闭某类型刮削须把该项设为空列表, 不允许删除 key.

`field_priority` 是稀疏字段例外: 只写需要提前尝试的站; 编译时与该类型路由求交后前置, 其余路由站点保序回退. `field_blacklist` 是稀疏字段排除: 只写该字段不采用的站. 二者同时列出同一站时以字段排除为准. 不在该类型路由中的站无效, 也不额外发请求. 取值顺序与空值回退见 [task-system.md](task-system.md).

外部影片来源的 descriptor 参与路由校验: 声明 `content_types` 时路由类型必须在声明集合内, 声明 `metadata_fields` 时字段优先级与字段排除只能选择声明过的字段; `traits` 决定来源的多语言节点展开与聚合分段.

建图对编译后站点链的消费见 [task-system.md](task-system.md), 默认表取舍见 [content-routes.md](content-routes.md).

## `actor_scraping` (Hot)

演员刮削与影片 `scraping` 分 section: 影片管线不读演员站列表, 演员任务也不读 `field_priority` / `field_blacklist`. 契约 (实现见 `ActorScrapeHandler` / `aggregate.actor`):

- **`profile_sites`** / **`image_sites`**: 资料来源与头像来源的顺序 (单源字段填空优先级, 头像优先于资料来源附图). 默认与 schema 枚举都是注册表里声明了对应能力的插入序, 不在配置层手写站点名单.
- **`download_images`**: 是否经 ResourceStore 缓存头像 (URL 仍为远端 locator).
- **`auto_scrape`**: 影片刮削成功后自动链式入队该片演员的 `ACTOR_SCRAPE` 任务; 见 [task-system.md](task-system.md).
- **`gfriends_repo`**: gFriends 仓库 URL, Filetree 缓存在 `data_dir`.

跨角色站点写入会被 section validator 拒绝. 发请求前仍按 `Actor.gender` 与站点性别覆盖再过滤 (见 [crawlers.md](crawlers.md)).

## `watermark` (Hot)

只在 ORGANIZE 落盘封面时绘制, 不修改 Resource; 片库 CSS overlay 不读 `enabled`. 角标高度 = 图高 × `scale` (不按宽、不按 PNG 原图像素); `corners` 是 `x-frozen-keys` 五类到四角的映射, 同角按中字 / 无码 / 破解 / 流出 / 分辨率向内叠. PNG 覆盖仍是 `{data_dir}/watermarks/{stem}.png`.
