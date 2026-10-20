# Everything 接入 Amane: 不走插件的主机侧改造方案

> 证据标记: 【源码】= 本仓库读到的定义/实现; 【文档】= `docs/dev/` 契约; 【推断】= 推理, 未验证.
> 本文只给方案与骨架, **尚未改动任何源码**. 前一篇「插件接口与 Everything 接入可行性」见 `docs/analysis-plugin-and-everything.md`.

## 本机实测(2026-10-19, 决定方案排序)

| 项 | 结果 | 证据 |
|------|------|------|
| Everything 已安装并运行 | ✅ Everything 1.5a + 1.4 进程均在(`Everything.exe` ×3、`Everything64.exe`), 安装目录 `C:\Program Files\Everything` | 【实测】`tasklist` |
| **HTTP server 是否可用** | ❌ **未启用**: `Everything-1.5a.ini` 中没有任何 `http_server_*` 键 | 【实测】grep ini |
| **8080 端口** | ⚠️ **已被占用且不是 Everything**: `http://localhost:8080/` 返回 200, 但正文是「aTrust APP 扫码登录演示」(深信服零信任客户端), 而 Everything HTTP server 默认端口正是 8080 | 【实测】curl |
| SDK DLL | ✅ `Everything3_x64.dll`(154 KB)导出 171 个 `Everything3_*` 函数(`Everything3_ConnectW` / `Everything3_FindFirstFileW` / `Everything3_GetFolderSizeFromFilenameW` / `Everything3_GetIPCPipeVersion` …) | 【实测】PE 导出表解析 |
| ctypes 裸调 SDK | ❌ 本机两种调用约定(`CDLL` / `WinDLL`)下 `Everything3_GetIPCPipeVersion()` 均 access violation | 【实测】python ctypes |

由此定下三条硬约束:

1. **连通性不能只看状态码**: 8080 返回 200 却是 aTrust 的登录页. 客户端必须**校验返回体特征**(例如按所用 Everything 版本核对 JSON 结构/字段), 否则"服务可用"是假阳性, 后面所有查询都会拿到 HTML 并静默失败.
2. **HTTP server 必须换端口**: 默认 8080 与 aTrust 冲突, 启用时改成高位端口并在配置里显式声明, 不要依赖默认值.
3. **SDK DLL 不要裸调**: 本机实测崩溃, 需按官方 SDK 头文件核对签名与初始化顺序; 若你已有能跑通的封装(例如 JAV 字幕流水线里那套 Everything 1.5 SDK 调用), 直接把它作为客户端实现, 对上本文的 `MediaEnumerator` 契约即可.

---

## 结论与推荐

| 方案 | 改造量 | 解决什么 | 代价 | 建议 |
|------|--------|---------|------|------|
| **0. `.strm` / 符号链接桥接** | **零改造** | Everything 负责"找", Amane 负责"管" | 多一层目录与生成脚本; 删除/移动需脚本同步 | **先做这个验证收益** |
| **1. 媒体枚举器抽象** | 小(1 新模块 + 2 调用点) | REFRESH / 首刷 / 目录补扫提速 | 面板清理清单丢"空目录 / 孤儿目录"条目 | **确认收益后的正式方案** |
| **2. `ingest=everything` 通道** | 中(枚举 + 配置 + 调度) | 替代 watchdog 做增量发现(大库/网络盘/NAS) | 轮询有延迟; 需处理删除检测 | 有第 1 步之后再上 |
| **3. Everything IPC(SDK) 实时** | 大(新依赖 + 平台绑定) | 真正实时 | 绑定 Windows + DLL; 打包与 Docker 部署复杂 | 不建议 |

共同前提: Everything 索引只覆盖本机 NTFS 卷; CloudDrive / FUSE / 网络挂载不在索引里, 这类库必须回退磁盘遍历【推断, 需按实际部署核实】.

---

## 一、现状落点(改动前务必看清)

| 位置 | 职责 | 是否 Everything 可替换 |
|------|------|----------------------|
| `src/amane/library/cleanup/inventory.py::scan_inventory` → `_walk` | **一趟遍历同时产出**: `state.media`(媒体命中→入库)、`state.entries`(无效文件 + **空目录** + **只含附属文件的孤儿目录**)、`state.dirs`(DirCoverage 覆盖表) | **只能替换"文件枚举"部分**. 目录类条目依赖逐层递归(`_DirResult.has_content` / `undeletable` / `orphan_in_subtree`), Everything 只有文件列表, 拿不到"某目录下的子项构成" |
| `src/amane/handlers/_common.py::scan_library` | 目录补扫: `scan_dir.glob("**/*")` + `LibraryScan.classify`, `@in_thread` | ✅ 可整体替换(只产 `LibraryHit`) |
| `src/amane/handlers/refresh.py::RefreshHandler.handle` | REFRESH: `scan_inventory(collect_media=True)` → 新增入库 / 缺失删索引 → 扇出 SCRAPE; 整库范围时把清单写入 `inventory_store` | 消费方, 只需换枚举入口 |
| `src/amane/handlers/scan_invalid.py` | SCAN_INVALID 巡检, 同样调 `scan_inventory` 并带 `orphan_scan` | 保持磁盘遍历(它要的就是清单) |
| `src/amane/scheduler/service.py::WatcherService` | `ingest=native` → watchdog `FileWatcher`; `ingest=clouddrive` → webhook 路由表; `_on_file_found` 是唯一的单文件入库点 | 方案 2 的落点 |
| `src/amane/enums.py::LibraryIngest` | `NATIVE` / `CLOUDDRIVE` | 方案 2 加枚举值 |

