# 本机播放插件

> 本文设计「把播放交给本机关联程序」的插件化方案: 新增插件能力 `local_playback`, 插件只声明"用哪个程序、播哪些文件", 由主机执行拉起动作; 前端在封面右上角给出入口。
> 现有的网页内播放源 (插件声明 `file` / `upstream` / `hls` 目标) 不变, 见 [plugins.md](dev/plugins.md). 播放目标的逐字段契约在 `src/amane/plugins/api.py` 的 docstring 里.

## 1. 目标与非目标

### 目标

1. 网页内播不了的格式 (mkv / avi / wmv / iso / rmvb) 能一键交给本机播放器, 播放器读原文件, 不经 HTTP, 因而不受浏览器解码能力限制.
2. 走插件: 装了就出现入口, 不装则界面零痕迹. 与播放源插件同一套安装 / 启停 / 配置 / 重建流程.
3. 多文件条目给播放器一份**内存级播放列表**, 不落地临时文件.
4. 跟随系统默认播放器, 换播放器不用改配置.

### 非目标

- **不做网页内播放**: 那是 `playback` 能力的职责, 本文不碰.
- **不做转码**: 主机不为播放做任何 codec 处理.
- **不解决"访问者本机"**: 拉起的是**运行主机进程那台机器**上的程序. 浏览器与服务不在同一台时, 播放器会开在服务器上, 这不是可用形态. 跨机器必须另做桌面端或自定义协议, 不在本文范围.
- **不追求脱离桌面会话可用**: 装成 Windows 服务 / 计划任务 (session 0) 时调用不报错但窗口不出现, 见 §8.

## 2. 为什么必须扩展宿主

现有插件契约里, 播放源只能返回三种目标, **全部由主机翻译成 HTTP 响应**:

| 目标 | 主机行为 | 能否触发本机程序 |
|---|---|---|
| `FilePlaybackTarget` | 主机读文件, 按 Range 输出字节流 | ❌ |
| `UpstreamPlaybackTarget` | 主机反向代理上游 URL | ❌ |
| `HlsPlaybackTarget` | 主机代理清单与分片 | ❌ |

`PlaybackProvider` 的三个方法 (`probe` / `resolve` / `subtitle`) 都是"给出流", 没有任何一条路径能让插件产生副作用. 插件确实能拿到 `PlaybackQuery.files` 里的路径, 但拿到路径 ≠ 能执行.

理论上插件可以在 `resolve` 里直接 `os.startfile`, 但这会把副作用挂到 GET 上: 列表刷新、失败重试、页面重进都会各拉起一个播放器, 而 `resolve` 的返回类型里没有错误字段, 失败原因传不回来. 因此**执行必须由宿主承担, 插件只做声明** —— 这是本文全部设计的前提.

## 3. 总体流程

```
 浏览器 (与主机同机)                    主机进程                        播放器
 ─────────────────                     ────────                        ──────
 ① 封面右上角图标
    GET /api/local-playback/status ──▶ 插件 status() ──▶ {available, player_label}
                                      (结果缓存, 不碰磁盘之外的东西)

 ② 点击
    POST /api/local-playback/
         {metadata_id}/launch ──────▶ 查索引取该条目文件
                                      插件 plan(query) ──▶ {exe, argv, files}
                                      校验 files ⊆ 索引
                                      ┌─ 1 个文件 ─▶ Popen([exe, *argv, path]) ──▶ 直接播
                                      └─ N 个文件 ─▶ Popen([exe, *argv, m3u_url]) ─┐
                                                                                 │
 ③ 播放器自己取列表                                              GET /localplay/{token}.m3u
    (不带 Authorization / cookie)  ◀────────────────────────────────────────────┘
                                      内存生成 M3U (file:/// URI)

 ④ 播放器逐项播放本地文件 (不经主机)
```

要点:

- 单文件**不经过** HTTP: 直接进入命令行的就是原文件路径, 少一跳, 也不依赖服务可达.
- 多文件才走 M3U. 原因是命令行长度有上限 (Windows 32767 字符), 而列表由服务端按需生成, 长度不受限.
- M3U 里写 `file:///` URI, 不写裸路径 —— 见 §6.2, 这是实测结论.

## 4. 契约增量

### 4.1 新能力

```python
class SourceCapability(StrEnum):
    ...
    LOCAL_PLAYBACK = "local_playback"
```

