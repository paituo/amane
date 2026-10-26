"""检索服务: 关键词与向量两路的调度与融合.

三个开关正交 (见 ``config.SearchConfig``), 全关时 ``mode`` 为 ``LIKE``, 调用方应退回原有的
子串扫描, 行为与未接入检索时完全一致.

调用方可在开关已开启的范围内**收窄**: 指定本次走哪条路 (``SearchType``) 与关键词路查哪些字段
(``SearchField``). 收窄之外的部分仍由配置决定 —— 关掉的路不因请求而打开.

融合口径来自 DigUp: 余弦相似度在本次查询的候选集内归一成 z 分数, ``zgate`` 决定"语义要多突出
才算命中", 不达标的候选不参与排名而不是硬塞低分; 关键词命中另给固定加分 —— 番号、人名这类
精确实体不能被语义门控滤掉.
"""

import math
from collections.abc import Iterable, Sequence

import structlog
from pydantic import BaseModel, Field

from ..config.manager import SearchConfig, VectorEngine
from ..enums import SearchField, SearchMode, SearchType
from .embedding import EmbeddingProvider, WarmableProvider
from .store import KeywordHit, SearchStore, VectorHit

logger = structlog.get_logger()

#: 关键词路的可信排序上限. 融合时关键词路按 ``limit * 4`` 取候选, 翻页深度受它约束.
KEYWORD_WINDOW = 1000

#: 首个查询等待模型就绪的上限. 下载/加载已由配置启用时的预热提前发起,
#: 这里只兜"启用与查询几乎同时发生"的短窗口 —— 超过就降级, 不能让请求陪着下载.
READY_WAIT_S = 2.0

#: 请求模式 → 实际路径. 三个值同名, 但类型不同: 请求是意图, 响应是事实.
_REQUESTED_MODES: dict[SearchType, SearchMode] = {
    SearchType.KEYWORD: SearchMode.KEYWORD,
    SearchType.VECTOR: SearchMode.VECTOR,
    SearchType.HYBRID: SearchMode.HYBRID,
}


class SearchResult(BaseModel):
    mode: SearchMode
    ids: list[int] = Field(default_factory=list)
    scores: list[float] = Field(default_factory=list)
    """与 ``ids`` 等长的**相对相关度**, 首名恒 1.0, 其余是它相对首名的比例 (0..1).

    绝对分不可跨 mode 比较: 关键词路是 bm25 负值, 向量路是余弦, 融合路是 z 分加常数.
    调用方要"多相关"这个判断时只能看相对量, 所以统一在这里归一, 顺带让阈值有一个不随
    路径漂移的语义 (0.5 = 首名的一半分).
    """

    total: int = 0
    """``KEYWORD`` 为索引命中数; ``VECTOR`` / ``HYBRID`` 为候选窗口内的命中数, 不是全库精确计数."""

    degraded: bool = False
    """向量路本该参与但不可用 (模型/端点/扩展异常), 已退回关键词."""


def relative_scores(raw: Sequence[float]) -> list[float]:
    """把任意口径的原始分压成 0..1 相对分, 首名恒 1.0.

    原始分只保证"越大越相关", 不保证量纲: bm25 越大越好的前提是已取负, 余弦本来就是 0..1,
    融合分是 z 分加常数. 唯一跨路径稳定的是比值, 所以这里只做归一, 不试图换算成概率.
    """
    if not raw:
        return []
    top = max(raw)
    if top <= 0.0:
        # 整批都没有正向信号 —— 纯 LIKE 回退就是这种情形 (计数型分数统一给 0). 顺序本身仍有
        # 信息 (它来自检索栈的排序), 所以按名次线性衰减, 而不是全判 0: 全 0 会让阈值一次砍光.
        return [1.0 - index / (len(raw) + 1) for index in range(len(raw))]
    return [max(0.0, min(1.0, value / top)) for value in raw]


def _z_scores(values: Sequence[float]) -> list[float]:
    """总体标准化. 样本内方差为 0 时全部返回 0, 由调用方避免据此判空."""
    if not values:
        return []
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    if variance <= 0.0:
        return [0.0] * len(values)
    std = math.sqrt(variance)
    return [(v - mean) / std for v in values]


