"""嵌入向量 provider: 内嵌与 HTTP 两种实现, 对外同一协议, 存储格式一致.

两种实现产出的向量都以 **L2 归一化后的 float32** 入库, 因此检索时内积即余弦.
换实现或换模型只需重建向量, FTS 索引与业务表不动; 维度与模型名一并入库用于版本校验.
"""

import asyncio
import math
import struct
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import httpx2
import structlog

if TYPE_CHECKING:
    from fastembed import TextEmbedding

logger = structlog.get_logger()

#: fastembed 属可选依赖 (extra ``vector``). 缺席时本模块仍可导入, 只是内嵌实现不可构造:
#: 调用方据此把向量路降级, 而不是在模块导入阶段就把进程砸掉.
_engine_cls: type[TextEmbedding] | None
try:
    from fastembed import TextEmbedding as _engine_cls
except ImportError:  # pragma: no cover - 取决于安装的 extra
    _engine_cls = None


def fastembed_available() -> bool:
    """内嵌实现是否可用. 未安装 ``amane[vector]`` 时为 False."""
    return _engine_cls is not None


#: 向量落库的二进制格式: 小端 float32 紧密排列, 与 sqlite-vec 的 float[N] 布局一致.
#: 格式串的重复计数在类型符之前 (``<384f``), 写成 ``<f384`` 会被 struct 拒绝.
VECTOR_FORMAT = "<{}f"

#: 预热用的最小输入. 目的只是让端点在配置启用时被真的调用一次, 内容不影响判据.
_WARMUP_TEXT = "warmup"


class EmbeddingError(RuntimeError):
    """provider 不可用或返回结构不符."""


@runtime_checkable
class EmbeddingProvider(Protocol):
    """嵌入 provider. 实现须保证 ``embed`` 返回顺序与入参一致, 且每条长度等于 ``dim``."""

    @property
    def model_id(self) -> str:
        """入库标识. 与 ``dim`` 一起构成向量表的版本键, 取值变化触发重建."""
        ...

    @property
    def dim(self) -> int: ...

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class WarmableProvider(Protocol):
    """可选能力: 在"配置启用"那一刻预热, 而不是把下载/连接摊到首个搜索请求上.

    刻意不并进 ``EmbeddingProvider``: 就绪状态是实现细节, 只做嵌入的实现 (以及测试里的假件)
    不该被迫实现它. 服务侧用 ``isinstance`` 探测, 探测不到就当作"随时可用".
    """

    @property
    def ready(self) -> bool:
        """模型/端点是否已经可用. 未就绪时服务侧应降级而不是排队等."""
        ...

    async def warmup(self) -> None:
        """开始预热. 允许失败: 失败只记日志, 具体降级由 ``ensure_ready`` 判定."""
        ...

    async def ensure_ready(self, timeout: float) -> bool:
        """有界等待就绪; 超时返回 False, 调用方据此降级而不是把请求挂住."""
        ...


def l2_normalize(vector: Sequence[float]) -> list[float]:
    """归一化到单位长度; 零向量原样返回 (下游距离恒为 0, 不参与排序)."""
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0.0:
        return list(vector)
    return [x / norm for x in vector]


def pack_vector(vector: Sequence[float]) -> bytes:
    """float32 小端紧密打包."""
    return struct.pack(VECTOR_FORMAT.format(len(vector)), *vector)