与 `PLAYBACK` 并列, 但不是同一种东西: `PLAYBACK` 说"我能给出流", `LOCAL_PLAYBACK` 说"我知道怎么在本机打开". 一个插件可以只声明后者 (本文的典型形态), 也可以两者都声明.

### 4.2 新基类

```python
class LocalPlaybackPlugin(_ConfiguredPlugin):
    """第三方本机播放插件. 目录布局与其它 drop-in 相同."""

    @abstractmethod
    def build_local_playback(self, context: PluginContext, config: BaseModel) -> LocalPlaybackProvider: ...
```

独立基类而非复用 `PlaybackPlugin`: 后者的 `build_playback` 返回 `PlaybackProvider`, 语义是"给流". 方法名分开也让同一类可以同时继承三种基类而不冲突 —— 与 `build` / `build_playback` 分家的理由相同.

发现流程要跟着改的地方: `plugins/manager.py::_instantiate_dropin` 的类型判定、`_require_matching_capabilities` 的一致性校验、`packaging.py` 的安装期校验错误文案 (现在硬编码了"必须是 FilmSourcePlugin 或 PlaybackPlugin"), `_build_descriptors` 里三类插件的遍历.

### 4.3 Provider 协议

```python
class LocalPlaybackStatus(BaseModel):
    """与具体条目无关: 本机播放现在能不能用."""

    available: bool
    player_label: str | None = None  # "VLC", 给前端拼文案
    reason: str | None = None  # 不可用原因, 已有 i18n 键时优先用 key
    multi_file: bool = False  # 是否支持一次入列多个文件


class LocalPlaybackPlan(BaseModel):
    """针对一个条目的执行计划. 宿主只执行, 不再二次判断."""

    executable: str | None = None  # None = 交回系统关联程序
    argv: tuple[str, ...] = ()  # 不含文件参数
    label: str
    files: tuple[str, ...] = ()  # 顺序即播放顺序; 空 = 该条目无可播文件


class LocalPlaybackProvider(ABC):
    @abstractmethod
    async def status(self) -> LocalPlaybackStatus:
        """本机播放可用性. 前端在详情页与卡片上判断是否渲染入口."""

    @abstractmethod
    async def plan(self, query: PlaybackQuery) -> LocalPlaybackPlan:
        """该条目用哪个程序打开、按什么顺序打开哪些文件."""
```

`plan` 只接收 `query.files` 里已有的路径 —— 插件不能凭空造路径, 宿主会再校验一遍 (§7.2).

### 4.4 契约里不开放的东西

| 不开放 | 原因 |
|---|---|
| 任意要执行的文件名 | 请求体里不能出现可执行路径. `executable` 只来自插件配置或注册表, 见 §5 |
| 任意要播放的路径 | `plan.files` 里的每一项都必须在 `metadata_id` 的索引内 |
| 附加环境变量 / 工作目录 | `Popen` 的 `env` / `cwd` 由宿主固定, 不接受插件传入 |
| 不经宿主的自执行 | 插件不提供"宿主回调", 拉起动统一在宿主 |

### 4.5 示例插件

```python
# {data_dir}/plugins/sources/nas.localplay/plugin.py
from pydantic import BaseModel

from amane.plugin import (
    EmptyPluginConfig,
    LocalPlaybackPlan,
    LocalPlaybackPlugin,
    LocalPlaybackProvider,
    LocalPlaybackStatus,
    PlaybackQuery,
    PluginContext,
    SourceCapability,
    SourceDescriptor,
)

VIDEO = frozenset({".mp4", ".mkv", ".avi", ".wmv", ".mov", ".ts", ".m2ts", ".rmvb", ".iso", ".mpg", ".mpeg"})


class Provider(LocalPlaybackProvider):
    def __init__(self, player: str | None) -> None:
        self._player = player

    async def status(self) -> LocalPlaybackStatus:
        if self._player is None:
            return LocalPlaybackStatus(available=False, reason="未找到可用的本机播放器")
        return LocalPlaybackStatus(available=True, player_label="VLC", multi_file=True)

    async def plan(self, query: PlaybackQuery) -> LocalPlaybackPlan:
        files = tuple(f.path for f in query.files if any(f.path.lower().endswith(e) for e in VIDEO))
        return LocalPlaybackPlan(executable=self._player, argv=(), label="VLC", files=files)


class Plugin(LocalPlaybackPlugin):
    @classmethod
    def descriptor(cls) -> SourceDescriptor:
        return SourceDescriptor(
            id="nas.localplay",
            name="本机播放器",
            version="1.0.0",
            capabilities=frozenset({SourceCapability.LOCAL_PLAYBACK}),
        )

    def build_local_playback(self, context: PluginContext, config: BaseModel) -> LocalPlaybackProvider:
        return Provider(resolve_player_executable())
```

