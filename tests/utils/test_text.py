"""normalize_long_text 表测试."""

from __future__ import annotations

import pytest

from amane.utils.text import clean_title, normalize_long_text

CASES: list[tuple[str | None, str | None]] = [
    (None, None),
    ("", None),
    ("   ", None),
    ("\xa0", None),
    ("<br>", None),
    ("<p></p>", None),
    # 换行: 标签与空行
    ("前戏<br>高潮", "前戏\n高潮"),
    ("前戏<br/>高潮", "前戏\n高潮"),
    ("前戏<br />高潮", "前戏\n高潮"),
    ("前戏<BR>高潮", "前戏\n高潮"),
    ("一<br><br>二", "一\n\n二"),
    ("第一行\r\n第二行", "第一行\n第二行"),
    ("第一行\r第二行", "第一行\n第二行"),
    ("<div><p>甲<br />乙</p></div>", "甲\n乙"),
    ("a\n\n\n\nb", "a\n\nb"),
    ("  首  \n  尾  ", "首\n尾"),
    # 实体: 解码一次, 不产生二次转义
    ("S&amp;M プレイ", "S&M プレイ"),
    ("&#038;演出", "&演出"),
    ("AT&amp;amp;T", "AT&amp;T"),
    # 异体空白收成普通空格
    ("a&nbsp;b", "a b"),
    ("a\u2003b", "a b"),
    ("a\u3000b", "a b"),
    # 非法 XML 字符剥离
    ("a\x0bb\x0cc", "abc"),
    ("a\x00b", "ab"),
    # 不是标签的尖括号保持原样
    ("5 < 6 > 4", "5 < 6 > 4"),
    ("A &lt; B", "A < B"),
    # 实体编码的标签按标签处理 (幂等优先)
    ("x &lt;br&gt; y", "x\ny"),
]


@pytest.mark.parametrize(("raw", "expected"), CASES)
def test_normalize_long_text(raw: str | None, expected: str | None) -> None:
    assert normalize_long_text(raw) == expected


_DOUBLE_ENCODED = {"AT&amp;amp;T"}


@pytest.mark.parametrize(("raw", "_expected"), [c for c in CASES if c[0] not in _DOUBLE_ENCODED])
def test_normalize_is_idempotent(raw: str | None, _expected: str | None) -> None:
    """聚合输出与落库钩子都会调用, 二次调用必须不再变化."""
    once = normalize_long_text(raw)
    assert normalize_long_text(once) == once


def test_double_encoded_entities_decode_per_pass() -> None:
    """双重编码是唯一的非幂等输入: 上游二次转义的实体每次调用再解一层."""
    assert normalize_long_text("AT&amp;amp;T") == "AT&amp;T"
    assert normalize_long_text(normalize_long_text("AT&amp;amp;T")) == "AT&T"


# --- clean_title: 爬取后处理 / 存量回填共用的标题去重 ---

_TITLE_CASES: list[tuple[str, str, tuple[str, ...], str]] = [
    # (原标题, 番号, 演员名单, 期望)
    ("YST-232 与最爱的母亲结婚的继父是个鬼畜。 泉莉音", "YST-232", (), "与最爱的母亲结婚的继父是个鬼畜。 泉莉音"),
    (
        "YST-221 大好きな母と結婚した義父は鬼畜な人でした。 泉りおん",
        "YST-221",
        ("泉りおん",),
        "大好きな母と結婚した義父は鬼畜な人でした。",
    ),
    ("YST221 标题", "YST-221", (), "标题"),
    ("YST-00221 标题", "YST-221", (), "标题"),
    ("CAWD-441 CAWD-441 双写番号", "CAWD-441", (), "双写番号"),
    # 番号不同不得剥离 (片名本身以别的编号打头)
    ("ABP-123 标题", "CAWD-441", (), "ABP-123 标题"),
    # 引号/书名号包裹: 右包裹符必须保留配对
    ("《妻子被挚友夺走的故事 三田樱》", "ABC-123", ("三田樱",), "《妻子被挚友夺走的故事》"),
    ("【脑BUG小穴高潮】正文 绝顶高潮", "MIAB-384", ("宫城理惠",), "【脑BUG小穴高潮】正文 绝顶高潮"),
    # 多个演员名连续出现
    ("标题 逢澤琉琉 廣瀨海", "ABC-123", ("逢澤琉琉", "廣瀨海"), "标题"),
    # 名字不是以分隔符为界时不动 (避免切正文)
    ("标题富含泉莉音", "ABC-123", ("泉莉音",), "标题富含泉莉音"),
    # 空/None 与只含番号的畸形标题
    ("", "ABC-123", (), ""),
    ("ABC-123", "ABC-123", (), "ABC-123"),
    # 无番号可比对时不动开头
    ("标题 泉莉音", None, ("泉莉音",), "标题"),
]


@pytest.mark.parametrize(("raw", "number", "actors", "expected"), _TITLE_CASES)
def test_clean_title(raw: str, number: str | None, actors: tuple[str, ...], expected: str) -> None:
    assert clean_title(raw, number, actors) == expected


@pytest.mark.parametrize(("raw", "number", "actors", "_expected"), _TITLE_CASES)
def test_clean_title_is_idempotent(raw: str, number: str | None, actors: tuple[str, ...], _expected: str) -> None:
    """爬取后处理与存量回填都会调用, 二次调用必须不再变化."""
    once = clean_title(raw, number, actors)
    assert clean_title(once, number, actors) == once


def test_clean_title_fuzzy_matches_translated_actor_name() -> None:
    """存量里标题已译成中文而演员表仍是日文原名时, 按共享汉字识别尾名人名."""
    # 句末标点属于正文, 剥人名时不连标点一起吃掉
    assert clean_title("与最爱的母亲结婚的继父是个鬼畜。 泉莉音", "YST-232", ("泉りおん",), fuzzy=True) == (
        "与最爱的母亲结婚的继父是个鬼畜。"
    )
    assert clean_title("禁忌看护 美谷朱里", "ABC-123", ("美谷朱音",), fuzzy=True) == "禁忌看护"
    # 关闭 fuzzy 时不做近似匹配
    assert clean_title("禁忌看护 美谷朱里", "ABC-123", ("美谷朱音",)) == "禁忌看护 美谷朱里"


def test_clean_title_keeps_bracket_balance() -> None:
    """剥离后不能让右括号孤立, 否则标题包裹符断裂."""
    cleaned = clean_title("做饭・洗衣 今日子（43）", "SDDE-526", ("久保今日子",), fuzzy=True)
    assert cleaned.count("（") == cleaned.count("）")
