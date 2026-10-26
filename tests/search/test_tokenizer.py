"""分词与查询规划: 索引端切分口径、查询分级、以及 FTS5 短语等价子串语义."""

from __future__ import annotations

import sqlite3

import pytest

from amane.enums import SearchField
from amane.search.tokenizer import (
    index_number_tokens,
    index_text_tokens,
    normalize_number,
    plan_keyword,
    selected_fields,
)

#: (输入, 期望归一形)
NORMALIZE_CASES: list[tuple[str, str]] = [
    ("SSIS-001", "ssis001"),
    ("ssis 001", "ssis001"),
    ("SSIS001", "ssis001"),
    ("FC2-PPV-1234567", "fc2ppv1234567"),
    ("010120-001", "010120001"),
    ("", ""),
    ("-_-", ""),
    ("ＡＢＰ-１２３", "abp123"),
]

#: (输入, 期望 token 流). 番号原子在前, 分段词在后.
NUMBER_TOKEN_CASES: list[tuple[str, list[str]]] = [
    ("SSIS-001", ["ssis001", "ssis", "001"]),
    ("FC2-PPV-1234567", ["fc2ppv1234567", "fc", "2", "ppv", "1234567"]),
    ("", []),
    ("----", []),
]

#: (输入, 期望 token 流). CJK 出 bigram, ASCII 出整词.
TEXT_TOKEN_CASES: list[tuple[str, list[str]]] = [
    ("人妻", ["人妻"]),
    ("巨乳人妻", ["巨乳", "乳人", "人妻"]),
    ("海", ["海"]),
    ("看護師 SSIS-001", ["看護", "護師", "ssis", "001"]),
    ("Julia", ["julia"]),
    ("", []),
    ("！？、。", []),
]


@pytest.mark.parametrize(("raw", "expected"), NORMALIZE_CASES)
def test_normalize_number(raw: str, expected: str) -> None:
    assert normalize_number(raw) == expected


@pytest.mark.parametrize(("raw", "expected"), NUMBER_TOKEN_CASES)
def test_index_number_tokens(raw: str, expected: list[str]) -> None:
    assert index_number_tokens(raw) == expected


@pytest.mark.parametrize(("raw", "expected"), TEXT_TOKEN_CASES)
def test_index_text_tokens(raw: str, expected: list[str]) -> None:
    assert index_text_tokens(raw) == expected


def test_token_field_order_matches_fts_columns() -> None:
    """bm25 权重按声明顺序对齐, 顺序变化会静默错位."""
    assert [f.value for f in SearchField] == [
        "number",
        "title",
        "plot",
        "actors",
        "tags",
        "series",
        "studio",
        "publisher",
    ]


#: (查询, 期望 like_fallback, 期望分级数)
PLAN_CASES: list[tuple[str, str | None, int]] = [
    ("SSIS-001", None, 2),
    ("人妻", None, 1),
    ("海", "海", 0),
    ("", None, 0),
    ("   ", None, 0),
    ("新人 看護師", None, 1),
    ("看護師 SSIS-001", None, 2),
    ("julia", None, 2),
]


@pytest.mark.parametrize(("query", "like_fallback", "tiers"), PLAN_CASES)
def test_plan_keyword(query: str, like_fallback: str | None, tiers: int) -> None:
    plan = plan_keyword(query)
    assert plan.like_fallback == like_fallback
    assert len(plan.tiers) == tiers


def test_plan_keyword_quotes_dash_term() -> None:
    """裸 ``SSIS-001`` 会被 FTS5 当成列引用, 词项必须带引号."""
    plan = plan_keyword("SSIS-001")
    assert '"ssis001"' in plan.tiers[0]


def test_plan_keyword_splits_quote_into_separate_terms() -> None:
    """引号不进词项: 它在切片阶段就被当作分隔符丢掉, 不会截断 FTS5 字面量."""
    plan = plan_keyword('ab"cd')
    assert '"ab"' in plan.tiers[0]
    assert '"cd"' in plan.tiers[0]
    assert '"ab"cd"' not in plan.tiers[0]


