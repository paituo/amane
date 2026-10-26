# 上游限速契约（硅基流动 / OpenAI 兼容协议）

> 目标：把"上游凭什么限、用什么信号告诉我们、我们据此做什么"写成可判定、可测试的契约，
> 而不是靠一个 `cooldown=60` 的经验值硬扛。
>
> 取证档位：**【实测】**= 本机抓到的真实报文/日志（2026-10-26）、**【文档】**= 官方文档、**【源码】**= 依赖库源码、
> **【未取到】**= 本次仍缺的样本。

## 1. 结论（先看这七条）

1. **服务器端限流是黑盒**：200/400/401 与流式响应**都不带任何限流配额头** —— 没有 `x-ratelimit-*`、
   没有 `Retry-After`、没有 `RateLimit-*`，唯一自定义头是 `x-siliconcloud-trace-id`（429 也带一个）。【实测】
   ⇒ **不可能做"读余量提前刹车"，只能事后响应**。
2. **拒绝时只有 429 + 统一错误体** `{"code": 50602, "message": "...Details: TPM limit reached.", "data": null}`。
   当前网关把触发指标写进 message 的 `Details: <METRIC> limit reached.` —— 这是**唯一**的指标级信号。【实测】
3. **`max_tokens` 不预扣**：`max_tokens=128000` 的小请求照样 200，说明限流不按 `prompt + max_tokens` 预留，
   按实际用量结算。【实测】⇒ 本地按"输入估算预扣 + 响应 usage 校正"的记账口径是对的。
4. **【重要更正】token 总量确实被限制，且它就是主要约束。** 串行（约 45 RPM，远低于文档 1000 RPM）下，
   单个 27,279 token 的请求**第 5 个就被拒**：通过时窗口内累计 109,148，再要 27,279（合计 136,427）即 429。
   用 5,461 token 的请求复测，收敛到 **125,555 通过 / 131,016 拒绝** ⇒ **TPM ≈ 128,000 / 分钟**。【实测】
   ⇒ 本节旧版写的"本账号额度 ≥ 21.6 万/分钟、36000 只是自我节流"**已被推翻**，原因见第 5 条。
5. **限流口径是"准入只读已结算值 + token 事后结算"，因此并发可以穿透。** 同一分钟窗口内，
   20 并发 × 27,295 = **545,900 token / 3 秒全 200**，是串行上限（≈128k）的 **4.3 倍**。【实测】
   机理：token 只有响应结束拿到 `usage` 才知道，不按 `max_tokens` 预留（第 3 条）；
   准入时读的是"已完成请求累计"，同一瞬间涌入的请求都看到同一个低值，于是全部放行。
   ⇒ 这也解释了此前"30 并发 216k 全过"的假象：**那不是额度大，是记账滞后**。
6. **穿透要还债，不是免费**：545k 打进窗口后，紧随其后的串行请求**全部 429**，
   且持续时间明显超过 60 s（恢复曲线见 §2.5）。⇒ 客户端靠并发绕过 TPM 只会换来整分钟以上的完全不可用。
7. **不存在"并发数"限制**：官方七指标为 RPM/RPH/RPD/TPM/TPD/IPM/IPD，**无 concurrency 项**【文档】；
   实测 20 并发、30 并发均未出现并发维度的拒绝，唯一拒绝原因始终是 `TPM`【实测】。

## 2. 实测报文（2026-10-26 02:15–02:34 UTC / 10:15–10:34 CST）

### 2.1 200（非流式，`/v1/chat/completions`）

```
HTTP/1.1 200 OK
date: Mon, 26 Oct 2026 02:16:24 GMT
content-type: application/json; charset=utf-8
content-length: 400
connection: keep-alive
x-siliconcloud-trace-id: ti_4gqh5rww5f0rs4crw9
```

### 2.2 200（流式，`text/event-stream`）

```
date / content-type: text/event-stream / transfer-encoding: chunked / connection: keep-alive
vary: Accept-Encoding / cache-control: no-cache
x-siliconcloud-trace-id: ti_rcqizth1q1jwguadvr
```

