# 上游故障

> 入口: `src/amane/llm/`、`src/amane/agent/`、`src/amane/api/routes/agent_agui.py`.
> 翻译管线见 [llm.md](llm.md), 助理见 [agent.md](agent.md), 端点自检见 `src/amane/probe/llm.py`.
> 本文记录上游不稳定 (限流 / 超时 / 协议不兼容) 时的实际行为与仍生效的约束; 参数与签名去源码.

## 传输层重试

`agent.max_retries` 是首次请求之外的重试次数, 经 `agent/runtime.py::resolve_model_settings` 写进 `ModelSettings['max_retries']` 按请求下发: 取值 `N` 时一次被限流的请求实发 `N + 1` 次, `0` 表示只发一次. 默认 `0`: SDK 退避是亚秒级, 对按分钟窗口的 token 限额几乎无效, 撞限流继续重发只是白烧配额; 等待改由 `agent.tpm_limit` 与 `agent.rate_limit_cooldown_s` 在请求前完成. 填 `None` 表示不传该键, 沿用各 SDK 客户端自身的重试配置 (OpenAI 默认两次).

约束:

- **经 `ModelSettings` 下发, 不要在 `build_model` 自建 SDK 客户端注入重试次数**. 按请求下发对 OpenAI、Anthropic 等协议一并生效; 自建客户端只覆盖 OpenAI 兼容路径, 且其生命周期不随 `AgentService.rebuild` 关闭.
- **重试策略仍归 SDK**: 该键只改次数, 哪些失败可重试与退避间隔由 SDK 决定. 调用侧不得再包一层重试, 否则实际请求次数为两层的乘积.
- **翻译路径不读该键**: 翻译自带传输客户端 (proxy、超时、连接池归属调用方), 其重试沿用 SDK 默认; 修改翻译侧重试须在该处的客户端构造上处理.

依赖约束: anthropic 2.x 把 `BetaMessageParam` 改为 `TypeAlias`, pydantic-ai 仍按构造函数调用, Anthropic 协议会在发出请求前抛 `TypeError`. 上游声明 `anthropic>=1.12.1` 没有上界, 故 `pyproject.toml` 的 `[tool.uv]` 约束 `anthropic<2`.

## 助理回合

- **限速**: 频率 / token 预算 / 限流冷却三项合成 `agent/limits.py::AgentRateLimiter`, 由 `agent/runtime.py::RateLimitedModel` 包在 model 上 (`AgentService.request_limiter()`, `rate_limit` / `tpm_limit` / `tpm_window_s` / `rate_limit_cooldown_s` / `rate_limit_backoff_factor` / `rate_limit_max_cooldown_s` 任一变化时换新实例). 全部会话与会话标题请求共用同一份状态; 不得改为每回合新建, 并发会话各自放行时上游看到的速率是配置值的数倍. **挂在 model 而不是回合能力上**: 会话标题 (`agent/naming.py`) 不经过 agent 回合, 只有包在 model 上它才与回合同桶.
  - 频率: `agent.rate_limit` (每秒请求数), 桶容量 1, 严格平滑.
  - token 预算: `agent.tpm_limit` (输入 + 输出), 窗口长度 `agent.tpm_window_s` (默认 60 秒), `None` 表示不按 token 限速. 请求前按估算预扣, 响应后按上游真实用量校正; 上游不回用量时保留估算. 估算口径: 正文按字符折算 (中日文 1.5 字/token、西文 4 字符/token), **工具声明与 `instruction_parts` 按 `ModelRequestParameters` 的实际内容逐次全额计入** (能力的工具在运行时才并入, 写死常量估不出), 另加 200 token 信封开销; 并且不低于上一轮实测的 input (历史只增不减). 超预算即等到窗口滑出再发, 不等到撞 429. 只限请求数防不住按 token 计量的上游: 助理全量声明工具, 光 explore 三个工具的参数 schema 就有两万字符. 实测上游阈值约 128000/分钟 (见 [llm-rate-contract.md](llm-rate-contract.md)), `tpm_limit` 须低于它.
  - 冷却: 收到 `ModelRateLimitError` (429) 后 `agent.rate_limit_cooldown_s` (默认 180 秒) 内不放行任何请求 (含会话标题); 上游给了 `Retry-After` 时以它为准, 本值只作回退. **连续撞墙时按 `agent.rate_limit_backoff_factor` 倍增** (180→360→720…), 封顶 `agent.rate_limit_max_cooldown_s` (默认 900 秒, 0 表示不封顶), 任何一次成功响应后回到基础值. 实测上游 429 后 154 秒内重试仍被拒且不发 `Retry-After`, 固定冷却值会让每个周期都再撞一次. 冷却不重发请求, 重试仍归 SDK 与本侧回合之外.
  - **`request_stream` 必须是上下文管理器, 不是裸 async generator**: `Model.request_stream` 的契约定在基类 (`models/__init__.py::Model` 与 `models/wrapper.py::WrapperModel`) 且全部 18 个 provider 实现都带 `@asynccontextmanager`. 包装器漏掉该装饰器时, 上游一旦按契约 `async with` 进入即报 `'async_generator' object does not support the asynchronous context manager protocol (missed aexit method)` —— 会话标题 (`direct.model_request` 经 `model_request_stream`, 返回 `AbstractAsyncContextManager`) 与 agent 回合都会中招, 而错误只被 `naming.py` 吞成一行 `session title request failed`. 单测若也用 `async for` 消费, 实现与测试错得一致, 照样全绿; 故 `tests/agent/test_limits.py` 一律 `async with`, 并留 `test_request_stream_is_context_manager` 作契约守卫.