def unpack_vector(blob: bytes) -> list[float]:
    """``pack_vector`` 的逆; 长度不是 4 的倍数时报错, 不做截断."""
    if len(blob) % 4 != 0:
        raise EmbeddingError(f"vector blob 长度 {len(blob)} 不是 float32 的整数倍")
    return list(struct.unpack(VECTOR_FORMAT.format(len(blob) // 4), blob))


class FastEmbedProvider:
    """进程内 ONNX 推理. 不做外部服务依赖, 代价是常驻内存.

    模型加载与推理都是 CPU 密集的同步调用, 一律经 ``asyncio.to_thread`` 让出事件循环.
    首次加载会从 HuggingFace 下载权重 (实测冷启动 53s), 因此加载由 ``warmup`` 在配置启用时
    提前发起, 而不是挂在首个查询上; 未就绪期间 ``ensure_ready`` 有界失败, 检索降级.
    """

    def __init__(
        self,
        *,
        model: str,
        dim: int,
        cache_dir: Path | None = None,
        threads: int | None = None,
    ) -> None:
        self._model_name = model
        self._dim = dim
        self._cache_dir = cache_dir
        self._threads = threads
        self._engine: TextEmbedding | None = None
        self._lock = asyncio.Lock()
        """推理锁: 保证同一时刻只有一次 embed 在跑, 避免多线程抢 CPU."""
        self._load_lock = asyncio.Lock()
        """加载锁: 与推理锁分开, 否则 embed 等加载时会与加载本身互锁."""
        self._load_task: asyncio.Task[None] | None = None

    @property
    def model_id(self) -> str:
        return f"fastembed:{self._model_name}"

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def ready(self) -> bool:
        return self._engine is not None

    def _ensure_engine(self) -> TextEmbedding:
        if self._engine is None:
            cls = _engine_cls
            if cls is None:
                raise EmbeddingError(
                    "内嵌嵌入不可用: 当前分发未包含 fastembed, 安装 amane[vector] 或改用 openai_compatible"
                )
            try:
                self._engine = cls(
                    model_name=self._model_name,
                    cache_dir=str(self._cache_dir) if self._cache_dir is not None else None,
                    threads=self._threads,
                )
            except Exception as e:
                raise EmbeddingError(f"加载内嵌嵌入模型失败: {self._model_name}: {e}") from e
            logger.info("embedding model loaded", provider="fastembed", model=self._model_name)
        return self._engine

    async def _load(self) -> None:
        """加载 (必要时下载) 权重. 失败只落日志, 由下一次 embed 自行重试."""
        async with self._load_lock:
            if self._engine is not None:
                return
            try:
                await asyncio.to_thread(self._ensure_engine)
            except Exception as e:
                # 不向上抛: 这里可能由 nobody await 的后台预热触发, 抛出去只会变成
                # "Task exception was never retrieved". 真正需要报错的是 embed 路径.
                logger.warning("嵌入模型加载失败", model=self._model_name, error=str(e))

    def _loader(self) -> asyncio.Task[None] | None:
        """共享同一个加载任务: 预热与并发查询不会各下一遍模型. ``None`` 表示已经就绪."""
        if self._engine is not None:
            return None
        task = self._load_task
        if task is None or task.done():
            task = asyncio.create_task(self._load(), name="embedding-load")
            self._load_task = task
        return task

    async def warmup(self) -> None:
        """配置启用时调用. 下载/加载在这里发生, 不在首个请求里."""
        task = self._loader()
        if task is not None:
            await task

    async def ensure_ready(self, timeout: float) -> bool:
        """有界等待首次加载; 超时返回 False, 加载任务继续跑完 (下个请求即命中)."""
        task = self._loader()
        if task is None:
            return True
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        except TimeoutError:
            logger.info("嵌入模型仍在加载, 本次检索降级", model=self._model_name, timeout=timeout)
        except Exception as e:
            logger.warning("嵌入模型加载失败, 本次检索降级", model=self._model_name, error=str(e))
        return self._engine is not None

    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        engine = self._ensure_engine()
        out = [l2_normalize([float(x) for x in vector]) for vector in engine.embed(texts)]
        for vector in out:
            if len(vector) != self._dim:
                raise EmbeddingError(f"模型 {self._model_name} 实际维度 {len(vector)} 与配置 dim={self._dim} 不符")
        return out

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        # 共享加载任务: 回填与查询并发时只加载一次. 这里不设超时 —— 调用方 (索引器/服务)
        # 自己决定能不能等, 服务侧在进入这里之前已经用 ``ensure_ready`` 判过就绪.
        task = self._loader()
        if task is not None:
            await task
        async with self._lock:
            return await asyncio.to_thread(self._embed_sync, list(texts))

    async def aclose(self) -> None:
        task, self._load_task = self._load_task, None
        if task is not None and not task.done():
            # 只放弃等待: 已经交给线程的加载停不下来, 但它不持有需要释放的资源.
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        self._engine = None


class OpenAICompatibleProvider:
    """标准 ``/v1/embeddings`` 端点. 可指向 Ollama / llama.cpp server / TEI / LM Studio 等.

    不内嵌推理也不做重试: 由调用方的任务重试策略统一处理, 此处失败即抛.
    HTTP 端点没有"下载权重"这一步, 预热只做一次最小请求, 把"地址填错 / 模型名不存在"暴露在
    启用时而不是首个查询; 失败不阻断启用, 后续 embed 仍会重试并自愈.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        dim: int,
        api_key: str | None = None,
        batch_size: int = 16,
        timeout_s: float = 30.0,
        client: httpx2.AsyncClient | None = None,
    ) -> None:
        self._model_name = model
        self._dim = dim
        self._api_key = api_key
        self._batch_size = max(1, batch_size)
        self._url = base_url.rstrip("/") + "/embeddings"
        self._client = client or httpx2.AsyncClient(timeout=httpx2.Timeout(timeout_s))
        self._owns_client = client is None
        self._ready = False

    @property
    def model_id(self) -> str:
        return f"openai:{self._model_name}"

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def ready(self) -> bool:
        """是否已成功通过一次请求验证过端点."""
        return self._ready

    async def warmup(self) -> None:
        try:
            await self._embed_batch([_WARMUP_TEXT])
        except Exception as e:
            logger.warning("嵌入端点预热失败, 首个查询会再试", url=self._url, error=str(e))
            return
        self._ready = True
        logger.info("embedding endpoint warm", provider="openai_compatible", model=self._model_name)

    async def ensure_ready(self, timeout: float) -> bool:
        """HTTP 路径不需要等待加载: 请求本身就是探测, 不再额外加一层超时预算."""
        return True

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            return {}
        return {"Authorization": f"Bearer {self._api_key}"}

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        try:
            response = await self._client.post(
                self._url,
                json={"model": self._model_name, "input": batch},
                headers=self._headers(),
            )
            response.raise_for_status()
            payload = response.json()
        except httpx2.HTTPError as e:
            raise EmbeddingError(f"嵌入端点请求失败: {self._url}: {e}") from e
        except ValueError as e:
            raise EmbeddingError(f"嵌入端点返回非 JSON: {self._url}") from e

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or len(data) != len(batch):
            raise EmbeddingError(
                f"嵌入端点返回条目数不符: 期望 {len(batch)}, 实际 {len(data) if isinstance(data, list) else 'N/A'}"
            )
        ordered = sorted(data, key=lambda item: item.get("index", 0))
        vectors: list[list[float]] = []
        for item in ordered:
            raw = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(raw, list):
                raise EmbeddingError("嵌入端点返回缺少 embedding 字段")
            vector = l2_normalize([float(x) for x in raw])
            if len(vector) != self._dim:
                raise EmbeddingError(f"嵌入端点维度 {len(vector)} 与配置 dim={self._dim} 不符")
            vectors.append(vector)
        return vectors

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        items = list(texts)
        out: list[list[float]] = []
        for start in range(0, len(items), self._batch_size):
            out.extend(await self._embed_batch(items[start : start + self._batch_size]))
        # 自愈: 预热失败后对端恢复, 这里成功一次即视为就绪.
        self._ready = True
        return out

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
