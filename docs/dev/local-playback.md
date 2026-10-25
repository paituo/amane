# 本机播放 (外部播放器)

详情页的「播放」可以交给服务所在机器上的外部播放器, 而不是在页面内解码. 本文记录这项能力的边界、接口与已实测的结论.

## 1. 为什么要它

播放源插件只能产出 `file` / `upstream` / `hls` 三种目标, 三种都由主机转成 HTTP 响应交给浏览器的 `<video>`. 浏览器能解码的容器有限, 库里实测的 17935 个文件中:

| 容器 | 数量 | 页面内可播 |
|---|---|---|
| mp4 | 16054 | 是 |
| avi | 948 | 否 |
| mkv | 344 | 否 (Chromium 不解 MKV) |
| wmv | 258 | 否 |
| iso | 151 | 否 |
| mpg | 98 | 否 |
| rmvb | 81 | 否 |

上限就是 89.5%. 主机不做转码, 因此剩下 10.5% 只有两条路: 事先转封装, 或者交给本机播放器. 后者零成本, 也不挑格式.

## 2. 为什么不是插件

曾考虑把「调用本机播放器」做成第三种插件能力 (`local_playback`), 让插件声明"用哪个程序、打开哪些文件". 放弃的理由:

- 拉起程序是**副作用**, 而插件契约里的 `probe` / `resolve` 会被重试、预取、缓存复用. 把副作用挂在只读钩子上, 刷一次码流列表就会起一个播放器.
- 副作用需要显式的动作端点, 而端点一旦存在, "哪个程序、开哪些文件"就只剩两三个决定, 不值得为此引入插件清单、安装包与版本兼容.
- 这是一个**部署形态**的选择 (服务跑在有桌面的机器上吗), 不是内容来源的选择. 放在设置里, 语义更准.

所以: 播放源仍然是插件的事, 本机播放是主机的设置.

## 3. 配置

`[playback]` 段, 设置页的「播放器」标签对应这三个字段.

| 字段 | 默认 | 含义 |
|---|---|---|
| `external` | `false` | 外部播放开关. 关闭时走内置播放器 (插件提供的码流). 界面文案: 使用外部播放器 |
| `mode` | `system` | `system` = 系统默认关联程序; `custom` = `player` 指定的可执行文件 |
| `player` | `""` | 自定义播放器的可执行文件. 仅 `custom` 使用; 空表示不可用, 接口会以 400 拒绝 |

```toml
[playback]
external = true
mode = "custom"
player = "C:\\Program Files\\VideoLAN\\VLC\\vlc.exe"
```

`mode` 在设置页用 `x-widget: "RadioRows"` 渲染成"一行一个选项、单选框在右"的列表, 每项带一句说明 (`fields.playback.mode.hints.<option>`). `player` 用 `PathPicker` (文件), 因此有「浏览...」按钮.

**注意**: 文件浏览受 `AMANE_SAFE_DIRS` 限制. 播放器通常装在 `C:\Program Files`, 默认不在允许目录内, 此时「浏览」进不去, 但输入框可以直接粘贴路径.

## 4. 接口

两个端点, 都在 `/api/playback` 下, 受既有的 Bearer / cookie 鉴权保护.

### `GET /api/playback/local-player`

```json
{ "enabled": true, "mode": "custom", "player": "vlc.exe" }
```

不读库, 打开详情页即可调用. `player` 是文件名 (不暴露完整路径), `null` 表示交给系统关联或未配置. 前端据此决定封面按钮是拉起外部播放器还是驱动页面内播放.

### `POST /api/playback/{metadata_id}/open-local?file_id=`

| 状态 | 条件 |
|---|---|
| 200 | `{ "paths": [...], "player": "vlc.exe" \| null }` |
| 403 | `playback.external` 关闭 |
| 400 | `mode=custom` 但 `player` 为空; `file_id` 不属于该条目; 播放器或文件不存在 |
| 404 | 条目不存在; 条目没有索引到文件; 没有视频文件; 全部视频文件都已不在磁盘上 |

路径**只取自条目索引**, 不接受任意地址. 这个动作的本质是"在服务所在机器上执行一个程序", 收窄到"已入库的文件 + 已配置的播放器"之后, 能触发的范围才是有界的.

## 5. 挑文件

| 情形 | 交给播放器 |
|---|---|
| 指定了 `file_id` | 只有那一条 |
| `mode=system` | 体积最大的视频, **一条** |
| `mode=custom` | 体积最大的视频在前, 其余视频按入库顺序 (id 升序) 跟随 |

非视频文件 (`jpg` / `nfo` / `srt`) 一律不入列 —— 扩展名取自 `amane.library.MEDIA_EXTENSIONS`. 主文件按体积取, 是因为条目下常混着预告与样片, 体积最大的那个才是正片. 缺少的文件被静默跳过; 全部缺失才报 404.

系统关联只认一个路径, 所以 `system` 方式拿不到多文件列表. 要按顺序播多片 (CD1/CD2) 必须用 `custom`.