`multi_file: false` 的插件只能单文件播放: 宿主收到多文件时会退回"只开第一个"并附带说明, 不会静默丢文件.

## 5. 播放器解析

### 5.1 为什么不直接 `os.startfile`

`os.startfile(path)` 只能传**一个路径**, 既不能传参数也不能传管道, 多文件列表与"播放器跳转到上次进度"这类参数都做不到. 要拿到这些能力, 必须知道 exe.

### 5.2 从注册表取出系统默认程序

"跟随系统默认"与"拿到 exe"不必二选一 —— Windows 的关联信息里就存着命令行:

```
.mp4
  └ HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.mp4\UserChoice
      ProgId = "VLC.mp4"
        └ HKCU\Software\Classes\VLC.mp4\shell\open\command
            (默认) = "C:\Program Files\VideoLAN\VLC\vlc.exe" --no-crashdump --started-from-file "%1"
```

本机实测值即上面这条. 解析步骤:

1. 取 `UserChoice\ProgId`; 取不到则退到 `HKCR\.<ext>` 的默认值 (老式关联 / 机器级关联).
2. `HKCU\Software\Classes\<ProgId>\shell\open\command` → 读不到再试 `HKCR\<ProgId>\...`.
3. 按 Windows 命令行规则拆成 `argv[0]` = exe, 其余为固定参数.
4. 把 `%1` / `%L` / `%*` 替换为实际文件参数 (`%1` 是单文件占位, `%*` 是全部参数); 展开 `%ENV%`.

第 3、4 步不能省: 上面 VLC 的命令里有 `--started-from-file`, 直接丢掉会改变播放器行为; PotPlayer / MPC 的命令模板里也有各自的开关.

### 5.3 三类默认程序

| 默认程序类型 | 能拿到 exe | 能力 | 降级行为 |
|---|---|---|---|
| 传统 Win32 (VLC / PotPlayer / MPC-HC / mpv) | ✅ | 完整 | `Popen`, 多文件走 M3U |
| UWP / AppX (如 `.m3u` 默认给 Windows Media Player, ProgId 形如 `AppXqj98...`) | ❌ 无 exe 路径 | 仅单文件 | `os.startfile` 打开第一个文件, `multi_file=false` |
| 未关联 | ❌ | 无 | `status().available=false`, 前端不渲染图标 |

`local_player` 配置项可以覆盖: 手填 exe 路径时跳过注册表解析, `argv` 为空. 适用于"默认程序是 AppX 但我想指定 VLC"的场景.

### 5.4 单文件 vs 多文件

| 文件数 | 有 exe | 执行 |
|---|---|---|
| 1 | ✅ | `Popen([exe, *argv, path])` |
| 1 | ❌ | `os.startfile(path)` |
| ≥2 | ✅ | `Popen([exe, *argv, m3u_url])`, 列表由 `/localplay/{token}.m3u` 生成 |
| ≥2 | ❌ | `startfile(files[0])` + 响应里 `truncated=true`, 前端提示"当前播放器不支持列表" |

`Popen` 一律**不等退出**: 播放器常驻, 主进程只负责拉起. 拉起动作经 `asyncio.to_thread` 提交, 不阻塞事件循环.

## 6. M3U 端点

### 6.1 路径与鉴权

```
GET /localplay/{token}.m3u
```

**不放在 `/api/` 下**, 这是刻意的:

- `TokenAuthMiddleware` 只对 `path.startswith("/api/")` 校验 Bearer / cookie (见 `api/middleware.py`). 播放器取列表时两者都不带, 放在 `/api/` 下必然 401, 那就得往 `_TOKEN_EXEMPT` 里加路径或改鉴权逻辑 —— 不必.
- 独立前缀让"免鉴权"这件事在代码里一眼可见, 而不是藏在豁免名单里.

**但有一处必须同步改**: `api/spa.py` 的 `_SPAFallbackMiddleware` 会把所有未匹配的 GET 回退成 `index.html`, 且它在路由之前执行. 不把 `"/localplay/"` 加进 `_RESERVED_PREFIXES`, 播放器拿到的就是一张 HTML 页, 解析失败且没有可读错误. 这是本条链路最容易漏的一处.