### 2.3 错误码表（同一网关，头部同样只有 trace-id）

| 场景 | HTTP | body |
| --- | --- | --- |
| 模型不存在 | 400 | `{"code":20012,"message":"Model does not exist. Please check it carefully.","data":null}` |
| `messages` 缺失 | 400 | `{"code":20014,"message":"Message field is required.","data":null}` |
| Key 无效 | 401 | `{"code":30014,"data":null,"message":"Token is invalid."}`（另有 `www-authenticate`，**无** trace-id） |
| 限流（用户 2026-10-26 上午实际撞到） | 429 | `{"code":50602,"message":"Request was rejected due to rate limiting. Details: TPM limit reached.","data":null}` |

⇒ 错误体形状统一为 `{code:int, message:str, data:null}`；**排障必须记 `code` 与 trace-id**。

### 2.4 加压测试（第一轮被误读，第二轮才是判据）

**第一轮（2026-10-26 02:15–02:22 UTC）—— 全部 200，当时误判为"额度很高"**

| 试验 | 参数量 | 结果 |
| --- | --- | --- |
| 8 并发 × 7,206 prompt token | 57,776 token / 2.0 s | 200 ×8 |
| 12 并发 × 7,206 | +86,664 token | 200 ×12 |
| 30 并发 × 7,206 | +216,660 token / 6.4 s | 200 ×30 |
| `max_tokens` 阶梯 20000/39000/40000/128000 | 单请求 | 全 200（未按 max_tokens 预扣） |
| DeepSeek-V3.2 连打 35 次（40 s） | 文档称 RPH=30/h | 全 200（该规则不适用于此模型/档位） |
| Kolors 图像 4 连发 | 文档称 IPM=2 | 全 200 |

⚠️ 这张表只能证明"**并发时**不按总量卡"，**不能**证明额度高 —— 见第二轮。

**第二轮（2026-10-26 02:27–02:31 UTC）—— 串行 vs 并发对照**

| 试验 | 频率 | 结果 |
| --- | --- | --- |
| 串行 4 × 27,279 token | ≈45 RPM（各隔 1 s） | 200 ×4，累计 **109,148** |
| 串行第 5 个 27,279 | 同上 | **429**，`Details: TPM limit reached` |
| 串行 3 × 5,461（承接上窗口余量） | ≈45 RPM | 200 ×3，窗口内累计 **125,555** |
| 串行第 4 个 5,461 | 同上 | **429**（131,016 > 阈值） |
| **20 并发 × 27,295** | 同时准入 | **200 ×20，合计 545,900 / 3 s** |
| 紧随其后的串行小请求（`hi`, 3 次） | — | 全部 **429**（穿透留下的账） |
| 25 rps × 50 s（1,250 次，`max_tokens=1`） | 1,250 RPM | 全部 **429**（债务未清，见 §2.5） |

⇒ 判据：**阈值 ≈ 128,000 token/分钟**（区间 125,555–131,016）；**并发可穿透 4.3 倍**；**穿透后整窗口不可用**。
频率不是瓶颈：45 RPM 就已被 token 卡住，而 1,000 RPM 的额度根本没机会验证。

### 2.5 恢复曲线（穿透之后要多久）

20 并发打进 545,900 token 后，串行小请求（`hi`，`max_tokens=1`）连续探测：

```
[  0.2s] 429   [ 10.4s] 429   [ 20.6s] 429   [ 30.8s] 429   [ 41.0s] 429
[ 51.2s] 429   [ 61.4s] 429   [ 71.6s] 429   [ 81.8s] 429   [ 92.3s] 429
[102.5s] 429   [112.8s] 429   [123.0s] 429   [133.2s] 429   [143.4s] 429
[153.9s] 200   ← 恢复
```

**实测**：从穿透结束（02:29:23 UTC）到恢复（02:34:00 UTC）约 **4.5 分钟**；
从开始探测（02:31:20）算，429 至少持续 **154 s**。

