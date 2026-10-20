# 插件开发接口与 Everything 接入可行性分析

> 证据标记: 【源码】= 本仓库读到的定义/实现; 【文档】= `docs/dev/` 已有契约; 【推断】= 推理, 未验证.

## 结论速览

1. **插件 API 目前只有 v1, 只开放两类能力: 影片元数据来源 与 播放源.** 文件发现、入库、任务类型、API 路由、React 页面、演员来源都不对插件开放【文档 `docs/dev/plugins.md`】.
2. **"用 Everything 获取来源数据"要分三种意图**: 做刮削来源(只能被动按番号取元数据, 拿不到全局文件视图)、做播放源(不能, `file` 目标必须是已索引文件)、做文件发现(插件做不到, 必须改主机).
3. **唯一在契约内、且明确允许插件自己查本地磁盘的口子是播放源的 `subtitle` 钩子**【文档 `docs/user/plugins.md`】, 外挂字幕检索是 Everything 在插件里最合规的落点; 整库发现提速必须改主机侧 `scan_inventory` / 新增 ingest 通道.

---

## 一、插件体系支持哪些接口

### 1.1 能力边界 (API v1)

| 项 | 事实 | 来源 |
|------|------|------|
| API 版本 | `PLUGIN_API_VERSION = "1"` | 【源码】`src/amane/plugins/models.py` |
| 开放能力 | `FilmSourcePlugin`(影片元数据) / `PlaybackPlugin`(播放源), 同一类可同时继承二者 | 【源码】`src/amane/plugins/api.py` |
| 能力枚举 | `SourceCapability`: `film_metadata` / `actor_profile` / `actor_image` / `playback`; 第三方只能声明前两者的子集——descriptor 未声明 `film_metadata`/`playback` 之一即加载失败 | 【源码】`plugins/models.py` + `plugins/manager.py::_require_matching_capabilities` |
| 加载方式 | `{cold.data_dir}/plugins/sources/<id>/plugin.py`, `importlib` 按目录名排序加载, 导出名为 `Plugin` 的类 | 【源码】`plugins/manager.py::PluginManager.discover` |
| 插件 ID | 必须 `namespace.local`(≥2 段, 小写); namespace 不得为 `amane`/`plugin`/`official`/`builtin` 或任何内置 `SiteName`; **ID 是持久化 key, 改 ID 会让历史 `raw` 失去身份** | 【源码】`plugins/models.py::validate_external_source_id` |
| 运行形态 | 进程内纯 Python, 与主机共用解释器, **无沙箱**; 禁止自带第三方依赖与原生扩展; 标准库可放心 import(桌面打包版收集整个标准库) | 【文档】`docs/dev/plugins.md` |
| 运行时数据 | `PluginContext.data_dir` = `{data_dir}/plugins/<id>`; 卸载只删源码树, 不删运行时数据 | 【源码】`plugins/api.py::PluginContext` |

### 1.2 影片元数据来源插件 —— 可实现的接口

来源: 【源码】`src/amane/plugins/api.py`, `src/amane/crawlers/models.py`, 【文档】`docs/user/plugins.md`

| 接口 | 形状 | 契约要点 |
|------|------|------|
| `FilmSourcePlugin.descriptor()` | → `SourceDescriptor` | 声明 `id`/`name`/`version`/`capabilities`/`content_types`/`metadata_fields`/`languages`/`urls`/`traits`/`rate_limit`; `id` 必须等于目录名 |
| `FilmSourcePlugin.configuration_model()` | → Pydantic model | 界面按 JSON Schema 渲染表单; 含 `token`/`api_key`/`secret` 的字段在任务快照中自动脱敏 |
| `FilmSourcePlugin.build(context, config)` | → `FilmSourceProvider` | 在目录发现期实例化, provider 由 `CrawlerFactory` 按来源 ID 延迟创建并缓存 |
| `FilmSourceProvider.fetch(query, options)` | `SearchQuery` → `MediaMetadata \| None` | 未命中返回 `None`; 网络/可分类失败抛 `SourceError`(`RequestError` 同), **禁止** `except RequestError: return None` 与裸 `except Exception` |
| `FilmSourceProvider.check_connectivity()`(可选) | → `ConnectivityOutcome \| None` | 供「网络检测」页探测; `None` = 主机探测 `descriptor.urls[0]`; 凭据缺失返回 `ConnectivityOutcome.skipped(SkipReason.MISSING_CREDENTIAL)` |

