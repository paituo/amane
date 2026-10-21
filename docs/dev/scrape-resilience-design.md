# Amane 搜刮韧性设计方案：跨任务状态共享 + 站点可用性记忆

> 目标：解决两类问题 —— ① 每个视频的独立搜刮任务无法共享"连接/站点健康"状态；② 没有"哪些站/代理可用"的记忆，导致每个任务无脑重试、既没性能也没效率。
> 触发根因（已实证）：`D:\amane-portable-20261021\data\config.toml` 配置 `network.proxy = "socks5h://127.0.0.1:10808"`，但代理未运行，导致 23 个 `SCRAPE` 任务全部 `CurlError (7) Could not connect to server`，合计浪费约 11.5 分钟连接等待。
>
> 本文所有改动点均标注 `文件:行号`，并逐条给出"能否真正落地"的判定与影响面分析。

---

> **实现状态（2026-10-21，六次更新）**：**P0-① + P1-③ + P0-② + P1-④ + P2-⑧ + P2-⑤ + P2-⑥ + P2-⑦ + P3-⑨ 全部落地。设计文档九项清零。**
> - **P0-① + P1-③**（首轮）：新增 `src/amane/net/connectivity_breaker.py`（熔断状态机，进程单例）；`net/http.py` 注入 `connectivity` + 连接错误 fail-fast + 补 500/502；`net/errors.py` 加 `CIRCUIT_OPEN`；`runtime.py` 注入单例。
> - **P0-②**（本轮）：批次级 skip-list 注入聚合图。
>   - 新增 `src/amane/scrape_availability.py`（`SourceAvailability`：进程级 TTL 缓存（180s）的批次探测，仅 `ConnectivityStatus.FAILED` 计入 `disabled_sources`，`invalidate()` 供配置/代理切换刷新）。**探测刻意不阻塞扇出路径**：`disabled_sources()` 只做非阻塞读——缓存过期时立即返回旧结论（死站大概率仍死）并在后台刷新，从未探测过才返回空集（由请求级熔断兜底首批请求）。实测无网环境下完整探测耗时约 10s，若放在 REFRESH 任务内会让空目录刷新也突然变慢（并超出既有 API 测试 2s 轮询窗口）；异常探测按 30s 退避，不热循环。
>   - `handlers/models.py`：`ScrapePayload` 新增 `disabled_sources: set[str] = set()`（向后兼容，旧任务 JSON 缺字段用默认）。
>   - `observability/models.py`：`SiteOutcomeKind` 新增 `SKIPPED`；`recorder.py:_worse_outcome` 把 `SKIPPED`/`CACHE_HIT` 置为中性（不覆盖 `OK`/`FAILED`）。
>   - `handlers/scrape.py`：`handle` 把 `disabled_sources` 合并进 `compile_priority` 的 `exclude`（作用到**全部字段**），并在建图前记为 `SKIPPED`（不触网、不计失败）。
>   - 三个扇出点注入：`RefreshHandler.__init__(availability)` / `RescrapeHandler.__init__(availability)`（影片 SCRAPE 注入，演员补刮留给熔断器）/ `FeedService(feed_service).set_availability(...)`（仅 `auto_enqueue` 注入，手动批次不注入以免触发真探测）。
>   - `app/runtime.py` / `app/bootstrap.py`：构造 `SourceAvailability` 单例并接线到 handlers / feed_service；热重载 `_rebuild` 透传。
> - **P1-④**（本轮）：网络诊断页暴露熔断器快照。
>   - `api/models/network.py`：`ConnectivityReportResponse` 新增 `circuit_breakers: dict[str, str]`（proxy:/host: 状态）。
>   - `api/routes/network.py`：`check_connectivity` 读取 `runtime.web_client.connectivity.snapshot()` 一并返回，用于诊断"全站批量失败"根因（代理/host 开路）。
> - **测试**：新增 `tests/net/test_scrape_availability.py`（9 例：仅 FAILED 计入 / TTL 缓存 / invalidate 重探 / 冷调用不阻塞 / 过期先服务旧结论 / 并发合并单轮探测 / 异常退避 / wait_idle / 取消静默）；`tests/handlers/test_scraper.py` 追加 `TestScrapeHandlerDisabledSources` + `TestRefreshHandlerDisabledInjection`；`tests/handlers/test_rescrape.py` 追加 disabled 注入用例；`tests/api/test_network.py` 追加 `test_network_check_exposes_circuit_breakers`。更新 `tests/api/test_feeds.py`、`tests/agent/test_feed_ops.py` 的预期 payload（补齐 `disabled_sources: []`）。
> - **验证结果**：受影响单测全部通过；`ruff check` 干净。全量测试与 `d07be84`（无本改动的上游基线）**逐条比对失败清单完全一致**（38 例环境预存失败：Windows 符号链接权限 `WinError 1314`、fastembed 可选依赖缺失、camoufox 后端未随分发提供等），零回归。注：`test_config_routes`/`test_ws`/`test_tasks` 的 3 例曾因初版**前台探测阻塞 REFRESH 任务约 10s**而失败——那不是环境问题，已由「探测改非阻塞后台刷新」修复（见上）。
> - **P2-⑧**（本轮验证）：可恢复 / 续跑 **已满足，无需改代码**。实证：① `MediaFileStatus.FAILED`/`SKIP` 在源码中**只定义、从不写入**（仅 `finalize_media_file` 在成功路径写 `SCRAPED`）；② `_EXCLUSIVE_FIELDS`（`db/repos/tasks.py:18`）仅含 `ACTOR_SCRAPE`，**SCRAPE 不在其中** → `_insert_or_reuse` 总会新建任务；③ REFRESH 按 `status=payload.scrape`（默认 `{PENDING}`）扇出。结论：失败的 SCRAPE 使 media_file 保持 PENDING，后续默认 REFRESH 重新派生新 SCRAPE（亦兼容 `retry_tasks` 克隆失败任务）。新增回归测试 `test_failed_scrape_keeps_media_file_pending` + `test_failed_file_refanned_by_default_refresh` 锁定该不变量。
> - **P2-⑤**（本轮）：跨任务 HTTP 响应缓存（类 Scrapy HTTPCACHE，进程内 TTL + LRU）。
>   - 新增 `src/amane/net/response_cache.py`：`ResponseCache`（`OrderedDict` + TTL + LRU，`enabled` = ttl>0 且 max>0）；`CachedResponse`（curl_cffi `Response` 绑定 curl handle 无法离线构造，故用鸭子类型还原 `status_code`/`headers.get`/`content`/`text`/`json()`/`url`/`encoding`）+ `_CaseInsensitiveHeaders`（大小写不敏感 `.get`，匹配 curl_cffi 语义）。
>   - `net/http.py`：`WebClient.__init__` 加 `response_cache: ResponseCache | None = None`；`request()` 在**熔断检查之前**做缓存查询（命中即返回、不触网、不进熔断统计 → 熔断开路时也能用缓存兜底），2xx 文本类 GET 成功即写入；新增 `bypass_cache: bool = False`（强制刷新）。缓存键 = `method+url+代理+指纹+请求头`（请求头影响响应内容必须进键）。**只缓存** GET、无请求体、无 `Range`（分片下载）、`status<300`、文本类 content-type、正文 ≤ 2MB 的响应；图片/视频/下载/错误响应天然排除。`_record_exchange` 类型放宽为 `Response | CachedResponse | None`。
>   - `config/manager.py`：`NetworkConfig` 加 `response_cache_ttl_s`（默认 300，`ge=0` 置 0 即关闭）+ `response_cache_max_entries`（默认 256，`ge=0`）。均 `x-hidden`，无 UI/迁移影响。
>   - `app/runtime.py`：`build_network_stack` 构造 `ResponseCache(hot.network.response_cache_ttl_s, ...)` 注入 `WebClient`（进程单例 → 跨任务共享）。热重载重建即重置，代价仅首批几次请求。
>   - 与 `CacheKind.metadata` 职责区分：本层缓存**原始 HTTP 响应**（更底层），后者缓存**解析后的 per-site 快照**；后者命中时前者根本不被访问。
>   - 测试：`tests/net/test_response_cache.py`（26 例：命中免触网/字段还原/miss-then-store/POST·Range·大响应·二进制·错误不缓存/bypass 旁路/熔断开路缓存兜底/无缓存向后兼容/TTL 过期/LRU 淘汰/关闭开关/get_text·get_json 命中/大小写不敏感头/键判别）。`tests/net` + `tests/handlers` 407 passed；`build_network_stack` 冒烟确认 ttl=300 已启用。
> - **P2-⑥**（本轮）：自适应限速（类 Scrapy AutoThrottle），`RateLimiters` 的每个 host 换成 `_AdaptiveLimiter`。
>   - `net/http.py`：新增 `_AdaptiveLimiter`（内层仍是容量 1 的 `AsyncLimiter`，**严格平滑契约不变**）；速率只在 `[配置 × floor_ratio, 配置]` 内浮动 —— **配置速率即礼貌上限，永不超过**。`request()` 两个出口（成功/最终失败）各上报一次 `observe(host, latency, status)`：429 → 立即减半（不受 1s 适应间隔约束）；5xx/408/连接错误 → 每间隔减速 15%；慢响应（≥3s）→ 减速 10%；连续 5 次快响应（≤0.5s）→ 向配置速率恢复一步；404/403/406/3xx 不调速。缓存命中不观测（非网络事件）。429 分支与 `_ADAPT_BACKOFF_STATUSES` = `_RETRYABLE_STATUS_CODES` − {429} 保持对称。
>   - 兼容性：`get()` 仍返回带 `.acquire()`/`.time_period` 的稳定对象（`tests/api/test_rate_limiters.py` 既有断言全绿）；`set_rate` 仍换新对象并把自适应状态复位到新配置速率（热重载语义）；`adaptive=False` 时退化为改造前的固定速率桶。
>   - `config/manager.py`：`NetworkConfig` 加 `adaptive_rate_limit`（默认 True）+ `adaptive_rate_floor_ratio`（默认 0.25，`ge=0.05`），均 `x-hidden`；`app/runtime.py` 的 `from_config` 透传。
>   - 与 P0-① 熔断的分工：熔断管"连不上就短路"，本项管"连得上但别太吵"；连接级失败两者都会记录（熔断开路 + 限速温和减速），互不依赖。
>   - 测试：`tests/net/test_adaptive_rate.py`（22 例：429 立即减半/floor 钳制/应激减速按间隔/连接错误/慢响应/恢复单调不降且不超配置/单次快响应不恢复/无关状态保持/关闭开关/低速率绝对下限/并发 acquire 期间调速/RateLimiters 契约回归/set_rate 复位/WebClient 端到端 429·成功·CurlError 三条路径）。`tests/handlers`+`tests/media`+`tests/net`+`tests/api/test_rate_limiters.py`+`tests/observability` 共 **657 passed, 3 skipped**；`build_network_stack` 冒烟确认 `_AdaptiveLimiter rate=5 floor=1.25`。
> - **P2-⑦**（本轮）：多代理池 / 故障转移，`network.proxies`（有序列表，非空即启用）。
>   - 新增 `src/amane/net/proxy_pool.py`：`ProxyPool` 健康态轮选（round-robin），**复用** P0-① 熔断器的 `proxy:<url>` 键（`proxy_fail_max=2`, `cooldown=120s`）——开路代理不被选中，冷却结束自动放出一个半开探针回池。不新增状态机、不加自己的锁（游标推进无 await；健康读走熔断器无锁读路径）。
>   - `net/http.py`：`WebClient.__init__` 加 `proxy_pool: ProxyPool | None = None`；`_proxy_for(host, exclude=)` 在无站点级覆盖时从池内选；代理解析**上移到重试循环外**（缓存键/熔断键/首次尝试绑定同一个代理，修掉了原先循环内外各自 `_proxy_for` 导致同请求两次 pick 的不一致）；连接级失败（`CurlError`/`TimeoutError`）且代理来自池时 `_rotate_proxy` 换下一个未试过的健康代理重发——与指纹轮换一样**不占重试预算**，每个死代理恰好烧掉一次连接 Attempt。
>   - 语义：池是**唯一**代理来源（不静默回退单值 `network.proxy`——静默回退会把流量灌回运营者想 relief 的代理并掩盖"池全灭"信号）；站点级代理覆盖优先于池；池内全部开路 → `CIRCUIT_OPEN` 快速失败（放在缓存查询之后，与单代理语义一致：开路不等于没有可用数据）。
>   - 关键交互修复：池模式下熔断复查**跳过 `proxy_key`**——`pick` 内部的 `is_open` 已消耗半开探针名额，随后再查一次会把"冷却后放行的探针"误判为阻塞，自动恢复会断（`test_cooled_proxy_is_probed_not_blocked_by_second_is_open` 锁定）。
>   - 缓存：池模式把缓存键的代理分量**归一为空串**——池内代理是运营者声明等价的多出口，按代理分会把缓存打散成 N 份（轮询导致同 URL 连续请求交替 miss），归一后任一代理取到的响应全池共享。
>   - `config/manager.py`：`NetworkConfig` 加 `proxies: list[str] = []`（`x-hidden`；**纯新增字段，无 config 迁移**——与改 `network.proxy` 类型的方案相比这是关键差异）；`app/runtime.py` 非空才建池并与 `WebClient` 共享同一 breaker 实例。
>   - 测试：`tests/net/test_proxy_pool.py`（17 例：轮询顺序/跳开路/排除集/全开路或全排除返回 None/冷却后半开探针放行且在飞阻塞/探针成功回池；WebClient：CurlError 轮换/TimeoutError 轮换/全死每个恰好一次/开路后不触网短路 CIRCUIT_OPEN/探针不被二次 is_open 阻断/池模式缓存跨代理共享/站点覆盖优先于池/use_proxy=False 不碰池/无池时全局代理与 fail-fast 向后兼容）。`tests/net`+`tests/handlers`+`tests/media`+`tests/api/test_rate_limiters.py`+`tests/observability`+`tests/scheduler` 共 **842 passed, 3 skipped**；冒烟确认池与熔断器共享实例、未配置时 `_proxy_pool is None`。
> - **P3-⑨**（本轮）：CDP 反爬兜底 —— 「curl 失败 → 降级浏览器后端」逃生通道。
>   - 缺口实证：`HttpClient.get_html` 原有降级只覆盖 **2xx 挑战页**（`classify_block` 正文路径）；而 Cloudflare 挑战常以 **403/503** 返回，`WebClient` 对非 2xx 直接抛 `RequestError`，正文分类根本没机会跑 → 站点记 `FAILED/CLOUDFLARE_CHALLENGE`，浏览器后端空置。`grep CLOUDFLARE_CHALLENGE` 全源码确认仅 `get_html` 正文路径与 `check` 探测路径两处降级，失败路径是真空档。
>   - `crawlers/http.py`：新增 `_BROWSER_SOLVABLE_REASONS = {CLOUDFLARE_CHALLENGE}`；`get_html` 把 `get_text` 包进 `try/except RequestError`，失败语义属该集合且 `AUTO` + 后端可用 → `_browser_required.add(scope)` 后走 `get_rendered`，与 2xx 挑战页**共用同一条切换路径**（首次降级后该来源后续请求直接渲染，不再付一次 curl 失败）。
>   - 收敛原则：只降级**浏览器真正能解决**的失败。IP 封禁/地域限制（同一出口 IP，渲染同样过不去）、连接错误/超时/CIRCUIT_OPEN（同一网络，渲染无意义）一律原样上抛——盲目降级只会白起一次浏览器。探测层 `check` 对 403 挑战本就降级（`probe_get` → `failed(CLOUDFLARE_CHALLENGE)` → `_render_check`），与刮削层至此同形态，本轮补测试锁定。
>   - 测试：`tests/crawlers/test_http.py` 追加 9 例（403 挑战降级渲染/切换对 scope 持久/无浏览器上抛/OFF 模式不降级/5 类不可解失败绝不渲染（IP 封禁·地域·404·超时·CIRCUIT_OPEN）/降级后挑战仍未解决上抛/探测层 403 降级）。`tests/crawlers`+`tests/net`+`tests/handlers`+`tests/aggregate`+`tests/plugins` 共 **1175 passed**（唯一失败为预存环境问题：分发未含 camoufox，`git stash` 验证先于本改动存在）。
> - 设计文档九项（P0-①/③、P0-②、P1-④、P2-⑤/⑥/⑦/⑧、P3-⑨）全部落地。

