from __future__ import annotations

from enum import StrEnum
from typing import get_args

from sqlmodel import SQLModel

#: 只有片库浏览与交付会拿这些表的枚举列做条件; 其余配置表 (libraries / tasks / feeds) 的枚举
#: 写进摘要纯属占位, 白占 prompt.
_ENUM_TABLES = frozenset({"metadata", "media_files", "actors", "saved_queries"})


def table_columns() -> dict[str, list[str]]:
    """表名 → 列名 (按模型定义顺序).

    与注入 system prompt 的清单同源 (``SQLModel.metadata``), 因此错误回执里报的列名永远不会
    和模型看到的 schema 打架. 惰性求值: 建表注册发生在模型模块导入时, 不能在模块级缓存.
    """
    return {name: [column.name for column in table.columns] for name, table in SQLModel.metadata.tables.items()}


def tables_with_column(column: str) -> list[str]:
    """真正拥有该列的表名 (大小写不敏感), 供 ``no such column`` 回执指出列的真实归属."""
    target = column.lower()
    return [name for name, columns in table_columns().items() if target in {c.lower() for c in columns}]


def _str_enum_of(annotation: object) -> type[StrEnum] | None:
    """注解里若含 ``StrEnum`` 子类就取出来 (兼容 ``X | None`` 这种 Optional 写法)."""
    for candidate in (annotation, *get_args(annotation)):
        if isinstance(candidate, type) and issubclass(candidate, StrEnum):
            return candidate
    return None


def enum_columns() -> dict[tuple[str, str], list[str]]:
    """(表名, 列名) → 枚举成员名.

    枚举列在 SQLite 里存的是**成员名**而非取值 (SQLAlchemy ``Enum`` 存 ``.name``), 也就是
    ``CENSORED`` 而不是 ``censored``. ``WHERE content_type = 'censored'`` 命中 0 行且不报错 ——
    这类静默错答比写错列名更难发现, 所以把取值一并写进 schema 摘要.
    """
    found: dict[tuple[str, str], list[str]] = {}
    pending: list[type[SQLModel]] = list(SQLModel.__subclasses__())
    while pending:
        model = pending.pop()
        pending.extend(model.__subclasses__())
        table_name = getattr(model, "__tablename__", None)
        if not isinstance(table_name, str):
            continue
        for field_name, field_info in model.model_fields.items():
            if (enum_type := _str_enum_of(field_info.annotation)) is not None:
                found[(table_name, field_name)] = [member.name for member in enum_type]
    return found


def build_schema_docs(*, include_tables: frozenset[str] | None = None) -> str:
    """注入 system prompt 的表/列摘要."""
    lines: list[str] = [
        "Read-only SQLite schema (do NOT write):",
        "Primary keys are always integer column `id` unless noted.",
        "",
    ]
    tables = sorted(SQLModel.metadata.tables.items(), key=lambda x: x[0])
    for name, table in tables:
        if include_tables is not None and name not in include_tables:
            continue
        cols = ", ".join(f"{c.name}:{c.type!s}" for c in table.columns)
        lines.append(f"- {name}({cols})")
    lines.append("")
    lines.append("Join keys (FK → PK):")
    lines.append("- media_files.metadata_id → metadata.id")
    lines.append("- metadata_actors.metadata_id → metadata.id ; .actor_id → actors.id")
    lines.append("- actor_aliases.actor_id → actors.id ; actor_aliases.name is NOT unique (shared aliases)")
    lines.append("- metadata_directors.metadata_id → metadata.id ; .director_id → directors.id")
    lines.append("- metadata_tags.metadata_id → metadata.id ; .tag_id → tags.id")
    lines.append("- metadata_user_tags.metadata_id → metadata.id ; .user_tag_id → user_tags.id")
    lines.append("- actor_user_tags.actor_id → actors.id ; .user_tag_id → user_tags.id")
    lines.append("- comments.metadata_id → metadata.id")
    lines.append("Note: `metadata` PK is `id` — there is NO `metadata.metadata_id` column.")
    lines.append(
        "Note: synopsis text lives in `metadata.plot`. There is NO `metadata.description` and NO "
        "`metadata.overview` — `overview` belongs to `actors`. Only use column names listed above."
    )
    lines.append(
        "Note: `metadata_fts` / `metadata_embeddings` are internal to the search stack and are not "
        "listed here — do not query them by guessing; use search_media instead."
    )
    lines.append("")
    lines.append("Enum columns store the member NAME in UPPERCASE — `= 'censored'` matches 0 rows:")
    real_columns = table_columns()
    for (table, column), members in sorted(enum_columns().items()):
        # 只留片库浏览/交付会过滤的表: 配置表 (libraries / tasks / feeds) 的枚举写进摘要纯占位,
        # 且模型根本不会拿它们做条件. 同时过掉 ``*updaterequest`` 这类并非库里真表的模型.
        if table not in _ENUM_TABLES or table not in real_columns or column not in real_columns[table]:
            continue
        if include_tables is not None and table not in include_tables:
            continue
        lines.append(f"- {table}.{column}: {' | '.join(members)}")
    lines.append("")
    lines.append("Browse deliver targets:")
    lines.append("- entity=metadata → table `metadata`, must SELECT column `id`")
    lines.append("- entity=actor → table `actors`, must SELECT column `id`")
    return "\n".join(lines)