- **不是 60 s 滚动窗口**：60 s 之后（02:30:23 起）仍是 429，说明超额部分不会随窗口滑出立即清零，
  更像"按额度速率偿还债务"（545k / 128k ≈ 4.3 分钟，与实测 4.5 分钟吻合）。
- **无 `Retry-After`**：全程 `retry-after=None`，客户端**没有任何服务端提示**可依。
- **给契约的硬结论**：`rate_limit_cooldown_s` 默认 60 **太短** —— 撞 429 后 60 s 重试几乎必然再撞。
  首退建议 ≥180 s 起倍增（与任务重排的 180 s 口径一致），且**不要**用高频重试去"探活"
  （本次探测期间夹了 1,250 次 25 rps 的失败请求，可能延长了惩罚，这一点无法完全排除）。

### 2.6 附带的网络事实（影响可信度）

`api.siliconflow.cn` 的 DNS 按出口分流：

- 国内 DoH（dns.alidns.com）→ 上海 ALB：`47.117.38.205 / 47.100.67.98 / 8.133.255.105 / 47.117.173.229`
  → **本机连不通**（TCP 可建，TLS 直接被 RST/EOF）。
- 国际 DoH（1.1.1.1）→ 香港 NLB：`8.218.57.96 / 47.76.77.108` → **可达，401/200 正常**。

本次所有实测都是把域名解析固定到香港 NLB 取得的（`temp/llm_rate_probe.py --pin-ip`）。
⇒ 报告里的额度数字**属于香港边缘**，上海边缘无法验证；两边是否同口径未知。

## 3. 官方口径【文档】（用于校验，不作契约依据）

- 计量主体：**账户级，不分 API key**；每个模型单独计数。
- 指标：RPM / RPH / RPD / TPM / TPD / IPM / IPD，任一到顶即触发。
- 档位：L0 1000 RPM / 40,000 TPM → L5 10,000 RPM / 2,000,000 TPM（按当月消费升级）。
  **实测 ≈128,000 不落在任何整档上**（L2 80k / L3 160k 之间）⇒ 免费模型用的是独立的固定值，
  与文档 2.3 的档位表无关（文档 2 节确实写了"免费模型 Rate Limits 指标是固定值"）。
- 文档里的 429 文案是旧版（`If you want more, please contact contact@siliconflow.com`），**现网已改成
  `Details: <METRIC> limit reached.`** ⇒ 文档落后于实现，解析要以实测为准。
- 文档通篇未提任何限流响应头，与 §2.1/2.2 的实测一致。

## 4. 契约

### 4.1 信号分级（按可用性降级，L0 实际为空）

| 级 | 信号 | 实测可用性 | 用途 |
| --- | --- | --- | --- |
| L0 | `Retry-After` / `retry-after-ms` | **不存在** | 有则直接当冷却；无则走 L2/L3 |
| L1 | `x-ratelimit-remaining-*` / `*-reset` | **不存在** | 有则同步余量；无则只能本地估算 |
| L2 | `code=50602` + `Details: <METRIC> limit reached` | **存在**（指标维度的唯一信号） | 按 4.2 分维度处理 |
| L3 | 仅 `HTTP 429` | 存在 | 通用冷却 `rate_limit_cooldown_s` |

实现上仍按 L0→L1→L2→L3 顺序取值：**为将来上游补头留出通道，但不得依赖**。pydantic-ai 已经把
`headers`（键小写）与 `retry_after` 挂在异常上（`ModelHTTPError.headers` / `ModelAPIError.retry_after`，
由 `_parse_retry_after` 从 `retry-after-ms`/`Retry-After` 解析），读它们零成本。
见 `pydantic_ai/models/openai.py::_map_status_error`（`headers=dict(e.response.headers)`）。

### 4.2 指标 → 动作

