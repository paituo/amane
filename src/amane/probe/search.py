"""向量库自检: 开关组合、引擎、模型、维度与索引规模.

维度不符是这里最贵的一类错误 —— 它不会在保存配置时报错, 只在第一次写入或检索时炸,
而且表现为"搜不到东西"而不是一条清晰的异常. 所以探针必须真的算一条向量出来比对.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import structlog

from ..app.runtime import build_embedding_provider
from ..config import EmbeddingProviderName, VectorEngine
from ..search.embedding import (
    EmbeddingProvider,
    WarmableProvider,
    fastembed_available,
)
from ..search.store import INDEX_TABLES, VECTOR_DB_NAME
from .base import ProbeContext, ProbeEvent, describe_error

logger = structlog.get_logger()

_LOAD_TIMEOUT_S = 120.0
"""等模型就绪的上限. 首次下权重要几十秒 (实测 ~53s), 留一倍余量再判失败."""

_PROBE_TEXTS = ["SSIS-001 检索探针", "検索テスト"]


@dataclass(frozen=True, slots=True)
class _IndexSnapshot:
    keyword_docs: int | None
    embeddings: int | None
    vectors: int | None
    stored_model: str | None
    stored_dim: int | None


def _connect_ro(path: Path) -> sqlite3.Connection:
    """只读连接. 探针绝不建表: 配置还没保存时不该在库里留下任何痕迹."""
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}


def _read_index(db_path: Path) -> _IndexSnapshot | None:
    """读索引规模与版本键. 库或表不存在返回 ``None`` (首次使用尚未建索引)."""
    if not db_path.exists():
        return None
    try:
        conn = _connect_ro(db_path)
    except sqlite3.Error:
        return None
    try:
        names = _table_names(conn)
        keyword_docs = (
            int(conn.execute(f"SELECT count(*) FROM {INDEX_TABLES['keyword']}").fetchone()[0])
            if INDEX_TABLES["keyword"] in names
            else None
        )
        embeddings: int | None = None
        stored_model: str | None = None
        stored_dim: int | None = None
        if INDEX_TABLES["embedding"] in names:
            row = conn.execute(f"SELECT count(*), max(model), max(dim) FROM {INDEX_TABLES['embedding']}").fetchone()
            embeddings = int(row[0])
            stored_model = str(row[1]) if row[1] is not None else None
            stored_dim = int(row[2]) if row[2] is not None else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    vectors: int | None = None
    vec_path = db_path.with_name(VECTOR_DB_NAME)
    if vec_path.exists():
        try:
            vec_conn = _connect_ro(vec_path)
        except sqlite3.Error:
            vec_conn = None
        if vec_conn is not None:
            try:
                if INDEX_TABLES["vector"] in _table_names(vec_conn):
                    vectors = int(vec_conn.execute(f"SELECT count(*) FROM {INDEX_TABLES['vector']}").fetchone()[0])
            except sqlite3.Error:
                vectors = None
            finally:
                vec_conn.close()

    return _IndexSnapshot(
        keyword_docs=keyword_docs,
        embeddings=embeddings,
        vectors=vectors,
        stored_model=stored_model,
        stored_dim=stored_dim,
    )


def _vec_extension_version() -> str | None:
    """``sqlite-vec`` 是否真能加载. 装了 wheel 但 DLL 缺依赖也会在这里暴露."""
    try:
        import sqlite_vec
    except ImportError:
        return None
    conn = sqlite3.connect(":memory:")
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        return str(conn.execute("SELECT vec_version()").fetchone()[0])
    except Exception:
        return None
    finally:
        conn.close()


def _acquire_provider(ctx: ProbeContext) -> tuple[EmbeddingProvider | None, bool]:
    """拿一个可用的 provider, 返回 (provider, 是否归本探针所有).

    能复用运行中的实例就复用: 换一种融合参数不该为了点一次测试再加载一遍模型.
    复用判据同时看"表单值"和"已落盘值" —— 只有两者相同, 运行中那个实例才是按同一份配置造的.
    """
    stack = ctx.runtime.search if ctx.runtime is not None else None
    if (
        stack is not None
        and stack.provider is not None
        and stack.embedding_key == ctx.hot.search.embedding
        and ctx.saved.search.embedding == ctx.hot.search.embedding
    ):
        return stack.provider, False
    return build_embedding_provider(ctx.hot, ctx.data_dir / "models"), True


def _flag(enabled: bool) -> str:
    return "开" if enabled else "关"


async def probe_search(ctx: ProbeContext) -> AsyncIterator[ProbeEvent]:
    cfg = ctx.hot.search
    rec = ctx.rec
    yield rec.info(
        "检索配置",
        detail=(
            f"关键词 {_flag(cfg.keyword_enabled)} · 向量 {_flag(cfg.vector_enabled)} · "
            f"融合 {_flag(cfg.hybrid_enabled)} · 引擎 {cfg.vector_engine}"
        ),
    )

    if not cfg.keyword_enabled and not cfg.vector_enabled:
        yield rec.warn(
            "关键词与向量都关闭",
            detail="检索会退回 LIKE 全表匹配; 功能可用, 但没有索引加速与语义召回",
        )
        return

    if cfg.vector_engine is VectorEngine.SQLITE_VEC:
        version = _vec_extension_version()
        if version is None:
            yield rec.warn(
                "sqlite-vec 扩展不可用",
                detail="向量检索会回退到应用侧内积 (结果等价, 大库更慢); 想用扩展请确认 sqlite_vec 可加载",
            )
        else:
            yield rec.ok(f"sqlite-vec 可用 (vec_version {version})")
    else:
        yield rec.info("引擎为 python", detail="应用侧内积, 不依赖原生扩展")

    snapshot = _read_index(ctx.db_path)
    if snapshot is None:
        yield rec.info("尚无索引库", detail="首次写入影片时会自动建立; 现在没有可校验的数据")
    else:
        keyword_size = f"{snapshot.keyword_docs} 条" if snapshot.keyword_docs is not None else "未建立"
        vector_size = f"{snapshot.vectors} 条" if snapshot.vectors is not None else "未建立"
        yield rec.info("索引规模", detail=f"词索引 {keyword_size} · 向量 {vector_size}")

    if not cfg.vector_enabled:
        yield rec.ok("向量路关闭, 不需要检查嵌入", detail="词索引在关闭时照常维护, 打开后可立即使用")
        return

    embedding = cfg.embedding
    yield rec.info(
        f"嵌入实现 {embedding.provider}",
        detail=f"模型 {embedding.model} · 配置维度 {embedding.dim}",
    )

    if embedding.provider is EmbeddingProviderName.OPENAI_COMPATIBLE:
        parts = urlsplit(embedding.base_url)
        if not parts.hostname or parts.scheme not in {"http", "https"}:
            yield rec.error(
                "嵌入端点地址不合法",
                detail=f"{embedding.base_url!r} 不是 http(s) URL; 实际请求会打到 {embedding.base_url}/embeddings",
            )
            return
        yield rec.ok(f"端点 {embedding.base_url}/embeddings")
    elif not fastembed_available():
        yield rec.error(
            "内嵌嵌入不可用: 未安装 fastembed",
            detail="当前分发没带 amane[vector]; 装上 extra 或把 provider 改成 openai_compatible",
        )
        return

    provider, owned = _acquire_provider(ctx)
    if provider is None:
        yield rec.error("嵌入 provider 未构造", detail="向量路已启用却拿不到实现, 检查上面的实现选择")
        return
    # 先取标识: 自有 provider 在 finally 里就关掉了, 之后不该再摸它的属性.
    model_id = provider.model_id

    try:
        if isinstance(provider, WarmableProvider) and not provider.ready:
            yield rec.info("加载模型", detail="首次会下载权重, 可能耗时数十秒; 期间不要关闭对话框")
            if not await provider.ensure_ready(_LOAD_TIMEOUT_S):
                yield rec.error(
                    f"模型在 {_LOAD_TIMEOUT_S:.0f}s 内未就绪", detail="稍后重试; 未就绪期间检索按 degraded 降级"
                )
                return
            yield rec.ok("模型已就绪")
        elif not owned:
            yield rec.ok("复用运行中的模型实例", detail="未重复加载权重")
        else:
            yield rec.ok("模型已就绪")

        try:
            vectors = await provider.embed(_PROBE_TEXTS)
        except Exception as e:
            yield rec.error("嵌入失败", detail=describe_error(e))
            return

        dims = {len(vector) for vector in vectors}
        if not vectors:
            yield rec.error("嵌入返回空结果", detail="provider 没有报错但没有产出向量")
            return
        if dims != {embedding.dim}:
            actual = "/".join(str(dim) for dim in sorted(dims))
            yield rec.error(
                f"维度不符: 配置 {embedding.dim}, 实际 {actual}",
                detail="维度写错时写入被拒、检索无结果; 改成模型的实际维度后需回填重建",
            )
            return
        yield rec.ok(f"嵌入成功, 维度 {embedding.dim} 与配置一致")
    finally:
        if owned:
            await provider.aclose()

    if snapshot is not None:
        if snapshot.vectors == 0:
            yield rec.warn("向量表为空", detail="打开向量后会全量回填; 回填完成前语义检索没有结果")
        if snapshot.stored_model is not None and snapshot.stored_model != model_id:
            yield rec.warn(
                "索引里的模型与当前配置不同",
                detail=f"已存 {snapshot.stored_model} → 现在 {model_id}; 保存后会自动重建向量",
            )
        elif snapshot.stored_dim is not None and snapshot.stored_dim != embedding.dim:
            yield rec.warn(
                "索引里的维度与当前配置不同",
                detail=f"已存 {snapshot.stored_dim} → 现在 {embedding.dim}; 保存后会自动重建向量",
            )