---

## 1. 现状代码盘点（带证据）

### 1.1 失败根因回顾
- `config.toml`：`network.proxy = "socks5h://127.0.0.1:10808"`（`D:\amane-portable-20261021\data\config.toml`）。
- 每个请求都报 `CurlError (7) Failed to connect to <host>:443 over proxy 127.0.0.1 ... Could not connect to server` —— 是代理本身没在监听，不是站点被墙。
- `net/http.py:325-330`：`except CurlError` / `except TimeoutError` 把 `should_retry=True`，最多 `1+max_retries`（默认 3）次。**连接级错误被当成可重试**，这是浪费的直接代码原因。

### 1.2 HTTP 层已经是跨任务共享的（好消息）
- `WebClient` 是**进程级单例**：由 `app/runtime.py:124-135` 的 `build_network_stack` 构造一次，注入到所有 handler（`runtime.py:533-541`）、`FeedService`（`scheduler/feeds.py:62`）、图片物化、资源下载、R18 导入等。
- 因此以下两项**本来就是跨任务共享**的：
  - `RateLimiters`（`net/http.py:82-151`，按 host 平滑限速）—— 单例。
  - `_FingerprintPicker`（`net/http.py:52-79`，按 host 的 TLS 指纹记忆 + 被拦轮换）—— 单例。
