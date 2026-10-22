# 刮削批量任务性能分析

> 数据来源：`logs/tasks/task-*/task.log`，样本为本轮批量（task id ≥ 2694）中已产生完成事件的 1538 个任务日志。
> 统计脚本：`temp/analyze_perf.py`、`temp/analyze_phases.py`。

## 一、结论

单任务平均耗时 52.5 秒，其中**真正的元数据抓取只占 1.5～2 秒**。时间花在三个可消除的等待上：

| 排名 | 瓶颈 | 实测占比 | 本质 |
|---|---|---|---|
| 1 | 图片物化串行 + 每个 URL 发两次相同 HEAD | 69.6%（p50 38.2 s） | 无谓请求 + 无谓串行 |
| 2 | LLM 翻译失败重试 | 10.0%（p50 ≈ 5 s，51.6% 任务至少失败一次） | 内容审核恒定拒绝，重试注定失败 |
| 3 | 熔断级联短路 | 9.8% 任务整条短路，每条白跑 6 s | 代理瞬时抖动被放大成全站封锁 |

一句话：**这批任务里 96% 的墙钟时间不是在刮削，而是在等自己制造的等待。**

## 二、时间去哪了（任务级实例）

`task-4100`（HUNTC-307，成功，49.91 s）：

| 时间点 | 事件 | 说明 |
|---|---|---|
| 0.00 s | task started | |
| 0.25–2.11 s | javbus / dmm / javdb / hhh-av 四次 GET | **元数据抓取 = 2.1 s，四源并行，已是最优** |
| 2.11 → 15.93 s | `LLM request failed` ×2 | 13.8 s 全空转，glm-4-flash 返回 400/1301 内容审核拒绝 |
| 15.93 → 31.34 s | thumb / poster 下载 | 每个 URL 连续 3 次请求，且 URL 之间严格 1.0 s 间隔 |
| 34.43 → 43.60 s | extrafanart 一批 HEAD | 串行，0.20 s/次，其中 4 张返回 405 |
| 40.93 → 49.88 s | 图片落地 | |
| 49.91 s | task completed | **元数据 4.2%，翻译 27.7%，图片 67.7%** |

聚合到 1538 个任务：

| 阶段 | p50 | mean | 占比（mean） |
|---|---|---|---|
| 元数据（start → 最后一次 scrape ok） | 1.54 s | 10.72 s¹ | 20.4% |
| 翻译 + 收尾 | ≈ 5 s | 5.26 s | 10.0% |
| 图片（ok → images materialized） | **38.18 s** | 36.53 s | **69.6%** |
| 合计 | 41.60 s | 52.51 s | |

¹ mean 被重排任务拉高：同一 task id 的多次执行串在同一个 `task.log` 里，中间隔着 200～500 s 退避
（`task-4106/4107` 因此显示 2324 s）。测单次执行耗时只能看 p50，或按 `task started` 切段。

**当前实时吞吐**（2026-10-22 00:30 前后采样）：最近 15 分钟完成 51 条 ⇒ **17.6 s/条**，
较昨日启动初期的 10.7 s/条 慢了 65%。剩余 14,897 条 SCRAPE + 850 条 ACTOR_SCRAPE 按此推算约 **77 小时**。

## 三、瓶颈逐项

### 1. 图片物化：69.6% 的时间，且一半是白发的请求

**（a）同一 URL 发两次 HEAD。** 每张图固定走三步：

```
ResourceStore._acquire_locked  → client.resolve_final_url(url)   # HEAD #1，取重定向终址
                               → client.download(url, tmp)       # 内部 _probe(url) → HEAD #2，取 Content-Length
                                                                 # → get_bytes(url)   → GET
```

`src/amane/net/http.py::WebClient.download` 拿到的 `file_size` 与上一步 `_probe` 的结果完全相同，但上一步的返回值只用了 `final_url`，size 被丢弃。全部 91,128 次请求里 **HEAD 占 54,819 次（60.2%）**，其中 67% 打向 `awsimgsrc.dmm.co.jp`（61,251 次/全部请求，p50 114 ms）。

**（b）部分图不支持 HEAD。** 7,397 次 HEAD 返回 405（占 HEAD 的 13.5%），是 dmm 图床对某些路径拒绝该方法。这类 URL 要连吃两次 405 才落到 GET，白白多付 2×115 ms 和两行失败日志。