| METRIC | 含义 | 动作 |
| --- | --- | --- |
| `TPM` | 分钟 token 预算（实测阈值 ≈128,000） | 冷却 **≥180 s**（实测恢复需 154 s，60 s 重试必再撞）；窗口已用量置满 |
| `RPM` | 分钟请求数 | 冷却；本窗口内频率桶降半速 |
| `TPD` / `RPD` | 日额度耗尽 | **长冷却（默认 1800 s）且不重试**，快速失败并给出可读原因 |
| `RPH` | 小时级（DeepSeek 系文档口径） | 冷却到 1 h 档，不重试 |
| `IPM`/`IPD` | 图片（助理不涉及） | 忽略 |
| 解析不出 | — | L3 通用冷却 |

### 4.3 记账口径（与实测一致）

- 上游是 **≈128,000 token 的滑动预算（分钟量级）**，且**按实际 usage 结算、不按 `max_tokens` 预留**（§2.4）。
  超额不是"过 60 s 自动清零"，而是**按额度速率偿还**（§2.5：545k 的债要还约 4.5 分钟）。
- **客户端必须自己"预扣"**：这是本地限速器存在的根本理由。上游准入只读"已完成"的累计，
  一旦我们并发放行 N 个请求，它们会同时穿透；穿透后整窗口被记账锁死（§1 第 6 条）。
  ⇒ `AgentRateLimiter.acquire()` 在**发请求前**按输入估算占住预算，等于替上游补齐它缺失的预扣，
  是**正确且必要**的，不是"自我节流"。**不得**为了提速把并发塞进 model 层。
- 请求后按 `usage.input+output` **校正**（不是"预扣 + 再加"，否则重复计数）。
- 估算必须含工具声明与 `instruction_parts`（实测首轮 ≈6,869 token，其中工具+指令 6,664，写死常量估不出）。
- 中文 token/字符比**随文本重复度变化**：重复填充文本实测 1 : 1.83（50,000 字符 → 27,279 token），
  自然文本 1 : 2.78（20,000 字 → 7,206 token）。现常量 `CJK_CHARS_PER_TOKEN=1.5` 按 1.5 字/token 计，
  即**估出的 token 数比实际偏多**（重复文本偏多 ~22%，自然文本偏多 ~85%），方向安全，不用改。
- **`tpm_limit` 必须 < 128,000**：留窗口漂移与估算误差的余量。建议 **100,000**（≈78% 使用率）。
  现值 36,000 只有上游额度的 28%，是纯自我节流。
- 上游账户级计量 ⇒ 限速器必须是**全进程单例**（`service.request_limiter()` 已按配置三元组缓存）；
  多进程部署各自放行，属已知缺口（会叠加穿透）。

### 4.4 可观测性（契约的一部分）

429 必须落一条结构化日志：`status / code / metric / retry_after / headers 里是否有 rate 相关键 /
窗口内已用 token / x-siliconcloud-trace-id`。否则"为什么慢、为什么失败"只能靠 UI 截图 ——
本次排查成本就是这么来的（app.log 里只有一行 `session title request failed`）。

## 5. 落地状态与差距

**已按实测落地（2026-10-26）**：

| 配置项 | 值 | 依据 |
| --- | --- | --- |
| `agent.tpm_limit` | **100,000** | 上游 ≈128,000/min 的 78%，留窗口漂移与估算误差 |
| `agent.tpm_window_s` | **60**（新增） | 与上游分钟窗口一致；原先硬编码为 `WINDOW_S` 常量 |
| `agent.rate_limit_cooldown_s` | **180**（原默认 60） | 实测 429 后 154 s 内重试仍被拒 |
| `agent.rate_limit_backoff_factor` | **2.0**（新增） | 连续撞墙 180→360→720…，成功即归零 |
| `agent.rate_limit_max_cooldown_s` | **900**（新增） | 封顶，避免日额度耗尽时回合无限挂起 |

实现落点：`config/manager.py::AgentConfig`（字段 + 校验范围）、`agent/limits.py::AgentRateLimiter`
（`window_s` / `backoff_factor` / `max_cooldown_s` 三个字段，`penalize()` 倍增、`record()` 归零）、
`agent/service.py::request_limiter()`（缓存键含全部六项，改值即换实例）。单测见
`tests/agent/test_limits.py` 的 `test_penalize_escalates_on_consecutive_limits` 等。