- **真正缺失的是"连接健康/熔断"记忆**：`app/proxy_failure_cache.py` 存在，但 grep 确认它**只在 `api/routes/resources.py` 的图片反代端点使用**，**完全没有接入 `WebClient.request`**（`net/http.py` 全文无 `ProxyFailureCache` 引用）。
- `crawlers/connectivity.py` 的 `ConnectivityChecker` 是现成的并发探测编排，但其结果**没有回灌**搜刮路由——它是独立诊断工具（`api/routes/network.py:31`）。

### 1.3 搜刮扇出点清单（注入 skip-list 必须覆盖全部）
| 扇出点 | 文件:行 | 构造 `ScrapePayload` 处 | 当前是否有 `ConnectivityChecker` |
|---|---|---|---|
| REFRESH | `handlers/refresh.py:136-152` | 是 | 否（需注入） |
| RESCRAPE（影片） | `handlers/rescrape.py:64-76` | 是 | 否 |
| RESCRAPE（演员） | `handlers/rescrape.py:78-93` | `ActorScrapePayload` | 否 |
| Feed 入队 | `scheduler/feeds.py:229-233` → `build_feed_scrape_payload`（`handlers/models.py:109-124`） | 是 | 否（FeedService 持有 `web_client`） |

### 1.4 现有可复用资产
- `compile_priority(route, prefer, exclude)`（`aggregate/engine.py:71-106`）**已经支持 `exclude` 参数**，handler 当前只传了 `field_blacklist`（`handlers/scrape.py:103-105`）。这是注入"死站 skip-list"的**天然入口**，零新机制。
- `ScrapePayload` 是 pydantic `BaseModel`（`handlers/models.py:102-106`）。新增可选字段 `disabled_sources: set[str] = set()` **向后兼容**：旧任务 JSON 缺字段 → 默认值；已落库任务反序列化不受影响，**无需 DB 迁移**。
- `ConnectivityChecker.check(source_ids)`（`crawlers/connectivity.py:77-80`）返回 `list[SourceCheck]`，含 `outcome.status`（`ConnectivityStatus.OK/SKIPPED/FAILED`）——可直接推导出"失败源集合"。