输入 `SearchQuery`: `number`, `file_path`(关联 MediaFile 的绝对路径, 无关联文件时 `None`), `file_hash`(仅声明 `uses_file_hash` 时计算), `content_type`, `partial_result`(仅第二段来源非空, 只读)【源码】`crawlers/models.py` + `handlers/scrape.py` 构造处.

输出 `MediaMetadata`: `number`/`title`/`actors`/`studio`/`publisher`/`release`/`runtime`/`tags`/`series`/`plot`/`poster_urls`/`thumb_urls`/`trailer_urls`/`score`/`external_id`/`source_url`/`directors`/`extrafanart`.

行为开关 `traits`: `needs_partial`(排第二段)、`multi_language`(按字段语言展开, 经 `options.language`)、`uses_file_hash`【源码】`plugins/models.py::SourceTrait`.

### 1.3 播放源插件 —— 可实现的接口

| 接口 | 形状 | 契约要点 |
|------|------|------|
| `PlaybackPlugin.build_playback(context, config)` | → `PlaybackProvider` | 与 `build` 分离, 避免双继承返回类型冲突 |
| `PlaybackProvider.probe(query)` | → `tuple[PlaybackOffer, ...]` | 列出本源可提供的流; `selected_key` 恒 `None`; 不可播的候选也列出并写 `unavailable`; 全部无内容抛 `SourceError(NO_USABLE_METADATA)` |
| `PlaybackProvider.resolve(query)` | → `PlaybackTarget \| None` | 按 `query.selected_key` 定位; `None` = 插件自己挑 |
| `PlaybackProvider.subtitle(query, track_id)`(可选) | → WebVTT 文本 / `UpstreamPlaybackTarget` / `None` | **需要读本机字幕文件时由插件自行读取并转成 WebVTT** — 主机不读字幕文件 |

输入 `PlaybackQuery` 含 `metadata_id`/`number`/`title`/`actors`/…/`source_urls`/`external_ids` 与 **`files: tuple[PlaybackMediaFile, ...]`**(该条目已索引文件快照: `id`/`path`/`size`/`content_type`/`mosaic`/`has_subtitle`/`definition`/`library_id`/`library_path`).

输出目标三种:

- `file`: 主机自行打开; **路径必须是该条目已索引文件之一**(两侧 `resolve()` 后逐条比较, 符号链接等价形式视为同一文件), 不在索引中或文件已消失 → 502.
- `upstream`: 上游 URL + 仅服务端请求头, 主机反向代理单段 Range.
- `hls`: 插件给 locator(`load_playlist` / `locate`), 主机改写清单 URI 并代理分片与密钥; 可用现成的 `RelativeHlsLocator`.

三种目标都可带 `cache_ttl`(上限 `RESOLVE_TTL_MAX_SECONDS`).

### 1.4 插件能用的主机服务 (`PluginContext`)

`source_id` / `http_client`(`HttpClient`) / `web_client`(`WebClient`) / `data_dir`.

- `http_client.get_html`(有拦截页启发式)、`get_json`、`get_text`、`get_bytes`、`post_json`; **必须走共享客户端**, 以继承代理、重试、Host 限速与任务 HTTP 记录【文档】.
- `RateLimiters` 对 `127.0.0.1` / `localhost` 有内置 300/s 限速【源码】`net/http.py::RateLimiters._LOCALHOST_RATE`.
- **代理坑**: `HttpClient.get_json/get_text` 不暴露 `use_proxy`, 底层默认 `True`; `proxy_overrides` 只由 `hot.scraping.site_config` 生成【源码】`app/runtime.py`】, 第三方来源不在其中. 因此**全局 `network.proxy` 非空时, 插件经 `http_client` 访问本机端口也会走代理**, 应改用 `context.web_client.request(..., use_proxy=False)` 强制直连【源码 `net/http.py::WebClient.request` + 【推断】未在真机验证】.
- 三个播放钩子在事件循环上调用, 阻塞 I/O 必须 `asyncio.to_thread`【文档】.

### 1.5 管理类 REST 接口

【源码】`src/amane/api/routes/plugins.py`; 全部挂在 `/api/plugins` 下, 鉴权与其它接口相同:

