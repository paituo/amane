"""检索索引存储: 关键词 (FTS5) 与向量 (vec0 / 应用侧内积).

存储分成两个文件, 原因是硬约束而非偏好:

- **业务库** 放 ``metadata_fts`` (FTS5 内建于 Python 的 SQLite) 与 ``metadata_embeddings`` (普通表).
- **向量库** 放 ``metadata_vec`` (vec0) —— 它是唯一需要原生扩展的表. 放在业务库里会让
  ``alembic check`` / autogenerate 在反射阶段就崩 (``no such module: vec0``), 因为 CLI 迁移连接
  不加载扩展, 而 ``include_object`` 的过滤发生在反射之后, 拦不住.

因此向量库是**可删的派生缓存** (与 ``llm.cache.TranslationCache`` 同策略): 删文件后重建即可,
不进迁移. 扩展只在向量库这条连接上加载, 加载窗口压到一条语句后立即关闭; 连接不外传,
只执行本模块硬编码的 SQL 与参数绑定.

实测边界 (勿改成"看起来更安全"的写法):
- ``sqlite3.set_authorizer`` **管不到** ``load_extension``: 全 DENY 仍可加载, 且不触发任何动作码.
  这里不设 authorizer —— 设了也挡不住, 反而会把 vec0 内部的函数调用一并拒掉.
- 真正的锁是 ``enable_load_extension(False)``; ``setconfig`` 可被 C-API 覆盖, 不构成额外保证.
"""

import asyncio
import sqlite3
import threading
from collections.abc import Sequence
from pathlib import Path

import sqlite_vec
import structlog
from pydantic import BaseModel, Field

from ..enums import SearchField
from .embedding import pack_vector, unpack_vector
from .tokenizer import (
    TEXT_FIELDS,
    index_number_tokens,
    index_text_tokens,
    like_columns,
    plan_keyword,
)

logger = structlog.get_logger()

BUSY_TIMEOUT_MS = 30_000

#: 向量库文件名; 与业务库同目录.
VECTOR_DB_NAME = "search_vec.db"

#: bm25 列权重, 顺序必须与 ``SearchField`` 声明顺序一致.
#: 番号最重 (标识符), 实体次之, 简介最轻 (长文本容易靠词频占优).
_BM25_WEIGHTS: dict[SearchField, float] = {
    SearchField.NUMBER: 8.0,
    SearchField.TITLE: 3.0,
    SearchField.PLOT: 1.0,
    SearchField.ACTORS: 6.0,
    SearchField.TAGS: 6.0,
    SearchField.SERIES: 5.0,
    SearchField.STUDIO: 5.0,
    SearchField.PUBLISHER: 5.0,
}
_BM25_ARGS = ", ".join(f"{_BM25_WEIGHTS[f]:g}" for f in SearchField)

_FTS_TABLE = "metadata_fts"
_EMBEDDING_TABLE = "metadata_embeddings"
_VEC_TABLE = "metadata_vec"

#: 索引表名对外暴露. 自检探针要用只读连接数这些表; 把字面量在那里再抄一遍, 重命名时必然漏改一侧.
INDEX_TABLES: dict[str, str] = {
    "keyword": _FTS_TABLE,
    "embedding": _EMBEDDING_TABLE,
    "vector": _VEC_TABLE,
}


class IndexEntry(BaseModel):
    """一条待索引记录. 向量为空时只更新关键词索引."""

    metadata_id: int = Field(gt=0)
    number: str = ""
    title: str | None = None
    plot: str | None = None
    actors: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    series: str | None = None
    studio: str | None = None
    publisher: str | None = None
    embedding: list[float] | None = None


class KeywordHit(BaseModel):
    metadata_id: int
    score: float


class VectorHit(BaseModel):
    metadata_id: int
    similarity: float


class IndexStats(BaseModel):
    keyword_docs: int = 0
    vector_docs: int = 0
    vec_index_docs: int = 0
    """vec0 里的条目数; 小于 ``vector_docs`` 说明向量索引需重建."""

    vector_model: str | None = None
    vector_dim: int | None = None


class SearchStoreError(RuntimeError):
    """索引不可用或结构不符."""