---

## 2. 设计方案总览

| # | 方案 | 优先级 | 落地可行性 | 核心代码落点 | 消除的浪费 |
|---|---|---|---|---|---|
| ① | 熔断（连接健康记忆）接入 `WebClient.request`（proxy + host 两把锁） | **P0** | ✅ 高 | 新模块 `net/connectivity_breaker.py` + `net/http.py:277-330` + `runtime.py:124` 注入 | 代理死 → 首个请求即开路，其余毫秒级短路（11.5min → 数秒） |
| ② | 批次级 skip-list 注入聚合图（让死站显示"跳过"而非"失败"） | **P0** | ✅ 已落地（见 3.2） | `handlers/models.py:102` + `refresh.py:136` / `rescrape.py` / `feeds.py` + `scrape.py:103` | 报告干净 + 省去建图/invoke 节点开销 |
| ③ | 错误分类，停止无效重试（连接错误 fail-fast，补 500/502） | **P1** | ✅ 高（随①自带） | `net/http.py:42`、`325-330` | 单任务 38s → 即时失败 |
| ④ | 全失败根因聚合 + 人工介入（批次 banner + 一键重试） | **P1** | ✅ 已落地（折中版，见 3.4） | 复用 `api/routes/network.py` 暴露 breaker 快照 | 诊断从"未找到元数据"→"代理 10808 拒连" |
| ⑤ | 跨任务 HTTP 响应缓存（类 Scrapy HTTPCACHE） | **P2** | ✅ 已落地（见状态注记） | `net/response_cache.py` + `net/http.py` 熔断前查询/2xx 写入 | 同源同页跨番号复用 |
| ⑥ | 自适应限速（类 AutoThrottle） | **P2** | ✅ 已落地（见状态注记） | `net/http.py` `_AdaptiveLimiter` + 两处 `observe` + `NetworkConfig` 两字段 | 更礼貌：429/慢响应自动减速，恢复有上限 |
| ⑦ | 多代理池 / 故障转移 | **P2** | ✅ 已落地（见状态注记） | `net/proxy_pool.py` + `net/http.py` 上移代理解析/轮换 + `NetworkConfig.proxies` | 消除单点故障：连接失败自动换池内健康代理 |
| ⑧ | 可恢复 / 续跑 | **P2** | ✅ 已验证（无需改代码，见 3.7 注） | `refresh.py:134-136` 状态过滤 + `db/repos/tasks.py:18` 非互斥 | 只重跑失败项 |
| ⑨ | CDP 反爬兜底 | **P3** | ✅ 已落地（见状态注记） | `crawlers/http.py` `get_html` 失败路径降级 | 仅 Cloudflare 挑战类 |