| 方法 + 路径 | 作用 | 状态码 |
|------|------|------|
| `GET /api/plugins` | 插件目录(含 `failures`) | 200 |
| `POST /api/plugins` | 安装(zip 上传或服务器路径) | 201 |
| `POST /api/plugins/reload` | 热重扫目录(注册顺序须早于 `/{plugin_id}`) | 200 |
| `GET /api/plugins/{plugin_id}` | 单个插件 + 配置 schema | 200 |
| `PATCH /api/plugins/{plugin_id}` | 改 `enabled` / `config` | 200 |
| `DELETE /api/plugins/{plugin_id}` | 卸载(保留运行时数据) | 204 |

播放侧(插件产出被消费的地方): `GET /api/playback/sources`、`/{source_id}/{metadata_id}/streams`、`/streams/{key}`(码流)、`index.m3u8` 系列、`/subtitles/{track_id}`【源码】`api/routes/playback.py`.

安装/卸载/启停/重扫共用一把 rebuild 锁, 不重启进程: 先处理源码树, 再 `AppRuntime._rebuild()` 建新网络栈/Factory/Worker 并退役旧 Worker, 已认领任务用旧插件对象跑到终态【文档】.

### 1.6 明确不开放

自定义任务类型、数据库迁移、API 路由、React 页面、演员来源、进程隔离、插件自带第三方依赖; 需要其它能力应先扩 API 版本与权限边界【文档 `docs/dev/plugins.md`】.

---

## 二、能否通过 Everything 获取来源数据

"来源数据"在本项目里有三种落点, 结论完全不同.

### 2.1 意图 A: 用 Everything 当「影片元数据来源」—— 只能做本地元数据, 不能做发现

- **能拿到的**: `SearchQuery.file_path`(该 MediaFile 的绝对路径). 插件可据此在**同目录/邻近目录**找 `.nfo`、封面、本地 JSON 库, 解析后回填 `MediaMetadata`【源码 `crawlers/models.py::SearchQuery`】.
- **拿不到的**: 全局文件视图. `fetch` 是"给一个番号 → 回一份元数据"的被动接口, 主机不会给它"扫一遍磁盘把新片加进来"的机会; 插件不接触 Repository, 无法 `register_media_file` 入库【文档 `docs/dev/plugins.md` 插件边界】.
- **Everything 的接入手段(技术可行, 契约代价递减)**:
  1. **Everything HTTP 服务**(`http://localhost:8080`, 需 Everything 运行并开启 HTTP server): 用 `context.web_client.request(..., use_proxy=False)` 取结果(JSON 需 `&j=1`, 否则默认 XML). 最干净 —— 不引入依赖, 走标准 HTTP.
  2. **`es.exe` 命令行**: `subprocess` 是标准库, 可用; 但必须 `asyncio.to_thread`, 否则阻塞整个事件循环.
  3. **Everything IPC(WM_COPYDATA)**: `ctypes` 是标准库, 理论可行; 但需自建窗口消息循环、跨进程 Unicode 内存读写, 属于在进程内做高危操作, **不推荐**【推断, 未实测】.
- **结论**: 可以做"本地 `.nfo` / 本地索引"这类来源插件, 但**不是**文件发现. Everything 在这里只是"比 glob 快地定位同番号周边文件", 收益有限(同目录 `Path.glob` 已经足够快).

### 2.2 意图 B: 用 Everything 当「播放源」—— 库外文件播不了

- `probe` 可以列任意候选(包括 Everything 搜出来的), 但 `resolve` 返回 `file` 目标时, **主机只接受该条目已索引文件之一**, 库外命中的路径一律 502【源码 `plugins/api.py::FilePlaybackTarget` + 【文档 `docs/dev/plugins.md`】.
- 因此 "Everything 搜到同名影片 → 直接播" **不成立**, 除非该文件已经过 REFRESH/监控入库.
- **唯一合规且被明确鼓励的本地检索口子是 `subtitle`**: 文档写明"需要读取本机字幕文件时由插件自行读取并转换为 WebVTT". 用 Everything 在这个钩子里找外挂字幕(`.srt`/`.ass`/`.vtt`)完全在契约内, 只需 `asyncio.to_thread` 包裹阻塞调用.

### 2.3 意图 C: 用 Everything 做「文件发现 / 入库」—— 插件做不到, 必须改主机

当前发现通道只有两条, 且**没有任何插件扩展点**:

| 通道 | 实现 | 触发 |
|------|------|------|
| `LibraryIngest.NATIVE`(默认) | watchdog `Observer` / `PollingObserver` | 该库 schedule 到进程内唯一 FileWatcher【源码】`enums.py::LibraryIngest` + 【文档 `docs/dev/watcher.md`】 |
| `LibraryIngest.CLOUDDRIVE` | `POST /api/webhooks/clouddrive` | 不 schedule Observer, 避免 FUSE 上漏事件与双通道重复入库 |

全量遍历(REFRESH / 首刷 / 清理清单共用一趟)在 `library/cleanup/inventory.py::scan_inventory` → `_walk`(`os.scandir` 递归); 单目录补扫在 `handlers/_common.py::scan_library`(`scan_dir.glob("**/*")`, 经 `@in_thread` 跑线程池). 这两个函数是**纯磁盘遍历**, 正是 Everything 的替换点, 但都属于主机侧, 插件 API 不覆盖.

### 2.4 可行性矩阵

| 目标 | 走插件 | 走主机改造 | 收益 | 判定 |
|------|--------|-----------|------|------|
| 本地 `.nfo` / 本地元数据作为刮削来源 | ✅ `FilmSourcePlugin.fetch` 读 `query.file_path` | 不需要 | 中 | **可行, 契约内** |
| 用 Everything 找外挂字幕并挂到播放 | ✅ `PlaybackProvider.subtitle` | 不需要 | 中 | **可行, 契约内(唯一允许插件读盘的口子)** |
| 用 Everything 播库外文件 | ❌ `file` 目标受索引约束 | 需要先让它入库 | 低 | **不可行** |
| 用 Everything 替代整库遍历做发现/入库 | ❌ 无扩展点 | 新增 ingest 通道 + 替换 `scan_inventory` | 高(大库首刷/REFRESH 提速) | **要改主机** |
| 用 Everything 做实时新增监控 | ❌ 无扩展点 | 新增第三条 ingest 通道(Everything IPC 轮询/HTTP 查询) | 高 | **要改主机** |

### 2.5 若要落地主机侧改动

最小改动面(不新增依赖的方案, 用 Everything HTTP server):

1. `src/amane/enums.py::LibraryIngest` 增加枚举值(如 `EVERYTHING`), 语义同 `CLOUDDRIVE`: 不 schedule Observer【源码】.
2. 新增查询适配层: 把 `Library.path` + `patterns` + `media_extensions` 翻译成 Everything 查询串(HTTP `/?s=<path> <ext>&j=1&path_column=1`), 复用 `WebClient.request(..., use_proxy=False)`(本机直连, 命中 `localhost` 300/s 限速).
3. 替换遍历入口: `library/cleanup/inventory.py::scan_inventory`(整库/REFRESH, 与清理清单共用一趟遍历)与 `handlers/_common.py::scan_library`(目录补扫)改为可切换的"枚举器"抽象, 输出仍是 `LibraryHit(path, kind)`, 后续 `LibraryScan.classify` 与 `register_media_file` 不变.
4. 配置与校验: `config/manager.py` 增加 Everything 段(服务地址、超时、是否启用), `api/routes/libraries.py` 的库 CRUD 校验随之扩展.
5. 降级: 服务不可达时回退磁盘遍历并记日志(参考 `scheduler` 中 Observer 启动失败只停 FileWatcher 的处理方式【文档 `watcher.md`】).
6. 同步更新 `docs/dev/watcher.md`(通道表)与 `docs/user/libraries.md`.

风险与约束: Everything 仅 Windows(主机侧可加依赖, 不受插件"禁第三方依赖"限制, 但 Docker/Linux 部署必须有回退); Everything 索引需包含目标卷且 NTFS 卷才能用 USN 索引; 结果路径需过 `safe_dirs` 与库根前缀校验, 不能因为查询快就放松边界.

---

## 三、给插件作者的现实建议

- 真想让 Amane "看到" Everything 搜出来的片子, 正确顺序是 **先入库**(REFRESH / 监控), 再由刮削管线处理; 插件不负责入库.
- 插件里访问本机服务一律显式 `use_proxy=False`, 否则全局代理一开就全挂.
- 任何阻塞调用(含 `es.exe`、`ctypes` IPC)都必须 `asyncio.to_thread`; 播放三钩子尤其严格, 阻塞会让其它来源探测一起超时.
- 不要依赖第三方包(含 `everything-sdk`、`pywin32`): 安装期导入失败会以 422 报出, 已放好的目录在发现期进入 `failures`.