**最关键的一条**: 面板清理清单来自 REFRESH 整库扫描(`refresh.py` 里 `inventory.scope_path is None` 时才 `put`). 一旦 REFRESH 改走 Everything, 清单里的**目录类条目会消失**, 只剩无效文件条目. 这是方案 1 必须显式接受的取舍(或让清单改由独立巡检任务产出).

---

## 二、方案 0: 零改造桥接(推荐先试)

Amane 的媒体扩展名里**已经包含 `.strm`**【源码 `config/manager.py::WatcherConfig.media_extensions`: `.mp4 .mkv .avi .wmv .flv .mov .ts .iso .strm`】, 且播放侧明确"指向库外的符号链接照常可播"【文档 `docs/dev/plugins.md` 播放源 `file` 段】. 因此:

1. 建一个扁平目录(如 `D:\amane-index`)作为 Amane 的库根, `ingest=native`、`automation=scrape`.
2. 外部脚本用 Everything SDK/HTTP 查出目标影片, 在库根生成 `.strm`(内容是绝对路径)或符号链接.
3. Amane 用原生 watchdog 捕获 `FileCreated` → `_on_file_found` → 入库 → 自动 SCRAPE.

优点: 不动 Amane 一行代码, 立刻可用, 失败可随时停. 缺点: 目录会随库规模膨胀; 删除/重命名要脚本自行同步(否则索引里留下悬空条目, 靠 REFRESH 的 remove 分支清理).

---

## 三、方案 1: 媒体枚举器抽象(正式改造)

### 3.1 新增模块 `src/amane/library/enumerators.py`

```python
"""文件枚举: 同一份契约, 磁盘遍历与 Everything 查询两种实现."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .scan import LibraryHit, LibraryScan


class MediaEnumerator(Protocol):
    async def enumerate(self, scope_dir: Path, *, recursive: bool, scan: LibraryScan) -> list[LibraryHit]: ...


class DiskEnumerator:
    """现状行为: scandir / glob 逐层遍历, 线程池执行."""

    async def enumerate(self, scope_dir: Path, *, recursive: bool, scan: LibraryScan) -> list[LibraryHit]:
        from ..handlers._common import scan_library  # 避免循环导入, 实装时应把 scan_library 下沉到本模块

        return await scan_library(scope_dir, recursive=recursive, scan=scan)


class EverythingEnumerator:
    """按库根查询 Everything, 只产 MEDIA / UNWANTED 命中.

    拿不到目录构成, 因此 has_companion_subtitle 恒 False(与云盘事件通道同口径, 见 LibraryHit 注释).
    """

    def __init__(self, client: EverythingClient) -> None: ...

    async def enumerate(self, scope_dir: Path, *, recursive: bool, scan: LibraryScan) -> list[LibraryHit]:
        rows = await self._client.query(scope_dir, recursive=recursive)
        hits: list[LibraryHit] = []
        for path, size in rows:
            kind = scan.classify(path)
            if kind is None:
                continue
            hits.append(LibraryHit(path, kind))
        return hits
```

### 3.2 Everything 客户端要点(`src/amane/library/everything.py`)

- **用现成的 `WebClient`, 但必须 `use_proxy=False`**: 全局 `network.proxy` 非空时, 默认 `use_proxy=True` 会把本机请求也送进代理【源码 `net/http.py::WebClient.request`】; `RateLimiters` 对 `localhost` 有内置 300/s, 不会误限速【源码 `net/http.py::RateLimiters._LOCALHOST_RATE`】.
- **`WebClient.request` 没有 `params` 参数**, 查询串要自己 `urllib.parse.urlencode` 拼到 URL 上【源码 `net/http.py::WebClient.request` 签名】.
- **返回体必须校验**: 端口可能被别的程序占着并返回 200(本机 8080 就是 aTrust 登录页), 只判状态码会拿到 HTML 后静默失败. 客户端要按所用 Everything 版本核对返回结构, 不匹配即视为"服务不可用"并降级.
- 查询三要素: 路径前缀限定 + 扩展名集合 + `recursive`(非递归时限定为直接子层). **具体参数名/取值按所用 Everything 版本核实** —— 先在 Everything 里手动开启 HTTP server(换端口), 用 `curl --noproxy '*'` 打一次确认返回结构与列开关, 再固化到代码【推断 — 本机 HTTP server 未启用, 未实测到查询返回】.
- 需要 `size` 列才能判 `UnwantedKind.UNDERSIZED`(`min_file_size`); 拿不到 size 时对过小文件会漏判 → 宁可让它们进库, 也不要误判成无效文件删除.
- 阻塞/超时: 客户端调用在事件循环上, 必须设 `timeout`(配置化, 建议 10s)并在失败时抛可分类异常, 由调用方降级.
- **降级**: 查询失败/超时 → 回退 `DiskEnumerator` 并记日志(参考 `scheduler/service.py` 中 Observer 启动失败只停 FileWatcher、不阻断其它通道的处理方式).