> **关键结论**：① 是根因直击，且自动惠及所有 `WebClient` 调用方（图片、feeds、下载）。② 是①的"体验增强"——没有②，仅靠①已能消除 95% 的时间浪费（请求在 `request()` 内毫秒级短路，只是聚合层仍会把它们记为 `FAILED/CIRCUIT_OPEN`）。建议①先行、②跟后。

---

## 3. 详细设计

### 3.1 P0-① 连接健康熔断接入 `WebClient.request`

**目标**：把"某代理 / 某 host 是否可用"记成进程级内存状态。某 key 连续失败达阈值 → 开路（直接抛错、不触网）；冷却后半开探测一次，成功则关闭。

**新模块 `src/amane/net/connectivity_breaker.py`**（复用 `ProxyFailureCache` 的 `asyncio.Lock` + LRU 思路，但实现 OPEN/HALF_OPEN/CLOSED 状态机）：

```python
class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

@dataclass
class _Entry:
    state: BreakerState = CLOSED
    failures: int = 0
    opened_at: float = 0.0      # monotonic, 开路时刻
    half_open_at: float = 0.0

class ConnectivityBreaker:
    def __init__(self, *, fail_max: int = 3, cooldown_s: float = 120.0,
                 half_open_probe: bool = True):
        ...
        self._lock = asyncio.Lock()

    def is_open(self, key: str) -> bool:
        """OPEN 且冷却未到 → True；否则（含 HALF_OPEN）放行一次探测。"""
    def record_failure(self, key: str) -> None:
        """失败计数 +1；达 fail_max → OPEN（记 opened_at）。"""
    def record_success(self, key: str) -> None:
        """HALF_OPEN 探测成功 → CLOSED；CLOSED 不变。"""
```

**两把锁 key 与阈值**：
- `proxy:<url>`（如 `proxy:socks5h://127.0.0.1:10808`）：`fail_max=2, cooldown=120s` —— 代理死影响所有走它的站，应快速开路。
- `host:<host>`（如 `host:www.dmm.co.jp`）：`fail_max=5, cooldown=300s` —— 单站抖动不应秒杀全站。

**`WebClient` 改造**（`net/http.py`）：
1. `__init__` 新增参数 `connectivity: ConnectivityBreaker | None = None`（`http.py:205`），由 `runtime.py:124` 构造并注入单例。
2. `request()` 在 `await self._limiters.get(host).acquire()`（`http.py:279`）之后、while 循环之前（`http.py:290` 之前）插入：
```python
host_key = f"host:{host}"
proxy_key = f"proxy:{self._proxy_for(host) if use_proxy else ''}"
if self._connectivity is not None:
    for k in (proxy_key, host_key):
        if k and self._connectivity.is_open(k):
            raise RequestError(url, RequestFailure(
                kind=FailureKind.CURL, reason=FailureReason.CIRCUIT_OPEN,
                message=f"circuit open: {k}"))
```
3. 在失败分支（`http.py:325-330` 的 `CurlError`/`TimeoutError`）`record_failure`：
```python
self._connectivity.record_failure(host_key)
if use_proxy and self._proxy_for(host):
    self._connectivity.record_failure(proxy_key)
```
4. 成功返回处（`http.py:315`）`record_success(proxy_key)`、`record_success(host_key)`。
5. 连接错误**不再**走重试预算：在 `should_retry` 计算处（`http.py:327/330`）对 `FailureKind.CURL`/`TIMEOUT` 置 `should_retry=False`（见 3.3）。