**仍有差距**：

| 项 | 现状 | 差距 |
| --- | --- | --- |
| `Retry-After` | `penalize(exc.retry_after)` ✅ | 上游从不发 ⇒ 永远走 `cooldown_s` + 倍增 |
| 响应头读取 | 未使用 | 无实际损失（上游不带头），但也没记 trace-id |
| 指标判定（L2） | 未做，所有 429 同一处理 | `TPD/RPD` 这类日额度耗尽会被当成 TPM 抖动，靠 900 s 封顶兜底 |
| 429 日志 | 只有 pydantic-ai 的异常串 | 缺 `code / metric / trace-id` |
| 冷却语义 | `acquire()` sleep 等冷却 | 深冷却应**快速失败**，不该占着回合墙钟干等 |
| **并发控制** | model 层单实例、串行 acquire ✅ | 正确。**不要**改成并发放行 —— 并发只会穿透然后被记账锁死 |

## 6. 仍未闭合的点

**已取到**（本次）：真实 429 的响应头（只有 `x-siliconcloud-trace-id`，无 `Retry-After`、无限流头）；
恢复曲线（429 持续 ≥154 s）；串行阈值区间（125,555–131,016）。

**仍缺**：

1. **阈值是否精确等于 128,000**：区间只能收窄到 125,555–131,016。要精确需在窗口内以更细步长逼近，
   每次试探都要还 2–4 分钟债，性价比低，对契约无影响（配 100,000 已在区间外）。
2. **`Details` 词表**：只见过 `TPM`。`RPM` / `RPD` / `TPD` 的文案未取到 ⇒ 解析器要写成通用正则
   `Details:\s*(?P<metric>[A-Z]+)\s+limit reached`，不要按 TPM 硬编码。
3. **上海边缘与香港边缘是否同额度**：本机连不通上海 ALB（§2.6），无法对照。
   ⇒ 若生产走上海出口，实际阈值可能不同，`tpm_limit` 首次上线要观察。
4. **429 期间的失败请求是否计入额度**：本次恢复偏慢（154 s vs 理论 60 s）无法排除
   "高频重试延长惩罚"的可能 ⇒ 客户端**不要用重试探活**。

复跑命令（`serial` 是本次新增的关键子命令）：

```bash
# 串行低频堆 token —— 唯一能测出真实 TPM 阈值的方式
.venv/Scripts/python.exe temp/llm_rate_probe.py --pin-ip 8.218.57.96 serial -n 40 -c 10000 --interval 1.0
# 并发放行（能穿透，但会留下数分钟的债）
.venv/Scripts/python.exe temp/llm_rate_probe.py --pin-ip 8.218.57.96 burst -n 20 -t 50000
# 撞墙后的恢复曲线
.venv/Scripts/python.exe temp/llm_rate_probe.py --pin-ip 8.218.57.96 recover --attempts 40 --interval 10
```

## 7. 后续（还没做的两项）

已做的见 §5「落地状态」（5 个配置项 + `penalize` 倍增 + `record` 归零 + 单测）。

还剩两项，都依赖"指标分类"这一件事：

1. `agent/limits.py` 加 `RateLimitSignal` + `classify_rate_limit(exc)`：`retry_after` → `headers` →
   `Details` 正则 `Details:\s*(?P<metric>[A-Z]+)\s+limit reached` → 缺省；顺带取 `provider_error_code`
   与 `x-siliconcloud-trace-id`。分类后 `penalize(signal)` 才能按 4.2 分维度动作
   （现状：所有 429 一律按 TPM 处理，`TPD/RPD` 靠 900 s 封顶兜底）。
2. 429 分支打结构化 warning（含 metric、trace-id、窗口已用）；配置再补
   `rate_limit_deep_cooldown_s`（默认 1800）与 `rate_limit_fail_fast_on_daily`（默认开）。
   单测用本文 §2.3 的录制样本断言分类，不依赖网络。