### 3.3 接入点

1. `handlers/refresh.py::RefreshHandler.handle`: `want_add` 分支不再强制走 `scan_inventory`; 当库启用 Everything 且**不需要写面板清单**时, 改用枚举器产出 `media_hits`, 后续 `register_media_file` / `_sync_external_subtitle` / `remove` 分支逻辑不变.
2. `handlers/_common.py::scan_library`(被 `scheduler/service.py::_scan_cloud_dir` 等调用): 换枚举器即可.
3. **保留** `scan_invalid.py` 与任何带 `orphan_scan` 的调用走 `scan_inventory`(磁盘遍历), 清理清单的目录类条目不丢.
4. 依赖注入: 枚举器在 `app/bootstrap.py` 构造(`WatcherService` 就在那里 new 出来的, 【源码 `bootstrap.py:207`】), 经 `AppRuntime` 传给 handler; 配置段挂在 `WatcherConfig` 旁边(新增 `EverythingConfig`: `enabled` / `base_url` / `timeout` / `fallback_on_error`).

### 3.4 取舍要写在代码注释里

- 面板清单在启用 Everything 的库上**不含目录类条目**.
- `LibraryHit.has_companion_subtitle` 恒假 → 外挂字幕标记靠 `register_media_file` 时的同目录检查补齐, 存量行仍由 REFRESH 的 `_sync_external_subtitle` 处理, 但 Everything 路径下拿不到该信息 → 该标记可能长期为假. 若在意, 让 Everything 额外查一次字幕扩展名并按目录聚合【推断: 需一次额外查询, 代价可接受】.

---

## 四、方案 2: `ingest=everything` 增量通道

落点(在方案 1 之后做):

1. `src/amane/enums.py::LibraryIngest` 加 `EVERYTHING = "everything"`, 语义同 `CLOUDDRIVE`: **不 schedule Observer**, 避免与查询双通道重复入库【文档 `docs/dev/watcher.md` 对 clouddrive 的同一条理由】.
2. `scheduler/service.py::WatcherService`: `start()` / `sync_library()` 按新枚举分流, 登记 `EverythingRoute`(复用 `CloudDriveRoute` 的字段形状: `library_id` / `local_path` / `recursive` / `scan`); 新增周期性任务(`check_interval` 已有, 【源码 `service.py::_CHECK_INTERVAL`】)按 `date-modified` 增量查询, 命中后走**现成的 `_on_file_found`**(单文件入库 + 解析番号 + 扇出 SCRAPE 全在里面, 不要另写一套).
3. 删除检测: Everything 查不到不等于文件没了(可能只是索引未更新) → 删除只在 REFRESH 的 remove 分支处理, 实时通道**只做新增**, 保持与 clouddrive 通道一致的保守口径.
4. `api/routes/libraries.py` 与 `db/models.py::Library` 的校验随之扩展(参考 `cloud_path` 的必填校验写法).
5. 同步更新 `docs/dev/watcher.md` 的通道表.

---

## 五、验证清单(动手前先能验)

1. **先验证服务可达**: `curl --noproxy '*' "http://localhost:8080/?s=<库路径>&j=1"` 拿到返回, 确认 JSON 结构与列开关(本机代理环境下 `curl` 必须 `--noproxy`).
2. **集合对拍**: 同一个库分别用 `DiskEnumerator` 与 `EverythingEnumerator` 枚举, 对 `LibraryHit.path` 集合做 diff(去重 + NFC 归一化, `refresh.py` 里用 `nfc_path` 做 key, 口径要一致). 允许差异只有: 未纳入 Everything 索引的卷、索引滞后造成的新文件缺失.
3. **表测试**: 按 `docs/dev/testing.md` 的表测试写法, 覆盖路径前缀限定、非递归、黑名单/预告片/过小文件、查询失败降级、代理开启仍走直连.
4. **实机**: 大库 REFRESH 计时对比; 确认新增/删除计数与旧实现一致.

---

## 六、不推荐的做法

- **整体替换 `scan_inventory`**: 会静默丢掉空目录与孤儿目录条目, 清理面板直接少一半信息, 且这类回归不会报错, 只会"看起来清干净了".
- **在插件里做**: 插件不接触 Repository, 无法入库(见前一篇).
- **让插件/主机对 Everything 做实时转码或搬运**: 与本项目"媒体正文由主机打开或代理"的边界冲突.