**可行性判定：✅ 高。**
- 改动局限在一个方法 + 一个新模块 + `runtime.py` 一行注入。
- 对所有 `WebClient` 调用方（actor_scrape、feeds、resource_store、playback）**自动一致生效**。
- `WebClient` 是单例 → breaker 天然跨任务共享。
- 内存态、不落库 → 无迁移、无跨重启状态依赖（进程/热重载重启后重新学习，代价仅首批几次请求）。

**影响面（见第 4 节详析）**：并发安全需 `asyncio.Lock`；误杀风险靠阈值+冷却+半开探测缓解；连通性诊断端点需能绕过熔断（避免自锁）。

---

### 3.2 P0-② 批次级 skip-list 注入聚合图

**目标**：把"本批次已确认不可用的源"作为 skip-list 写入每个 `SCRAPE` 任务的 `compile_priority` `exclude`，使死站显示 `SKIPPED` 而非 `FAILED`，并省去建图/invoke 节点。

**改动点**：
1. `handlers/models.py:102` `ScrapePayload` 新增：
```python
disabled_sources: set[str] = Field(default_factory=set)
```
（向后兼容：旧 JSON 缺字段用默认值，`model_dump(mode="json")` 也正常序列化 `set` → `list`。）
2. `handlers/scrape.py:103-105` 把 `disabled_sources` 并入 `exclude`：
```python
exclude = {
    f: list(s) for f, s in self._config.scraping.field_blacklist.items()
}
# 合并批次级禁用源（整字段链禁用，等价于"该源整体不可用"）
if payload.disabled_sources:
    exclude = {f: [*s, *payload.disabled_sources] for f, s in exclude.items()}
    # 或构造一个覆盖全字段的 disabled 映射
field_priority = compile_priority(route, self._config.scraping.field_priority, exclude)
```
   `compile_priority` 的 `exclude` 是按字段剔除的（`engine.py:83-85`）；为"整源禁用"需把 `disabled_sources` 应用到**所有字段**。可在 handler 内构造 `exclude = {field: disabled_sources for field in ALL_FIELDS}` 与 `field_blacklist` 合并。
3. 扇出点注入（必须覆盖全部，否则有遗漏）：
   - `RefreshHandler.handle`（`refresh.py:136-152`）：在构造 `ScrapePayload` 前，调用 `ConnectivityChecker.check()`（结果按 TTL 进程级缓存），把 `failed source_ids` 写入 `payload.disabled_sources`。需给 `RefreshHandler.__init__` 注入 `ConnectivityChecker`（当前 `refresh.py:28-33` 只接收 `repo/media_extensions/inventory_store`）。
   - `RescrapeHandler`（`rescrape.py:64-76` 影片、`78-93` 演员）：同上加 `disabled_sources`。
   - `FeedService`（`feeds.py:229-233` → `build_feed_scrape_payload`）：FeedService 已有 `web_client`；可复用同一进程级 connectivity 缓存。
4. `observability/models.py:45-48` `SiteOutcomeKind` 新增 `SKIPPED = "skipped"`（向后兼容枚举扩展），并在 `recorder.py:455-456` 的 `_worse_outcome` 排序里把 `SKIPPED` 置于 `CACHE_HIT` 同级（非失败，不覆盖 `OK`）。
5. 聚合层在 `compile_priority` 剔除后，`execute_graph` 对已排除节点**不调用 `invoke_source`**（`engine.py:191-193` 对不在 `crawlers` 的节点标空结果；需在 `engine` 或 handler 内对 `disabled_sources` 也走同样"标空不计入 failed"的分支，或在 `ScrapeHandler` 里过滤 `crawlers`）。

**可行性判定：⚠️ 中高，改动面比①大。**
- `compile_priority` 的 `exclude` 机制现成（零风险）；`ScrapePayload` 新字段向后兼容（零风险）。
- 真正的工作量在**三个扇出点都要注入 `ConnectivityChecker`**，且要保证"批次级可用性缓存"进程单例（避免每次 REFRESH 都重新探测）。漏掉任一扇出点 → 该路径仍会白跑死站。
- 与①的关系：② 是体验增强，① 已能在网络层消灭浪费。建议①合并入首个 PR，② 后续 PR。

**落地形态（与初版设计的差异）**：探测未按原计划放在 `RefreshHandler.handle` 内前台执行，而是收敛为 `src/amane/scrape_availability.py` 的 `SourceAvailability`——`disabled_sources()` 是**非阻塞读**（过期即返回旧结论 + 后台刷新；从未探测过返回空集，由熔断器兜底首批），`wait_idle()` 供测试/优雅关闭等待在飞探测。原因：实测无网环境完整探测约 10s，前台执行会把健康检查成本转嫁给 REFRESH 任务（空目录刷新也变慢，并超出既有 API 测试的 2s 轮询窗口）。

---

### 3.3 P1-③ 错误分类，停止无效重试