### 6.2 内容格式

```
#EXTM3U
file:///Z:/115/AV2_Output/7756/7756.mp4
file:///Z:/115/AV2_Output/7756/7756-CD2.mp4
```

必须写 `file:///` URI, 不能写裸路径 —— 实测 (VLC 3.0.24, 抓 `-vv` 的 playlist 日志):

| 写法 | 日志 | 结论 |
|---|---|---|
| `file:///D:/...` (绝对 URI) | `rebuild done - 3 items` | ✅ |
| `D:/...` (裸绝对路径) | `1 items`, 未展开 | ❌ |
| `clip1.mp4` (裸相对路径) | `cannot open file \\0\clip1.mp4` | ❌ |
| 直接多文件参数 `vlc a b c` | `rebuild done - 3 items` | ✅ (单文件路径用这个, 见 §5.4) |

同一份 `file:///` 内容走 stdin 与走 HTTP 都成立, 本文选 HTTP 是因为单文件场景不需要它, 而多文件场景下 URL 让命令行长度与文件数解耦.

`#EXTINF` 行不是必需的 (VLC 会自己从文件名取标题), 加不加待实测, 见 §11.

### 6.3 不设过期

用户明确要求不设过期时间, 因此 `token` 里不含时间戳, 只绑定 `metadata_id`:

```
token = HMAC-SHA256(key, str(metadata_id))[:32]
```

- `key` 持久化在 `{data_dir}/localplay.secret` (32 字节随机, 文件权限 0600, 首次使用时生成). **必须持久化**: 每次启动随机生成的话, 重启后旧链接会全部失效, 等于隐式过期.
- 不含时间戳的后果: 链接永久有效. 拿到链接的人可以无限次取到该条目的文件路径列表. 缓解措施见 §8.
- 用 HMAC 而非明文 `metadata_id` 的原因只有一个: 让地址不可枚举 (条目 id 是自增整数, `/localplay/1.m3u` 这种一眼能猜).

校验失败返回 404 而不是 403 —— 不告诉调用方"这个 id 存在但签名不对".

### 6.4 每次请求重新取数

端点不缓存内容, 每次重算:

1. 校验 `token` → `metadata_id`.
2. 该能力对应的插件是否启用; 未启用 → 404.
3. 查索引取该条目的文件, 按 `plan.files` 的顺序输出; 索引里已不存在的文件跳过.
4. 一个可用文件都没有 → 404.

不在 token 里编码文件清单, 也不把清单存进服务端会话: 库变了就该反映新的文件集, 旧链接不该吐已经不存在的路径.

响应头: `Content-Type: audio/x-mpegurl; charset=utf-8`, `Cache-Control: no-store`.

## 7. 宿主侧改动清单

### 7.1 文件级

| 文件 | 改动 |
|---|---|
| `src/amane/plugins/models.py` | `SourceCapability.LOCAL_PLAYBACK` |
| `src/amane/plugins/api.py` | `LocalPlaybackStatus` / `LocalPlaybackPlan` / `LocalPlaybackProvider` / `LocalPlaybackPlugin`; `InstalledPlugin` 联合类型加一支 |
| `src/amane/plugins/manager.py` | 发现期类型判定、能力一致性校验、`build_local_playback_provider()` |
| `src/amane/plugins/packaging.py` | 安装期类型校验与错误文案 |
| `src/amane/plugin/__init__.py` | 导出新类型 (`amane.plugin` 是插件唯一入口) |
| `src/amane/playback/local_player.py` | **新增**: 注册表解析 + 命令行拆分 + 拉起 |
| `src/amane/playback/playlist.py` | **新增**: M3U 生成 + token 签发/校验 |
| `src/amane/api/routes/local_playback.py` | **新增**: `GET /status`, `POST /{id}/launch`, `GET /localplay/{token}.m3u` |
| `src/amane/api/models/local_playback.py` | **新增**: 响应模型 |
| `src/amane/api/routes/__init__.py` | 挂到既有 API 路由器 (照 `app.py` 现有顺序) |
| `src/amane/api/spa.py` | `_RESERVED_PREFIXES` 加 `/localplay/` |
| `src/amane/config/manager.py` | `ColdSettings.local_playback` (总开关, 默认 `True`) + `local_player` (覆盖 exe) |
| `web/src/components/media/metadata-detail.tsx` | 封面右上角图标 |
| `web/src/components/media/poster-card.tsx` | 卡片角标组里加同一个图标 |
| `web/src/hooks/use-local-playback.ts` | **新增**: `status` query + `launch` mutation |
| `web/src/i18n/locales/{zh-CN,en}/*.json` | 文案与 `plugins.json` 的 `capabilities.local_playback` |
| `docs/dev/plugins.md` | 能力表加一行, 指向本文 |
| `docs/user/configuration.md` | 两个环境变量 |