**（c）三个下载循环串行。** `src/amane/media/pipeline.py::materialize_images` 里 thumb、poster、trailer 各是一个 `for` + `await` 的串行循环；只有 `acquire_extrafanart` 用了 `asyncio.gather`。一个任务 17 个图片 URL 就是一个接一个等。

**（d）图片共享站点的限速桶。** 限速键是 host，`javbus` 站配置 `rate_limit = 1.0`，其图床 `/pics/cover/*.jpg` 与页面共用同一个桶 → 图片之间实测严格 1.02 s 间隔。默认限速 5/s，实测 `awsimgsrc` 相邻请求间隔 p50 = 0.205 s、请求本身 0.104 s，**约一半时间在等桶**。另外 `adaptive_rate_limit = True` 在遇到 429/5xx 时会降到 25%（5/s → 1.25/s），一旦误判就长期跑在慢速上。

### 2. LLM 翻译：51.6% 任务在等一个注定失败的请求

- `LLM request failed, retries exhausted` 共 **1,031 次，涉及 794 个任务**（51.6%）。
- 失败原因固定：`aigw1.immersivetranslate.com` 的 `glm-4-flash` 返回 `{"code":"1301","message":"系统检测到输入或生成内容可能包含..."}`。
- 单次失败白等 11.7 s（`task-4106` 实测），一个任务有 title + plot 两个字段，每个字段都要试一遍。
- `src/amane/llm/translator.py::LLMTranslator._ask` 捕获所有异常返回 `None`，不区分 4xx（确定性失败）与 5xx/超时（可恢复），也没有任何熔断，因此每个新任务都会重新踩同一个确定性的坑。
- `llm.rate_limit = 1.0` 且字段串行翻译，即使成功也要排队。

### 3. 熔断级联：9.8% 的任务整条被短路

526 次 `source failed / circuit_open`，分布在 151 个任务（9.8%）。开路的键：

| 键 | 次数 | 阈值 / 冷却 |
|---|---|---|
| `host:www.javbus.com` | 167 | 5 次 / 300 s |
| `host:www.dmm.co.jp` | 164 | 5 次 / 300 s |
| `host:javdb570.com` | 164 | 5 次 / 300 s |
| `proxy:socks5h://127.0.0.1:10808` | 136 | **2 次 / 120 s** |

四个数字几乎相等，说明**触发源是代理抖动**（Clash 的 curl 5/6/7），`proxy` 锁两次失败即开路，随后所有站点、所有在跑任务同时短路。被短路的任务 6 秒内依次失败四个来源（javdb → dmm → javbus → official）然后 `task deferred`，退避 180 s 后重排——而此时 host 锁还没到 300 s 冷却，很可能再次命中。

另一个细节：`javdb570.com` 是镜像，镜像开路期间主域 `javdb.com` 是健康的（同期 2,808 次请求 p50 402 ms 全部正常），但任务仍在镜像上短路，没有回切。

**重排把 6 秒的短路放大成 13 分钟的墙钟。** `task-4122` 一次日志里的三段：

| 段 | 时刻 | 结局 |
|---|---|---|
| 第 1 次执行 | 0.0 – 0.8 s | 三源 `circuit_open` → `task deferred`，退避 202 s |
| 第 2 次执行 | 203.0 – 209.6 s | 同样短路 → 再 `deferred`，退避 511 s |
| 第 3 次执行 | 720.3 s 起 | 才真正刮到，全程 780 s |

重排队列里 `retries ≥ 1` 的只有 104 条（占 0.7%），比例不高，但每一条都要占一个 worker 槽位反复空跑，且退避期间任务不可用。

### 4. 顺带观测到的两个小项

- `resource is upstream placeholder` 346 次：占位图仍先 HEAD 探测才放弃，每次约 0.2 s。
- `worker.concurrency = 4` 是所有任务类型共用一个 `asyncio.Semaphore`，实测 5 条 RUNNING 同时存在（claim 与 acquire 之间的计数窗口），SCRAPE 与 ACTOR_SCRAPE 互相抢占槽位。

## 四、加速清单

### A 档：改配置即可，热加载生效（零代码）