**改动点**（`net/http.py`）：
1. `_RETRYABLE_STATUS_CODES`（`http.py:42`）补 `500/502`：
```python
_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
```
2. 连接级错误（`CurlError`/`TimeoutError`，`http.py:325-330`）**不再** `should_retry=True`，改为 `should_retry=False` + 由①的 breaker 触发开路（fail-fast）。`404/空` 本就不重试。
3. `FailureReason`（`net/errors.py:37-66`）新增 `CIRCUIT_OPEN = "circuit_open"`；`classify_request_error`（`errors.py:161-179`）在 `failure.reason` 已设置时直接返回（已有的 `if failure.reason is not None: return failure.reason`），无需额外分支。

**可行性判定：✅ 高。** 纯常量/分支调整，随①一并落地。

---

### 3.4 P1-④ 全失败根因聚合 + 人工介入

**现状痛点**：23 个任务各自报 `error="未找到 X 的元数据"`，真因（代理拒连）埋在 `outcomes` 明细里；REFRESH 只数 scrape 数量（`refresh.py:154-158`），不聚合子结果。

**落地（折中、高性价比，无需大改前后端）**：
1. 暴露 breaker 快照 API：`WebClient.connectivity.snapshot() -> dict[proxy_key/host_key, state]`，新增到 `api/routes/network.py` 的 `check_connectivity` 响应或独立端点。前端网络检测页直接显示"代理 `127.0.0.1:10808` 开路"。
2. **关键**：连通性诊断端点需能**绕过熔断**探测（避免自锁）——给 `request()` 加 `bypass_circuit: bool = False` 参数，`ConnectivityChecker` 的探测走 `bypass_circuit=True`，保证诊断永远反映真实状态。
3. 一键重试：现有任务列表已具备重跑能力，无需新开发。

**可行性判定：⚠️ 中。** 完整版（父任务聚合子结果 + 前端 banner + 自动根因文案）需改完成事务与前端，工作量大；上述折中版仅需在 `network.py` 暴露快照 + `request()` 加 `bypass_circuit`，改动小且立即可用。

---

### 3.5 P2-⑤ 跨任务 HTTP 响应缓存

**现状**：`CacheKind.metadata` 是按 `media_file` 的 per-site 原始快照（`handlers/models.py:75-81`、`scrape.py:68-72`），不是按 URL 的响应缓存，跨番号不共享同一 dmm/javbus 搜索页。

**落地**：在 `WebClient` 内加一层（key=`method+url+proxy+fingerprint`，短 TTL，LRU 复用 `ProxyFailureCache` 思路）。需缓存 `content+status+headers` 并自构造 `Response`（curl_cffi `Response` 不可直接 pickle/缓存），并提供强制刷新开关（避免掩盖站点 schema 变更）。

**可行性判定：⚠️ 中。** 优先级低于①②。注意与 `metadata` 缓存职责区分：本层缓存的是"HTTP 响应"，`metadata` 缓存的是"解析后的 per-site 快照"。

---

### 3.6 P2-⑥ 自适应限速 / P2-⑦ 多代理

- **⑥**：`RateLimiters` 当前是固定 `AsyncLimiter(1,1/rate)`（`http.py:82-151`）。改成延迟感知的可调限流器（类 Scrapy AutoThrottle）收益主要是"更礼貌"，对"代理死"无直接帮助，优先级低。
- **⑦**：`network.proxy` 当前是单值（`runtime.py:125`）。改多代理需改 **HotSettings config 类型 + 配置加载 + UI**，影响面大。建议先做"单代理死即快速失败+告警"（①已覆盖大半），多代理池作为后续增强。

---

### 3.7 P2-⑧ 可恢复 / 续跑

**已有基础**：`refresh.py:130-132` 取 `list_media_files(library_id, status=payload.scrape)`，默认 `payload.scrape={PENDING}`（`models.py:87`）→**只扇出未刮过的，已刮的不重复**。

**需验证项（非新功能）**：FAILED 的 `SCRAPE` 任务对应的 `media_file` 状态是否回到 `PENDING`。若停在别的状态，"一键重跑全失败"无法真正续跑。建议在落地①后实测确认；若不符合预期，加一处小修复（失败任务将其 `media_file` 状态复位为 `PENDING`）。

**可行性判定：✅ 已有基础，仅需验证/小修。**

---

### 3.8 P3-⑨ CDP 反爬兜底（已落地）

**骨架已存在**：`BrowserPool`（`runtime.py:136-146`）+ per-site `use_browser`/`BrowserMode`（`config`）。本次故障是代理死，不是 Cloudflare 挑战，暂不必动。仅当实测某些源触发挑战时，再扩展"curl 失败 → 降级浏览器后端"的逃生通道。

**落地结论（本轮）**：逃生通道已补全，缺口在 `HttpClient.get_html` 的**失败路径**——
原有降级只覆盖 2xx 挑战页（`classify_block` 正文启发式），而 Cloudflare 挑战常以 **403/503** 返回，
`WebClient` 对非 2xx 直接抛 `RequestError`，正文分类没机会跑。现在失败语义为
`CLOUDFLARE_CHALLENGE` 且 `AUTO` + 后端可用时，与 2xx 挑战页共用同一条切换路径降级渲染；
其余失败（IP 封禁/地域限制/连接错误）换浏览器同样无解，原样上抛。详见顶部状态注记。

---

## 4. 影响面与风险分析