class SearchService:
    """检索入口. 组装 store 与 provider, 不持有连接生命周期以外的状态."""

    def __init__(
        self,
        *,
        store: SearchStore,
        provider: EmbeddingProvider | None,
        config: SearchConfig,
    ) -> None:
        self._store = store
        self._provider = provider
        self._config = config

    @property
    def mode(self) -> SearchMode:
        keyword = self._config.keyword_enabled
        vector = self._config.vector_enabled and self._provider is not None
        if keyword and vector:
            # 两路同时开启即混合; 是否真正融合由 ``hybrid_enabled`` 在 search 内决定.
            return SearchMode.HYBRID
        if keyword:
            return SearchMode.KEYWORD
        if vector:
            return SearchMode.VECTOR
        return SearchMode.LIKE

    def _vector_usable(self) -> bool:
        if self._config.vector_engine is VectorEngine.SQLITE_VEC:
            return self._store.vector_engine_ready
        return True

    def _degraded(self) -> bool:
        """要求走向量但实际走不了.

        ``provider`` 缺席也要算: 配置里写了 ``vector_enabled=True`` 却没接上实现,
        对外必须表现为"降级"而不是"关键词就是全部结果", 否则调用方无从发现配置没生效.
        """
        if not self._config.vector_enabled:
            return False
        return self._provider is None or not self._vector_usable()

    def _use_vec0(self) -> bool:
        return self._config.vector_engine is VectorEngine.SQLITE_VEC

    @property
    def recall_window(self) -> int:
        """可信排序的最大深度.

        向量路只保证召回窗口内的顺序; 关键词路是按 bm25 截断的, 窗口外同样没有顺序可言.
        调用方翻页深度超过它时应停下, 而不是继续往后取没有意义的顺序.
        """
        if self._config.vector_enabled:
            return max(self._config.fusion.vector_k, 1)
        return KEYWORD_WINDOW

    def _resolve(self, requested: SearchType | None) -> tuple[SearchMode, bool]:
        """请求模式与可用能力的交集.

        请求只能**收窄**不能扩张: 配置关掉的路不因请求而打开, 否则"关掉语义检索"这个开关
        会被任意调用方绕过. 请求的路走不通时退回配置模式并置 ``degraded`` —— 调用方要能区分
        "这就是关键字结果"与"想要语义但没走成".
        """
        degraded = self._degraded()
        if requested is None:
            return self.mode, degraded
        keyword = self._config.keyword_enabled
        vector = self._config.vector_enabled and not degraded
        available = {
            SearchType.KEYWORD: keyword,
            SearchType.VECTOR: vector,
            SearchType.HYBRID: keyword and vector,
        }[requested]
        if available:
            return _REQUESTED_MODES[requested], degraded
        return self.mode, True

    async def search(
        self,
        query: str,
        *,
        limit: int,
        mode: SearchType | None = None,
        fields: Sequence[SearchField] | None = None,
    ) -> SearchResult:
        """按开关组合与请求覆盖执行检索.

        ``limit`` 为返回条数上限; ``mode`` 收窄路径; ``fields`` 收窄**关键词路**的字段范围
        (``None`` 为全选). 向量路不受 ``fields`` 约束: 一条影片只有一个向量, 它是整条
         ``entry_text`` 的嵌入, 查询时无法再按字段切分.
        """
        text = query.strip()
        resolved, degraded = self._resolve(mode)
        if resolved is SearchMode.LIKE:
            return SearchResult(mode=SearchMode.LIKE, degraded=degraded)
        if not text or limit <= 0:
            return SearchResult(mode=resolved, degraded=degraded)

        # 精确番号层与关键词开关无关: 词索引始终维护, 探针只是一次受限 MATCH,
        # 而纯向量模式没有它会把同厂牌近邻排在真番号前面. 字段范围不含 number 时同样跳过.
        exact_ids = await self._exact_ids(text, limit, fields)

        keyword_hits: list[KeywordHit] = []
        if self._config.keyword_enabled:
            # 关键词路多取一些, 让融合有足够候选; 向量路的上限由 fusion.vector_k 控制.
            keyword_hits = await self._store.search_keyword(text, limit=limit * 4, fields=fields)

        if resolved is SearchMode.KEYWORD:
            ranked = self._exact_first(exact_ids, (hit.metadata_id for hit in keyword_hits))
            page = ranked[:limit]
            raw = {hit.metadata_id: hit.score for hit in keyword_hits}
            return SearchResult(
                mode=resolved,
                ids=page,
                scores=self._scores_for(page, exact_ids, raw),
                total=len(ranked),
                degraded=degraded,
            )

        vector_hits, vector_failed = await self._vector_hits(text)
        degraded = degraded or vector_failed
        if not vector_hits and vector_failed:
            ranked = self._exact_first(exact_ids, (hit.metadata_id for hit in keyword_hits))
            page = ranked[:limit]
            raw = {hit.metadata_id: hit.score for hit in keyword_hits}
            return SearchResult(
                mode=SearchMode.KEYWORD,
                ids=page,
                scores=self._scores_for(page, exact_ids, raw),
                total=len(ranked),
                degraded=True,
            )
        if resolved is SearchMode.VECTOR:
            ordered = self._exact_first(exact_ids, (hit.metadata_id for hit in vector_hits))
            page = ordered[:limit]
            raw = {hit.metadata_id: hit.similarity for hit in vector_hits}
            return SearchResult(
                mode=resolved,
                ids=page,
                scores=self._scores_for(page, exact_ids, raw),
                total=len(ordered),
                degraded=degraded,
            )
        if not self._config.fused:
            # 两路各自排序后拼接去重: 关键词结果优先, 语义结果补齐.
            merged = self._exact_first(
                exact_ids,
                (
                    *(hit.metadata_id for hit in keyword_hits),
                    *(hit.metadata_id for hit in vector_hits),
                ),
            )
            page = merged[:limit]
            raw = {hit.metadata_id: hit.similarity for hit in vector_hits}
            raw.update({hit.metadata_id: hit.score for hit in keyword_hits})
            return SearchResult(
                mode=resolved,
                ids=page,
                scores=self._scores_for(page, exact_ids, raw),
                total=len(merged),
                degraded=degraded,
            )

        fused = self._fuse(keyword_hits, vector_hits, exact_ids)
        page = fused[:limit]
        return SearchResult(
            mode=SearchMode.HYBRID,
            ids=[metadata_id for metadata_id, _ in page],
            scores=relative_scores([score for _, score in page]),
            total=len(fused),
            degraded=degraded,
        )

    @staticmethod
    def _scores_for(
        ids: Sequence[int],
        exact_ids: Sequence[int],
        raw: dict[int, float],
    ) -> list[float]:
        """按**最终顺序**取原始分再归一; 精确番号命中给一个高出全场的分数.

        精确命中是被 ``_exact_first`` 强制前移的, 若分数沿用它在关键词路里的 bm25, 就会出现
        "排第一但相对分 0.2"的自相矛盾. 给它 ceiling 既保住排序, 也让首名恒为 1.0.
        """
        exact = set(exact_ids)
        ceiling = max(raw.values(), default=0.0) + 1.0
        return relative_scores([ceiling if metadata_id in exact else raw.get(metadata_id, 0.0) for metadata_id in ids])

    async def _exact_ids(
        self,
        text: str,
        limit: int,
        fields: Sequence[SearchField] | None,
    ) -> list[int]:
        """精确番号命中集. 探针失败不影响检索, 只是少了这一层的排序优势.

        与关键词字段范围解耦: 番号精确探针是独立的标识符解析, 用户收窄到 title/plot 等文本列
        (``fields`` 不含 ``number``) 时, 关键词路不再把番号列纳入 OR 匹配, 但"按番号查"这一意图
        仍应生效. 形如番号的查询 (``plan_keyword(...).number_exact`` 非空) 始终走探针, 不受
        ``number`` 是否在 ``fields`` 中影响. 纯文本 / 中文查询没有 ASCII 词项, ``number_exact``
        为空, 探针自身也会归零, 仍按原语义跳过. 这样搜 ``sisters`` 不会因番号列前缀巧合而误命中,
        搜 ``CAWD-441`` 则能稳定命中番号.
        """
        try:
            return await self._store.exact_number_ids(text, limit=limit)
        except Exception as e:
            logger.warning("番号精确探针不可用, 本次跳过精确层", error=str(e))
            return []

    @staticmethod
    def _exact_first(exact_ids: Sequence[int], ranked: Iterable[int]) -> list[int]:
        """精确命中整体前移, 其余保持传入顺序. 去重, 不做数值比较."""
        head = list(dict.fromkeys(exact_ids))
        seen = set(head)
        return [*head, *(metadata_id for metadata_id in ranked if metadata_id not in seen)]

    async def _provider_ready(self) -> bool:
        """向量路是否可立即使用. 只有实现了预热能力的 provider 才需要等."""
        provider = self._provider
        if provider is None:
            return False
        if not isinstance(provider, WarmableProvider):
            return True
        return await provider.ensure_ready(READY_WAIT_S)

    async def _vector_hits(self, text: str) -> tuple[list[VectorHit], bool]:
        """向量召回. 任何 provider / 扩展异常都降级而不是让整个搜索失败."""
        if self._provider is None:
            return [], True
        if not self._vector_usable():
            logger.warning("向量引擎不可用, 退回关键词", engine=self._config.vector_engine)
            return [], True
        if not await self._provider_ready():
            # 预热尚未完成 (例如刚打开开关就开始搜): 有界等待已在上一步用掉, 直接降级.
            logger.warning("嵌入模型尚未就绪, 本次退回关键词")
            return [], True
        try:
            vectors = await self._provider.embed([text])
            if not vectors:
                return [], True
            return (
                await self._store.search_vector(vectors[0], k=self._config.fusion.vector_k, use_vec0=self._use_vec0()),
                False,
            )
        except Exception as e:
            # 捕获范围刻意覆盖一切: 这里是降级路径, provider 实现可能抛任意异常
            # (模型文件损坏 / 端点返回畸形 JSON / 原生库崩溃), 都不该让整个搜索 500.
            logger.warning("向量召回失败, 退回关键词", error=str(e))
            return [], True

    def _fuse(
        self,
        keyword_hits: Sequence[KeywordHit],
        vector_hits: Sequence[VectorHit],
        exact_ids: Sequence[int],
    ) -> list[tuple[int, float]]:
        """z 门控 + 词命中加分的加权融合, 回 ``[(metadata_id, 融合分)]``.

        带分数返回是为了让调用方能给出相对相关度 (阈值与"多相关"的判断), 而不是只有名次:
        名次在跨查询合并时会撞车, 分数不会.

        候选集 = 向量路召回 ∪ 关键词命中. 只在向量候选上算 z 分数, 关键词独有项没有语义分数,
        靠 ``word_weight`` 进入排名 —— 否则一个只在标题里出现过的番号会被语义路整个丢掉.

        **精确番号命中自成一层, 压在所有融合分数之前.** 理由: z 分数是样本内标准化, 上界是
        ``sqrt(n-1)``, 候选越多越大; 而 ``word_weight`` 是常数. 两者相加意味着任何固定权重在
        语料足够大时都会被语义近邻压过 —— 实测 300 条时搜 ``SSIS-001`` 排第一的已是同厂牌
        语义近邻. 番号是库的主键级标识, 用户输入它就是"要那一条", 不能参与数值比大小.

        ``exact_ids`` 由调用方传入 (来自 ``store.exact_number_ids``), 不依赖关键词开关.
        """
        zgate = self._config.fusion.zgate
        word_weight = self._config.fusion.word_weight
        exact = list(dict.fromkeys(exact_ids))
        exact_set = set(exact)
        keyword_ids = {hit.metadata_id for hit in keyword_hits}
        scores = [hit.similarity for hit in vector_hits]
        zs = _z_scores(scores)
        flat = all(z == 0.0 for z in zs)

        fused: list[tuple[float, int]] = []
        seen: set[int] = set()
        for hit, z in zip(vector_hits, zs, strict=True):
            seen.add(hit.metadata_id)
            passed = flat or z >= zgate
            word_bonus = word_weight if hit.metadata_id in keyword_ids else 0.0
            if not passed and word_bonus == 0.0:
                continue
            fused.append((max(z, 0.0) + word_bonus, hit.metadata_id))

        # 关键词独有项: 没有语义分数, 仅凭加分入列, 排在语义达标项之后由加分数值决定.
        for index, hit in enumerate(keyword_hits):
            if hit.metadata_id in seen:
                continue
            seen.add(hit.metadata_id)
            fused.append((word_weight - index * 1e-6, hit.metadata_id))

        fused.sort(key=lambda item: item[0], reverse=True)
        ordered = [(metadata_id, score) for score, metadata_id in fused if metadata_id not in exact_set]
        ceiling = max((score for score, _ in fused), default=0.0) + 1.0
        return [*((metadata_id, ceiling) for metadata_id in exact), *ordered]