| 动作 | 键 | 预期 |
|---|---|---|
| 关掉 extrafanart 下载 | `scraping.download_resources` 去掉 `extrafanart` | 每任务图片 URL 从 ~17 降到 ~3，图片阶段 38 s → 8 s 量级。**收益最大的一项**，代价是失去剧照（Emby 只用 poster/thumb 时不损失展示效果） |
| 提高并发 | `worker.concurrency` 4 → 8~12 | 纯 IO 等待，吞吐近线性，1.49 万条 77 h → 30～38 h |
| 停用或换 LLM | `llm.enabled = false`，或换成不会被审核拒的模型 | 省 5 s/任务，且消除 51.6% 任务的无谓重试。翻译属于元数据增强，不影响入库 |
| 图片 host 单独放宽 | `network.rate_limits` 给 `awsimgsrc.dmm.co.jp` / `c0.jdbstatic.com` 配 10 | 图片阶段等待减半 |
| 关自适应降速 | `network.adaptive_rate_limit = false` | 避免一次 5xx 把速率长期锁在 1.25/s |

### B 档：小改，收益明确

| 动作 | 位置 | 预期 |
|---|---|---|
| 复用已探到的 Content-Length | `WebClient.download` 增加可选 `file_size` 入参，`ResourceStore._acquire_locked` 把 `resolve_final_url` 的 size 传下去 | 图片请求 **-33%**，图片阶段 -3～5 s |
| 小图直接 GET | `download` 先 `get_bytes`，从响应头读 Content-Length 再决定是否改走分片 | 图片请求再 **-33%**（对 < 2 MB 的图，HEAD 完全多余） |
| 记录 405 的方法不支持 | 探测失败时记住 host+方法，后续跳过 HEAD | 省 7,397 次请求 |
| thumb/poster 循环并发 | `materialize_images` 三个 `for` 改 `asyncio.gather`，注意 poster 裁剪依赖 thumb 本地文件，需保留该依赖 | 重叠限速等待 |

### C 档：需要设计取舍

| 动作 | 说明 |
|---|---|
| LLM 失败熔断 | 对 4xx 不重试、且同 `(base_url, model)` 连续失败 N 次后短期跳过翻译（与站点熔断同构）。当前每任务都要重新踩一遍 |
| 代理熔断参数 | `proxy_fail_max` 2 → 4～6；或改为「N 秒窗口内失败率」而非绝对计数。当前 2 次抖动封锁全部站点 120 s |
| 镜像回切 | 镜像 host 开路时回切主域，而不是在镜像上短路 |
| 图片免探测 | 图床 URL 形态稳定时，直接按扩展名判定为图片、跳过所有 HEAD |

## 五、组合效果测算

以 14,897 条 SCRAPE + 850 条 ACTOR_SCRAPE 剩余为基准。当前实测 17.6 s/条（4 并发下有效并发
约 3，单任务 52.5 s）。估算按「单任务耗时 ÷ 有效并发数」外推：

| 方案 | 单任务耗时 | 有效并发 | 有效吞吐 | 总时长 |
|---|---|---|---|---|
| 现状 | 52.5 s | 3.0 | 17.6 s/条（实测） | ≈ 77 h |
| 仅提并发到 8 | 52.5 s | 6.5 | 8.1 s/条 | ≈ 35 h |
| A 档全开（关 extrafanart + 关 LLM，8 并发） | ≈ 12.5 s | 6.5 | ≈ 1.9 s/条 | ≈ 8 h |
| A 档 + B 档（再砍 2/3 图片请求） | ≈ 8 s | 6.5 | ≈ 1.2 s/条 | **≈ 5 h** |

注意并发不是无限有效：图片阶段共享按 host 的限速桶（`awsimgsrc` 5/s、`javbus`/`javdb` 1/s），
并发升高只会让人排队等同一个桶。**先砍请求数（A/B 档）再提并发，收益才不互相抵消。**

## 六、不建议的动作

- **加大 `network.timeout`**：当前 10 s 对文本请求足够，加大只会让真挂起的请求占用 pydantic-ai 与 worker 槽位更久。
- **关响应缓存**：`response_cache_ttl_s = 300` 已经在跨番号复用搜索页，是当前少有的净收益项。
- **提高 `network.concurrency`（分块下载并发）**：只影响 > 2 MB 的预告片，这批任务没开 trailer，无收益。