### 4.1 并发安全
- `WebClient` 是单例，breaker 的 `is_open/record_*` 在 event loop 内被大量协程并发调用。`ProxyFailureCache` 已用 `asyncio.Lock`（`proxy_failure_cache.py:23`）作为范式，新模块必须同样加锁（读多写少，锁开销可忽略）。

### 4.2 持久化 / 迁移兼容性
- ①的 breaker、②的 skip-list、⑤的响应缓存**全部内存态**，不落库 → **无 DB 迁移**。
- `ScrapePayload.disabled_sources`（可选默认空）、`SiteOutcomeKind.SKIPPED`（枚举新值，只由新代码写）、`FailureReason.CIRCUIT_OPEN`（枚举新值）均为**向后兼容扩展**：旧任务 JSON / 旧 `summary.json` 反序列化不受影响。
- **唯一需迁移的是⑦（多代理）**：改 `network.proxy` 类型需 config 迁移 + UI，故建议后做。

### 4.3 误杀风险（最关键）
- proxy 锁 `fail_max` 太小（如 1）会在代理**短暂抖动**时误开路 2–5 分钟、暂停全部刮削。缓解：proxy 锁 `fail_max=2` + 短冷却(120s) + 半开探测；host 锁 `fail_max=5` 防单站瞬时抖动。
- 半开探测保证"代理恢复后自动 reopen"：冷却到期后放行一次真实请求，成功则 `record_success` → CLOSED。
- 可选优化：proxy_key 由 OPEN→CLOSED（恢复）时，批量 `forget` 共享该代理的 host_key，避免"代理已活但个别 host 仍被锁"的短暂窗口。

### 4.4 连通性诊断端点的"自锁"
- 若 breaker 已开路，`ConnectivityChecker` 的探测请求也会被 `request()` 短路 → 返回 `CIRCUIT_OPEN` 而非真实探测结果。这对"显示代理死"无害，但会让诊断页无法区分"代理死"与"个别站死"。
- **对策**：给 `request()` 加 `bypass_circuit: bool = False`；`ConnectivityChecker._run`（或直接在被诊断路径）传 `bypass_circuit=True`，保证诊断永远真实触网。

### 4.5 其它调用方波及
- ① 对所有 `WebClient` 使用者**一致生效**（图片物化 `materialize_images`、资源下载 `download`、feeds、playback）。
- 好处：代理死时图片下载也快速失败，不再白等。
- 风险：某 host 误开路会同时挡住该 host 的图片下载——靠 TTL + 半开探测恢复，可接受。

### 4.6 与 `check_connectivity` 端点的关系
- 该端点复用同一 `WebClient`，其探测请求也会被 breaker 约束（见 4.4）。P0-② 的批次探测应走 `max_attempts=1` 且 `bypass_circuit=True` 以避免自锁（探测本身就要"第一个触发开路"）。

### 4.7 热重载
- `build_network_stack`（`runtime.py`）在热重载时可能重建 `WebClient` → 新 breaker 实例 → 状态重置，重新学习。代价仅首批几次请求，**可接受**（已在前文标注）。

---

## 5. 推荐实施顺序

1. **P0-① + P1-③**（一个 PR）：新增 `net/connectivity_breaker.py`；`net/http.py` 注入 breaker + 连接错误 fail-fast + 补 500/502；`net/errors.py` 加 `CIRCUIT_OPEN`；`runtime.py` 注入单例。**立刻消灭"代理死全库白跑 11 分钟"**，且自动惠及所有调用方。
2. **P2-⑧ 小验证/修复**：确认并修复 FAILED 任务状态，保证可续跑。
3. **P0-②**（后续 PR）：skip-list 注入 + `SKIPPED` Outcome + 复用网络检测页 → 报告干净。
4. 视资源推进 P2-⑤/⑥/⑦ 与 P3-⑨。

---

## 6. 测试建议（符合 `docs/dev/testing.md` 的边界/非法用例要求）

- `connectivity_breaker`：OPEN（连续失败达阈值）→ HALF_OPEN（冷却内放行一次）→ CLOSED（探测成功）；并发 `record_failure` 不丢计数；`asyncio.Lock` 下多协程 `is_open` 一致性。
- `http.py` 注入 breaker：mock breaker 全开 → `request()` 抛 `RequestError(CIRCUIT_OPEN)` 且不触网（`_session.request` 调用次数为 0）；breaker 关闭 → 正常发请求；连接错误 → `record_failure(proxy_key/host_key)` 且 `should_retry=False`；成功 → `record_success`。
- `models.py`：`ScrapePayload` 旧 JSON（无 `disabled_sources`）反序列化得默认值；`model_dump(mode="json")` 正常。
- `engine.py`：`compile_priority` 在 `exclude` 含整源时，该源不出现在任何字段链（回归既有单测）。

---

## 7. 回滚策略

- ①/③ 全为新增可选模块 + 行为调整，无 schema 变更。若误杀严重：调大 `fail_max`/冷却，或直接给 `WebClient` 传 `connectivity=None`（运行时开关）即可彻底关闭熔断。
- ② 的 `disabled_sources` 默认空集，不启用 skip-list 时零影响；可通过不注入 `ConnectivityChecker` 关闭批次探测。