def test_selected_fields_keeps_declared_order() -> None:
    """列过滤的写法与传入顺序无关, 否则同一组字段会因勾选先后产生不同 SQL."""
    assert selected_fields(None) == tuple(SearchField)
    assert selected_fields([SearchField.PLOT, SearchField.TITLE]) == (SearchField.TITLE, SearchField.PLOT)


#: (查询, 字段范围, 期望出现在列过滤里的列, 期望 like_fallback)
FIELD_SCOPE_CASES: list[tuple[str, list[SearchField], str | None, str | None]] = [
    ("人妻", [SearchField.TITLE], "{title}", None),
    ("人妻", [SearchField.TITLE, SearchField.PLOT], "{title plot}", None),
    ("SSIS-001", [SearchField.TITLE], "{title}", None),
    ("SSIS-001", [SearchField.NUMBER], "{number}", None),
    # 一个可用通道都不剩: 空计划, 不退化成全字段扫描.
    ("人妻", [SearchField.NUMBER], None, None),
    # 单字回退只扫已选文本列; 缺省范围与接入字段范围之前一致 (四个自由文本列).
    ("海", [SearchField.TITLE], None, "海"),
    # 显式只勾实体列时, 回退就落在实体列上, 而不是静默 0 命中.
    ("海", [SearchField.SERIES], None, "海"),
    # 只勾 number 没有可扫的文本列: 空计划.
    ("海", [SearchField.NUMBER], None, None),
]


@pytest.mark.parametrize(("query", "fields", "scope", "like_fallback"), FIELD_SCOPE_CASES)
def test_plan_keyword_respects_field_scope(
    query: str,
    fields: list[SearchField],
    scope: str | None,
    like_fallback: str | None,
) -> None:
    plan = plan_keyword(query, fields)
    assert plan.like_fallback == like_fallback
    if scope is None:
        assert plan.tiers == []
        return
    assert scope in plan.tiers[0]


def test_plan_keyword_drops_number_channel_when_field_excluded() -> None:
    """number 通道与精确探针都随该字段一起消失."""
    assert plan_keyword("SSIS-001").number_exact is not None
    assert plan_keyword("SSIS-001", [SearchField.TITLE]).number_exact is None
    assert "{number}" not in plan_keyword("SSIS-001", [SearchField.TITLE]).tiers[0]


def test_plan_keyword_rejects_only_punctuation() -> None:
    """纯分隔符切不出词项: 返回空计划, 不退化成"用标点做子串扫描"."""
    plan = plan_keyword("---")
    assert plan.tiers == []
    assert plan.like_fallback is None
    assert plan.is_empty


def _fts() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    columns = ", ".join(f.value for f in SearchField)
    con.execute(f"CREATE VIRTUAL TABLE fts USING fts5({columns}, content='', contentless_delete=1)")
    corpus = [
        (1, "SSIS-001", "巨乳人妻の濃厚な性交", ""),
        (2, "SSIS-497", "人と話す妻の日常", ""),
        (3, "FC2-PPV-1234567", "湘南の海で", ""),
    ]
    for metadata_id, number, title, plot in corpus:
        con.execute(
            f"INSERT INTO fts(rowid, {columns}) VALUES ({', '.join('?' * (len(SearchField) + 1))})",
            (
                metadata_id,
                " ".join(index_number_tokens(number)),
                " ".join(index_text_tokens(title)),
                " ".join(index_text_tokens(plot)),
                "",
                "",
                "",
                "",
                "",
            ),
        )
    return con


def _hits(con: sqlite3.Connection, query: str) -> list[int]:
    plan = plan_keyword(query)
    for tier in plan.tiers:
        rows = [r[0] for r in con.execute("SELECT rowid FROM fts WHERE fts MATCH ? ORDER BY rowid", (tier,))]
        if rows:
            return rows
    return []


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("SSIS-001", [1]),
        ("ssis001", [1]),
        ("SSIS 001", [1]),
        ("FC2-PPV-1234567", [3]),
        ("fc2ppv1234567", [3]),
        ("001", [1]),
        # 短语等价子串: 只有相邻的"人妻"命中, "人…妻"不相邻的第 2 条不命中.
        ("人妻", [1]),
        ("湘南", [3]),
    ],
)
def test_keyword_query_semantics(query: str, expected: list[int]) -> None:
    assert _hits(_fts(), query) == expected
