"""注入 system prompt 的 schema 摘要: 列归属与枚举取值.

摘要里的每一条都对应一次真实误用 (见 ``tests/agent/test_search_media.py`` 里 ``no such column``
的用例), 所以这里断言的是"摘要能不能挡住那个误用", 不是文案本身.
"""

from __future__ import annotations

from amane.agent.schema_docs import build_schema_docs, enum_columns, tables_with_column


def test_plot_belongs_to_metadata_and_overview_to_actors() -> None:
    """no such column 回执靠这两个归属给出真实落点."""
    assert tables_with_column("plot") == ["metadata"]
    assert tables_with_column("overview") == ["actors"]


def test_schema_docs_denies_metadata_description() -> None:
    """模型把剧情列写成 description, 摘要必须明确说没有这一列."""
    docs = build_schema_docs()

    assert "NO `metadata.description`" in docs
    assert "`metadata.plot`" in docs


def test_enum_columns_are_upper_member_names() -> None:
    """枚举列存的是成员名 (SQLAlchemy Enum 存 .name), 取值写错只会静默返回 0 行."""
    enums = enum_columns()

    assert enums[("metadata", "content_type")] == [
        "CENSORED",
        "UNCENSORED",
        "CHINESE",
        "WESTERN",
        "FC2",
        "AMATEUR",
        "HENTAI",
    ]
    assert "CENSORED" in build_schema_docs()


def test_schema_docs_enum_section_respects_include_tables() -> None:
    """裁剪表集合时枚举段不能漏出被裁掉的表."""
    docs = build_schema_docs(include_tables=frozenset({"actors"}))

    assert "- actors.gender:" in docs
    assert "metadata.content_type" not in docs