- **墙钟上限**: `agent.request_timeout` (秒) 由 `agent_agui.consume()` 以 `asyncio.timeout` 包住整个回合, 到点写超时错误行并终止; `None` 表示不设上限. **不得改用 `run_stream(timeout=)`**: 它到点抛 `RunTimedOut`, 而该类继承 `RunCancelled`, 适配器按取消处理 (发 `RUN_CANCELLED` 而非 `RUN_ERROR`), 页面会把超时显示成用户主动取消.
- **请求上限**: `agent/runtime.py::AGENT_USAGE_LIMITS` 的 `request_limit` 限制单回合请求次数. `Agent.retries` 是工具与输出校验的重试预算 (预算 `N` 对应实发 `N + 1` 次请求), 与请求上限是两道独立约束, 两者都不得取消上限.
- **单次请求超时**: 助理不传 `http_client`, 取 pydantic-ai 默认值; 翻译 60 秒、会话标题 15 秒、自检真实回合 180 秒由本侧各自设定.
- **并发**: 首条用户输入同时发起两个请求 (会话标题与回合并行), 上游按并发计量时这算两次.

## 故障后果

| 调用点 | 上游失败的结果 |
|--------|----------------|
| 助理回合 | 回合终止, `RUN_ERROR` 事件与 `error` 行进入回放, 页面按 `error` 行原文展示; 本轮用户输入仍写入 `messages.json` (见下) |
| 会话标题 | 回退首条输入截断 |
| 翻译 | 返回 `None`, 刮削保留源语言值并记 warning; 译文不写入缓存, 下次刮削仍会请求 |
| 端点自检 | 限流与凭据缺失同级判为 error, 真实回合超时只判 warn |

## 限流后的客户端状态

`RUN_ERROR` 是回合终态, 服务端不会为它补 `RUN_FINISHED` (那会把失败回合伪装成已完成). 但 `@ag-ui/client` 的未决中断 (`AbstractAgent.pendingInterrupts`) **只在 `RUN_FINISHED` 时重置**: 一次「审批 → 续批时撞 429」之后, 中断留在客户端, 而服务端已写空 `approvals` 并置回 `ACTIVE`, 两侧不一致 —— 用户再输入, 请求在发出前就被 `onInitialize` 拦下, 报 `Thread has N pending interrupt(s) not addressed by resume`. 该报错由客户端抛出, 服务端日志里没有对应记录.

对齐由前端负责, 真源是回放行 (`agent-home.tsx::syncPendingInterrupts`): 收到 `RUN_ERROR` 立即清空; 每次用回放行重建运行时话题本时按折叠结果重设 (有未决审批则按原清单重设, id 不变故续批照常). **不要在服务端为错误回合补 `RUN_FINISHED`**.

## 失败回合的历史

`on_complete` 只在回合成功时调用. 模型侧异常由 pydantic-ai 的 `UIEventStream` 经 `on_error` 转成 `RUN_ERROR` 事件, **不再向调用方重抛**, 因此失败回合的收尾不能依赖 `except`: `agent_agui.consume()` 在流正常结束且 `on_complete` 未触发时也补一次写入.

写入内容仅为**本轮用户输入** (`history + 新用户请求`). 约束:

- **不得写入半截助手消息或工具消息**: 失败可能发生在任一工具执行之后, 该步在服务端并未合成完整消息; 只补用户输入, 下一轮由模型重新生成.
- **不得省略这次写入**: 否则该轮输入既不进 `messages.json` 也不在下一轮 `history` 里, 一次限流即丢失用户已发送的内容, 模型上下文断开.
- **去重依据是用户文本**: `_new_messages` 按用户输入与服务端历史逐条比对, 裁掉已送部分; 失败轮补入的输入在重试时会被识别为历史, 不会重复发送. 修改该匹配规则须同步本约束.

取消 (任务被 `cancel` 或适配器发出 `RUN_CANCELLED`) 与超时同样补写用户输入.

## 配额与输出上限

`agent.max_tokens` 默认 128 000. 超过上游模型的输出上限时请求被直接拒绝且不重试, 接入免费或输出上限较小的模型前须下调该值.

`llm.auth_mode` 为 `guest_token` 时走免密钥上游, deviceId 持久化在 `data_dir/llm_device_id`, 有效期内复用同一 token.

## 复现手法

传输层行为可用本地端点复现: 以 `httpx2.MockTransport` 替代真实端点, 恒定返回目标状态码并统计请求次数 (见 `tests/llm/test_model.py`). 无需真实凭据, 也不产生出站流量.