def _column_text(value: str | Sequence[str] | None) -> str:
    """多值字段按空格拼接; 值本身含的空白不影响 bigram 切分."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return " ".join(value)


def _fts_row(entry: IndexEntry) -> tuple[str, ...]:
    numbers = " ".join(index_number_tokens(entry.number))
    texts = [" ".join(index_text_tokens(_column_text(getattr(entry, field.value)))) for field in TEXT_FIELDS]
    return (numbers, *texts)


class SearchStore:
    """关键词与向量索引的唯一入口. 所有方法在工作线程执行, 对外即异步."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        dim: int,
        model: str,
        vector_db_path: Path | str | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._vector_db_path = Path(vector_db_path) if vector_db_path else self._db_path.with_name(VECTOR_DB_NAME)
        self._dim = dim
        self._model = model
        self._conn: sqlite3.Connection | None = None
        self._vec_conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._vec_loaded = False

    # ---------------------------------------------------------------- 生命周期

    async def open(self) -> None:
        await asyncio.to_thread(self._open_sync)

    def _open_sync(self) -> None:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        self._conn = conn
        self._ensure_keyword_tables()
        self._open_vector_db()
        logger.info(
            "search store ready",
            path=str(self._db_path),
            vector_db=str(self._vector_db_path),
            dim=self._dim,
            vec=self._vec_loaded,
        )

    def _open_vector_db(self) -> None:
        """向量库独立连接; 扩展加载失败不致命, 调用方可退回应用侧内积."""
        vec_conn = sqlite3.connect(self._vector_db_path, check_same_thread=False)
        vec_conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        vec_conn.execute("PRAGMA journal_mode=WAL")
        vec_conn.execute("PRAGMA synchronous=NORMAL")
        vec_conn.enable_load_extension(True)
        try:
            vec_conn.load_extension(sqlite_vec.loadable_path())
            self._vec_loaded = True
        except sqlite3.Error as e:
            self._vec_loaded = False
            logger.warning("sqlite-vec 加载失败, 向量引擎需退回应用侧", error=str(e))
        finally:
            vec_conn.enable_load_extension(False)
        self._vec_conn = vec_conn
        self._ensure_vector_table()

    def _ensure_keyword_tables(self) -> None:
        conn = self._require_conn()
        columns = ", ".join(f.value for f in SearchField)
        # contentless_delete 省掉原文副本, 同时允许按 rowid 删除旧版本.
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {_FTS_TABLE} USING fts5({columns}, content='', contentless_delete=1)"
        )
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_EMBEDDING_TABLE} ("
            "metadata_id INTEGER PRIMARY KEY, model TEXT NOT NULL, dim INTEGER NOT NULL, "
            "vector BLOB NOT NULL)"
        )
        conn.commit()

    def _existing_vec_dim(self) -> int | None:
        vec_conn = self._require_vec_conn()
        row = vec_conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (_VEC_TABLE,)
        ).fetchone()
        if row is None or not row[0]:
            return None
        # DDL 形如 vec0(embedding float[384] distance_metric=cosine)
        sql = str(row[0])
        start = sql.find("float[")
        end = sql.find("]", start)
        if start < 0 or end < 0:
            return None
        try:
            return int(sql[start + len("float[") : end])
        except ValueError:
            return None

    def _ensure_vector_table(self) -> None:
        """vec0 是派生索引: 维度变化时整表重建, 由调用方的回填任务重算."""
        if not self._vec_loaded:
            return
        vec_conn = self._require_vec_conn()
        existing = self._existing_vec_dim()
        if existing is not None and existing != self._dim:
            vec_conn.execute(f"DROP TABLE IF EXISTS {_VEC_TABLE}")
            logger.warning("向量维度变化, 已丢弃向量索引并等待回填", was=existing, now=self._dim)
        vec_conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {_VEC_TABLE} USING vec0("
            f"embedding float[{self._dim}] distance_metric=cosine)"
        )
        vec_conn.commit()

    @property
    def vector_engine_ready(self) -> bool:
        """vec0 是否可用. 为假时调用方应改用应用侧内积."""
        return self._vec_loaded

    @property
    def model(self) -> str:
        """构造时的向量模型标识; 实际写入可用 ``upsert(model=...)`` 覆盖."""
        return self._model

    async def aclose(self) -> None:
        await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        with self._lock:
            for conn in (self._conn, self._vec_conn):
                if conn is not None:
                    conn.close()
            self._conn = None
            self._vec_conn = None

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise SearchStoreError("索引连接未初始化")
        return self._conn

    def _require_vec_conn(self) -> sqlite3.Connection:
        if self._vec_conn is None:
            raise SearchStoreError("向量库连接未初始化")
        return self._vec_conn

    # ---------------------------------------------------------------- 写入

    async def upsert(self, entries: Sequence[IndexEntry], *, model: str | None = None) -> int:
        """``model`` 是产出入库向量的模型标识; 缺省用构造时的那个.

        换模型后必须先 ``drop_vectors()``, 否则同表里会混两个模型的向量 —— 它们不在同一空间,
        内积比较没有意义.
        """
        if not entries:
            return 0
        return await asyncio.to_thread(self._upsert_sync, list(entries), model or self._model)

    def _upsert_sync(self, entries: list[IndexEntry], model: str) -> int:
        conn = self._require_conn()
        columns = ", ".join(f.value for f in SearchField)
        placeholders = ", ".join("?" * (len(SearchField) + 1))
        with self._lock:
            try:
                for entry in entries:
                    conn.execute(f"DELETE FROM {_FTS_TABLE} WHERE rowid = ?", (entry.metadata_id,))
                    conn.execute(
                        f"INSERT INTO {_FTS_TABLE}(rowid, {columns}) VALUES ({placeholders})",
                        (entry.metadata_id, *_fts_row(entry)),
                    )
                    if entry.embedding is None:
                        continue
                    if len(entry.embedding) != self._dim:
                        raise SearchStoreError(f"向量维度 {len(entry.embedding)} 与索引 dim={self._dim} 不符")
                    blob = pack_vector(entry.embedding)
                    conn.execute(
                        f"INSERT OR REPLACE INTO {_EMBEDDING_TABLE}(metadata_id, model, dim, vector) "
                        "VALUES (?, ?, ?, ?)",
                        (entry.metadata_id, model, self._dim, blob),
                    )
                conn.commit()
            except sqlite3.Error as e:
                conn.rollback()
                raise SearchStoreError(f"索引写入失败: {e}") from e
        # vec0 是派生索引, 单独提交: 写失败只影响 KNN, 由重建修复, 不回滚已落库的向量.
        try:
            self._upsert_vec(entries)
        except sqlite3.Error as e:
            logger.warning("vec0 写入失败, 索引待重建", error=str(e))
        return len(entries)

    def _upsert_vec(self, entries: list[IndexEntry]) -> None:
        if not self._vec_loaded:
            return
        vec_conn = self._require_vec_conn()
        for entry in entries:
            if entry.embedding is None:
                continue
            vec_conn.execute(f"DELETE FROM {_VEC_TABLE} WHERE rowid = ?", (entry.metadata_id,))
            vec_conn.execute(
                f"INSERT INTO {_VEC_TABLE}(rowid, embedding) VALUES (?, ?)",
                (entry.metadata_id, pack_vector(entry.embedding)),
            )
        vec_conn.commit()

    async def remove(self, metadata_ids: Sequence[int]) -> int:
        if not metadata_ids:
            return 0
        return await asyncio.to_thread(self._remove_sync, list(metadata_ids))

    def _remove_sync(self, ids: list[int]) -> int:
        conn = self._require_conn()
        with self._lock:
            for metadata_id in ids:
                conn.execute(f"DELETE FROM {_FTS_TABLE} WHERE rowid = ?", (metadata_id,))
                conn.execute(f"DELETE FROM {_EMBEDDING_TABLE} WHERE metadata_id = ?", (metadata_id,))
            conn.commit()
            if self._vec_loaded:
                vec_conn = self._require_vec_conn()
                for metadata_id in ids:
                    vec_conn.execute(f"DELETE FROM {_VEC_TABLE} WHERE rowid = ?", (metadata_id,))
                vec_conn.commit()
        return len(ids)

    # ---------------------------------------------------------------- 查询

    async def search_keyword(
        self,
        query: str,
        *,
        limit: int,
        fields: Sequence[SearchField] | None = None,
    ) -> list[KeywordHit]:
        if limit <= 0:
            return []
        return await asyncio.to_thread(self._search_keyword_sync, query, limit, fields)

    def _search_keyword_sync(
        self,
        query: str,
        limit: int,
        fields: Sequence[SearchField] | None,
    ) -> list[KeywordHit]:
        plan = plan_keyword(query, fields)
        if plan.is_empty:
            return []
        if plan.like_fallback is not None:
            return self._like_fallback(plan.like_fallback, limit, like_columns(fields))
        for tier in plan.tiers:
            rows = self._match_rows(tier, limit)
            if rows:
                # 这里只给 bm25 顺序; "精确番号压最前"由 service 用 exact_number_ids 统一加,
                # 因为纯向量模式也需要那一层.
                return rows
        return []

    async def exact_number_ids(self, query: str, *, limit: int) -> list[int]:
        """标识符精确命中的 id.

        单独成方法而不是塞进 ``search_keyword``: FTS5 不回报命中了哪一列, 只能另跑一次
        归一原子前缀探针; 而这条信息对**向量路同样必要** (纯语义会把同厂牌近邻排在真番号前面),
        所以调用方必须在关键词开关之外也能拿到它. 探针本身是一次受限 MATCH, 代价可以忽略.
        """
        if limit <= 0:
            return []
        return await asyncio.to_thread(self._exact_number_ids_sync, query, limit)

    def _exact_number_ids_sync(self, query: str, limit: int) -> list[int]:
        number_exact = plan_keyword(query).number_exact
        if number_exact is None:
            return []
        conn = self._require_conn()
        try:
            cursor = conn.execute(
                f"SELECT rowid FROM {_FTS_TABLE} WHERE {_FTS_TABLE} MATCH ? "
                f"ORDER BY bm25({_FTS_TABLE}, {_BM25_ARGS}) LIMIT ?",
                (number_exact, limit),
            )
        except sqlite3.OperationalError as e:
            logger.warning("番号精确探针失败", match=number_exact, error=str(e))
            return []
        return [row[0] for row in cursor]

    def _match_rows(self, match_expr: str, limit: int) -> list[KeywordHit]:
        conn = self._require_conn()
        try:
            cursor = conn.execute(
                f"SELECT rowid, bm25({_FTS_TABLE}, {_BM25_ARGS}) FROM {_FTS_TABLE} "
                f"WHERE {_FTS_TABLE} MATCH ? ORDER BY bm25({_FTS_TABLE}, {_BM25_ARGS}) LIMIT ?",
                (match_expr, limit),
            )
        except sqlite3.OperationalError as e:
            # MATCH 语法由本模块生成; 出错说明表达式构造有缺陷, 记日志而不是把异常抛给请求.
            logger.warning("关键词 MATCH 失败", match=match_expr, error=str(e))
            return []
        # SQLite 的 bm25 越小越相关, 取负得到"越大越好"的分数.
        return [KeywordHit(metadata_id=row[0], score=-float(row[1])) for row in cursor]

    def _like_fallback(
        self,
        needle: str,
        limit: int,
        columns: Sequence[SearchField],
    ) -> list[KeywordHit]:
        """单字等 FTS5 无法表达的查询回退子串扫描.

        只扫 ``columns`` (调用方按已选范围给出); 计数型分数没有意义, 统一给 0,
        由融合侧按"词命中"处理.
        """
        if not columns:
            return []
        conn = self._require_conn()
        pattern = f"%{needle}%"
        clauses = " OR ".join(f"{column.value} LIKE ?" for column in columns)
        try:
            cursor = conn.execute(
                f"SELECT id FROM metadata WHERE {clauses} LIMIT ?",
                (*([pattern] * len(columns)), limit),
            )
        except sqlite3.OperationalError as e:
            logger.warning("LIKE 回退失败", error=str(e))
            return []
        return [KeywordHit(metadata_id=row[0], score=0.0) for row in cursor]

    async def search_vector(self, vector: Sequence[float], *, k: int, use_vec0: bool) -> list[VectorHit]:
        if k <= 0 or not vector:
            return []
        return await asyncio.to_thread(self._search_vector_sync, list(vector), k, use_vec0)

    def _search_vector_sync(self, vector: list[float], k: int, use_vec0: bool) -> list[VectorHit]:
        if len(vector) != self._dim:
            raise SearchStoreError(f"查询向量维度 {len(vector)} 与索引 dim={self._dim} 不符")
        if use_vec0 and self._vec_loaded:
            return self._search_vector_vec0(vector, k)
        return self._search_vector_python(vector, k)

    def _search_vector_vec0(self, vector: list[float], k: int) -> list[VectorHit]:
        vec_conn = self._require_vec_conn()
        rows = vec_conn.execute(
            f"SELECT rowid, distance FROM {_VEC_TABLE} WHERE embedding MATCH ? AND k = ?",
            (pack_vector(vector), k),
        ).fetchall()
        # distance_metric=cosine 下 distance = 1 - cos.
        return [VectorHit(metadata_id=row[0], similarity=1.0 - float(row[1])) for row in rows]

    def _search_vector_python(self, vector: list[float], k: int) -> list[VectorHit]:
        """应用侧内积. 向量已归一化, 内积即余弦; 全表载入, 规模上限见 docs/dev/search.md."""
        conn = self._require_conn()
        scored: list[VectorHit] = []
        cursor = conn.execute(f"SELECT metadata_id, vector FROM {_EMBEDDING_TABLE}")
        for metadata_id, blob in cursor:
            stored = unpack_vector(blob)
            if len(stored) != self._dim:
                continue
            scored.append(
                VectorHit(metadata_id=metadata_id, similarity=sum(a * b for a, b in zip(vector, stored, strict=True)))
            )
        scored.sort(key=lambda hit: hit.similarity, reverse=True)
        return scored[:k]

    async def stats(self) -> IndexStats:
        return await asyncio.to_thread(self._stats_sync)

    def _stats_sync(self) -> IndexStats:
        conn = self._require_conn()
        keyword_docs = conn.execute(f"SELECT count(*) FROM {_FTS_TABLE}").fetchone()[0]
        row = conn.execute(f"SELECT count(*), max(model), max(dim) FROM {_EMBEDDING_TABLE}").fetchone()
        vec_docs = 0
        if self._vec_loaded:
            vec_docs = self._require_vec_conn().execute(f"SELECT count(*) FROM {_VEC_TABLE}").fetchone()[0]
        return IndexStats(
            keyword_docs=int(keyword_docs),
            vector_docs=int(row[0]),
            vec_index_docs=int(vec_docs),
            vector_model=row[1],
            vector_dim=int(row[2]) if row[2] is not None else None,
        )

    # ---------------------------------------------------------------- 维护

    async def indexed_ids(self) -> set[int]:
        """已进关键词索引的 id 集合, 供回填做差集."""
        return await asyncio.to_thread(self._indexed_ids_sync)

    def _indexed_ids_sync(self) -> set[int]:
        conn = self._require_conn()
        return {int(row[0]) for row in conn.execute(f"SELECT rowid FROM {_FTS_TABLE}")}

    async def missing_vector_ids(self) -> set[int]:
        """有词索引但没有向量的 id. 索引落后或上次嵌入中断时非空."""
        return await asyncio.to_thread(self._missing_vector_ids_sync)

    def _missing_vector_ids_sync(self) -> set[int]:
        conn = self._require_conn()
        indexed = {int(row[0]) for row in conn.execute(f"SELECT rowid FROM {_FTS_TABLE}")}
        embedded = {int(row[0]) for row in conn.execute(f"SELECT metadata_id FROM {_EMBEDDING_TABLE}")}
        return indexed - embedded

    async def drop_vectors(self) -> None:
        """丢弃全部向量与 vec0 索引, 保留词索引. 模型或维度变化后由回填重算."""
        await asyncio.to_thread(self._drop_vectors_sync)

    def _drop_vectors_sync(self) -> None:
        conn = self._require_conn()
        with self._lock:
            conn.execute(f"DELETE FROM {_EMBEDDING_TABLE}")
            conn.commit()
        if not self._vec_loaded:
            return
        # vec0 只支持按 rowid 删除, 整表清空走 DROP + 重建.
        vec_conn = self._require_vec_conn()
        with self._lock:
            vec_conn.execute(f"DROP TABLE IF EXISTS {_VEC_TABLE}")
            vec_conn.commit()
        self._ensure_vector_table()
        logger.info("向量索引已清空, 等待回填")