### 7.2 端点

```
GET  /api/local-playback/status
     200 {available, player_label, multi_file, reason}
     # 不传 metadata_id: 可用性与条目无关, 结果可缓存; 前端两处共用一次请求

POST /api/local-playback/{metadata_id}/launch?file_id=
     200 {path, label, count, m3u_url?, truncated?}
     403 总开关关闭
     404 条目不存在 / 条目无文件 / 文件已不在磁盘
     409 插件未启用, 或插件报不可用 (reason 回传)
     400 file_id 不属于该条目
```

`launch` 显式用 POST: 它有副作用, 不能被预取、不能被重试策略自动重放. 与"GET 里藏副作用"的做法相比, 这是本方案能安全重试的前提.

`file_id` 可选: 不传时由插件 `plan` 决定顺序, 宿主取第一个. 传了则只播这一个 —— 用于详情页文件列表上的"用本机播放器打开这一条".

### 7.3 校验顺序

```
开关 → 插件启用 → 条目存在 → 文件在索引内 → 文件仍在磁盘 → 插件 plan → files ⊆ 索引 → 执行
```

先拒绝后执行, 任何一步失败都不产生进程. 「files ⊆ 索引」这一步不能省: 插件是进程内可信代码, 但这份名单会被写进 M3U 交给外部程序, 一旦插件有 bug 把 `/etc/passwd` 拼进去, 就变成了"任意本地文件路径泄漏给播放器". 两侧 resolve 后逐条比较, 与 `file` 目标的处理同口径.

## 8. 安全

| 面 | 说明 | 处置 |
|---|---|---|
| 永久 M3U 链接 | 不设过期, 拿到链接即可无限次取该条目路径列表 | 仅本机 / 可信内网监听; 反代处限流; 泄漏面仅是路径, 不含文件内容 |
| 服务端执行 | 本质是"让服务器跑一个程序" | `executable` 只来自插件配置与注册表, 不接受请求参数; `argv` 由宿主拼装; 总开关 `AMANE_LOCAL_PLAYBACK=0` 可整体关闭, 关闭后 `status` 直接 `available=false` |
| 插件即代码 | 插件与本机共用解释器, 能执行任意命令 | 与现有插件同一套信任模型: 装之前审源码. 本机播放插件比刮削插件更敏感, 因为它明确要求执行能力 |
| session 0 | 装成服务 / 计划任务时 `Popen` 不报错, 窗口不出现 | 不检测、不阻止; 文档写明. 检测手段 (查 `WTSGetActiveConsoleSessionId`) 列为后续可选 |
| 路径穿越 | M3U 只输出索引内路径 | §7.3 的索引校验; `query.files` 本身已由主机装配 |

## 9. 前端

### 9.1 位置

- **详情页**: 封面容器已是 `position: relative` (`metadata-detail.tsx`), 图标放右上角, 与中央那个"预告片"大播放按钮**并存不冲突**: 中央按钮语义是预告片, 不动.
- **列表卡片**: `poster-card.tsx` 已有右上角角标组 (`<Group pos="absolute" top={6} right={6}>`), 把图标加进这个组, 不另起定位.

图标选 `IconCast` (tabler 里没有 `IconDeviceDesktopPlay`). 备选 `IconDeviceDesktop`. 语义是"把播放交给外部", 与分享图标的观感接近, 满足"类似分享图标"的要求.

### 9.2 与现有播放面板的关系

三者并存, 互不改写:

| 入口 | 语义 | 触发内容 |
|---|---|---|
| 封面中央大按钮 | 预告片 | 网页内 `<video>` 播放 `trailer_url` |
| 播放面板 (PlaybackPanel) | 正片, 网页内播 | 播放源插件的流 |
| 封面右上角图标 | 正片, 本机播 | 关联程序读原文件 |