`mode=system` 时配置里的 `player` 一律忽略, 不会透传给拉起逻辑: 从 `custom` 切回 `system` 后旧路径仍留在 TOML 里, 若继续使用, "系统默认"就名不副实; 路径失效时更会直接报错, 而不是回退到关联程序. 接口返回的 `player` 也随之为 `null` —— 值取自"实际用到的程序", 不是配置.

## 6. 拉起

`src/amane/playback/local_player.py`, 只负责"拉起", 不等播放器退出 (播放器是长驻进程).

| 平台 | `system` 方式 |
|---|---|
| Windows | `os.startfile(path)` |
| macOS | `open <path>` |
| Linux | `xdg-open <path>` |

`custom` 方式一律 `Popen([exe, *paths])`, 三个标准流都接 `DEVNULL`, 不继承 Web 服务的管道. 启动即返回.

Windows 上没有关联程序时 `startfile` 抛 `winerror 1155`, 原文是英文资源串, 已转成 `系统没有与该格式关联的程序: .xxx`.

**已实测** (Windows 11 + VLC 3.0.24):

- `.mp4` / `.mkv` / `.avi` / `.wmv` / `.iso` 都已关联到 VLC, `startfile` 能真的把 Z: 盘上的 1.9 GB 文件交给它并开始解码;
- `vlc a.mp4 b.mp4 c.mp4` 直接得到 3 项播放列表 (`rebuild done - 3 items`), 因此多文件不需要生成任何列表文件.

**限制**: 服务必须有可见桌面会话. 装成 Windows 服务或计划任务 (session 0) 时, 调用不报错, 但窗口不会出现. 这条没有实测, 是按 session 0 的窗口站隔离推断的.

## 7. 前端

### 封面按钮

详情页封面上那个大播放按钮**原本播预告片, 现在播正片**; 预告片的播放逻辑 (内联播放器、失败提示、关闭按钮) 已整体删除. `trailer_url` 仍然是可编辑、可锁定的元数据字段, 只是不再有播放入口.

| 设置 | 点封面按钮 |
|---|---|
| `external = true` | `POST .../open-local`, 成功后提示"已交给 X 打开" |
| `external = false` | 把播放请求交给下方的播放面板: 滚动过去并自动起播 |

请求以计数 (`playNonce`) 传递: `autoPlay` 与播放器的 `key` 都直接按它推导, 不另存一份状态.

### 播放卡片

`external = true` 时不探测任何来源, 卡片只留一个入口: 16:9 黑底 + 播放按钮 + "外部播放器已启用, 点击后交给 X 打开". `mode=custom` 但路径为空时, 按钮换成一条橙色提示.

`external = false` 且**没有任何播放源插件**时, 卡片不再整块消失, 而是显示一行说明. 原先的 `return null` 会让封面按钮点下去毫无反馈.

## 8. 涉及的文件

| 文件 | 作用 |
|---|---|
| `src/amane/config/manager.py` | `PlaybackConfig` / `ExternalPlayerMode`, 挂在 `HotSettings.playback` |
| `src/amane/playback/local_player.py` | 拉起逻辑与 `LocalPlayerError` |
| `src/amane/api/routes/playback.py` | 两个端点与文件挑选 |
| `src/amane/api/models/playback.py` | `PlaybackLocalPlayerResponse` / `PlaybackLocalOpenResponse` |
| `web/src/lib/media/local-player.ts` | `useLocalPlayer` / `useOpenLocalPlayback` / `canLaunchLocalPlayer` |
| `web/src/components/media/metadata-detail.tsx` | 封面按钮语义 |
| `web/src/components/media/playback-panel.tsx` | 外部模式卡片、播放请求、无来源提示 |
| `web/src/components/schema-form/fields/radio-rows-field.tsx` | `x-widget: "RadioRows"` |
| `web/src/i18n/locales/{zh-CN,en}/settings.json` | 设置页文案 |
| `web/src/i18n/locales/{zh-CN,en}/metadata.json` | 详情页文案 |

## 9. 测试

- `tests/api/test_playback.py`: 6 例 —— 默认关闭、未开开关拒绝、`system` 只传一个、`custom` 传全部视频且封面图不入列、四种拒绝、越权 `file_id`.
- `tests/playback/test_local_player.py`: 3 例 —— 空列表、文件不存在、播放器不存在.

## 10. 没有做的

- **转封装**: mkv → mp4 (`ffmpeg -i in.mkv -c copy out.mp4`) 能把页面内可播率提到约 93%, 属独立任务.
- **外部 HLS 服务**: 插件可以用 `hls` 目标指向 Jellyfin/Emby, 让 avi/rmvb 也在页面内播. 与本文互不冲突.
- **单实例与进度**: 重复点按钮会重复调用播放器 (由播放器自己决定是新窗口还是复用); 也不传 `--start-time`, 不记忆进度.
- **路径探测**: 不扫注册表推荐播放器安装位置, 需要手填路径.