不把本机播放能力塞进 `GET /api/playback/sources`, 而是单开 `GET /api/local-playback/status`: 后者是"播放源列表", 本机播放不是一种"源" (它不产出任何流). 混进去会让前端"没有播放源就整块不渲染"的判断再次纠缠, 而这正是上一次尝试里最先出问题的地方.

### 9.3 显示条件

只有 `status.available === true` 才渲染. 三种不渲染的情况: 没装插件 / 插件被禁用 / 默认程序是 AppX 且没配 `local_player`. 这样"不装就没有任何痕迹", 与现有播放源面板"没有源就不渲染"的逻辑一致.

### 9.4 交互

| 状态 | 表现 |
|---|---|
| 点击 | 图标短暂变 loading; 成功后 toast "已交给 VLC 打开", 图标恢复 |
| 失败 | toast 显示后端 `reason` 或通用文案, 不重试 |
| 多文件被截断 | toast 补充"当前播放器只能打开第一个文件" |
| 不可用 | 不渲染 (不灰显, 避免留下死控件) |

### 9.5 文案键

```
localPlayback.open          "用本机播放器打开"  / "Open with local player"
localPlayback.openWith      "用 {{player}} 打开"  / "Open with {{player}}"
localPlayback.done          "已交给 {{player}} 打开" / "Handed to {{player}}"
localPlayback.failed        "无法用本机程序打开" / "Could not open with a local program"
localPlayback.truncated     "当前播放器只能打开第一个文件"
plugins.capabilities.local_playback  "本机播放" / "Local playback"
```

## 10. 测试

| 层 | 用例 |
|---|---|
| 注册表解析 | `UserChoice` 命中; `HKCR` 回退; ProgId 无 `shell\open\command`; AppX ProgId; 未关联; 命令含 `%1` / `%*` / `%ENV%` |
| 命令行拆分 | 带引号路径、带空格参数、引号内空格 (`winreg` 全部 mock, 不在 CI 依赖真实注册表) |
| M3U 生成 | 非 ASCII 路径、含空格路径、`#` 与 `?` 的 URI 转义、空列表、索引内文件已消失 |
| token | 正确签名通过; 改一位失败; 其它 `metadata_id` 的 token 不通用; 重启后旧 token 仍可用 (密钥持久化) |
| 端点 | 未装插件 409; 开关关闭 403; 条目无文件 404; `file_id` 越权 400; 单文件走 `Popen` 单文件参数; 多文件走 M3U URL; AppX 降级 `startfile` + `truncated` |
| SPA 干扰 | 未带 token 的 `/localplay/x.m3u` 返回 404 而不是 `index.html` (回归 `_RESERVED_PREFIXES`) |
| 前端 | `available=false` 不渲染; 点击调用 mutation; 失败 toast |
| 手工 | 真实 VLC 拉起 (mkv / avi / iso 各一); 多文件条目列表内可见全部项 |

## 11. 未实测 / 待定

1. `#EXTINF:-1,<文件名>` 是否影响 VLC 与 mpv 的展开 —— 只测过不含 EXTINF 的写法.
2. mpv 对 `http://...m3u` 的支持与参数差异 (`--playlist=`). 本文的 exe 解析是播放器中立的, 但 M3U 喂法只验证过 VLC.
3. PotPlayer / MPC-HC 的命令模板里 `%1` 的位置与额外开关.
4. 服务只绑非回环地址时, 播放器能否访问 `http://127.0.0.1:{port}/localplay/...`. 若不通, 需要回落到"多文件直接传参"(受 32767 上限) 或按实际监听地址拼 URL.
5. Windows 之外 (macOS `open -a` / Linux `xdg-open`) 的等价解析: 不在本期, 接口留了 `executable=None` 的位置.

## 12. 分期

| 阶段 | 内容 | 交付判据 |
|---|---|---|
| P0 | 契约 + 注册表解析 + `status` / `launch` + M3U 端点 + 详情页图标 | 本机 mkv 一键进 VLC; 多文件条目列表 3 项 |
| P1 | 列表卡片图标、`local_player` 覆盖、i18n 补齐 | 两处入口行为一致 |
| P2 | 播放器参数适配 (续播、字幕挂载 `#EXTVLCOPT`)、非 Windows | 按需 |

P2 的字幕挂载要注意: `#EXTVLCOPT:sub-file=` 是 VLC 专有指令, 写进 M3U 会让 mpv 忽略整行但不报错. 若要做, 应做成插件声明的"方言", 而不是主机无条件写入.
