"""长文本归一: 把上游的 HTML 片段 / 实体 / 异体空白收成纯文本.

调用时机是「聚合输出 + 落库写入」两处, 因此必须幂等 — 实体解码排在标签处理之前,
``&lt;br&gt;`` 与 ``<br>`` 都收成换行, 二次调用不再变化 (代价是实体编码的标签也当标签处理).
唯一例外是上游**双重编码**的实体 (``&amp;amp;``): 每次调用再解一层 — 这正落在它想要的字符上.

段落用空行 (``\\n\\n``) 表示, 单个 ``\\n`` 表示折行: Kodi 逐行折行, Emby / Jellyfin 按
markdown 渲染时只有空行才分段.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable

_LINE_BREAK = re.compile(r"(?i)<br\s*/?>")
_BLOCK_END = re.compile(r"(?i)</(?:p|div|li|h[1-6]|tr|td|section|article)\s*>")
# 标签名以字母开头, 避免把 ``5 < 6 > 4`` 这类数学文本当标签忽略.
_TAG = re.compile(r"</?[a-zA-Z][a-zA-Z0-9-]*(?:\s[^<>]*?)?/?>")
_UNICODE_SPACES = re.compile("[\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]")
_BLANK_RUN = re.compile(r"\n{3,}")
# XML 1.0 非法字符 (C0 除 \t\n\r, C1, 孤立代理, BOM 与非字符).
_ILLEGAL_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff\ufeff\ufffe\uffff]")


def strip_illegal_xml_chars(text: str) -> str:
    """去掉无法写进 XML 的字符; 写 NFO 前的兜底 (存量数据可能仍带脏字符)."""
    return _ILLEGAL_XML.sub("", text)


def normalize_long_text(value: str | None) -> str | None:
    """长文本字段 (简介等) → 纯文本; 无有效内容返回 None."""
    if value is None:
        return None
    text = html.unescape(value)
    text = _LINE_BREAK.sub("\n", text)
    text = _BLOCK_END.sub("\n\n", text)
    text = _TAG.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _UNICODE_SPACES.sub(" ", text)
    text = strip_illegal_xml_chars(text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_RUN.sub("\n\n", text).strip() or None


# 标题里的番号: 2-12 个字母 + 2-6 位数字, 后面须是分隔符或结尾.
_LEAD_NUMBER = re.compile(r"^([A-Za-z]{2,12})[\s\-_]*(\d{2,6})(?=$|[\s\-–—_/:：|｜·.,、，。()（）\[\]【】])")
# 番号/演员名之后的连接符与空白 (逐次剥掉).
_TITLE_SEP = re.compile(r"^[\s\-–—_/:：|｜·.,、，。]+")
# 只剥连接符与空白, **不含包裹符**: 右括号/右书名号由 _split_closers 保留配对.
_TRAIL_SEP = re.compile(r"[\s\-–—_/:：|｜·]+$")
_NAME_BOUNDARY = frozenset(" \t-–—_/:：|｜·.,、，。()（）[]【】")
# 结尾最后一个以空白分隔的词 (fuzzy 匹配候选人名时用)
_TRAILING_TOKEN = re.compile(r"(?:^|\s)([^\s]+)$")
_CJK_CHAR = re.compile("[\u4e00-\u9fff]")
# 标题包裹符: 剥演员名时要原样保留 (否则会吃掉右书名号/右括号, 破坏配对)
_CLOSERS = "》」』）)]】〉”\"'"


def _split_closers(text: str) -> tuple[str, str]:
    """拆出结尾连续的包裹符: ``《… 三田樱》`` → ``("《… 三田樱", "》")``."""
    index = len(text)
    while index > 0 and text[index - 1] in _CLOSERS:
        index -= 1
    return text[:index], text[index:]


_BRACKET_PAIRS = (("（", "）"), ("(", ")"), ("《", "》"), ("【", "】"), ("「", "」"), ("『", "』"))


def _imbalance(text: str) -> int:
    """包裹符不成对的程度; 用于否决「把右括号单独剩下」的剥离."""
    return sum(abs(text.count(open_) - text.count(close)) for open_, close in _BRACKET_PAIRS)


def _number_parts(value: str) -> tuple[str, int] | None:
    """``ABP-123`` → ``("ABP", 123)``; 非番号返回 None. 前缀零按数值比较."""
    match = _LEAD_NUMBER.match((value or "").strip())
    if not match:
        return None
    return match.group(1).upper(), int(match.group(2))


def strip_leading_number(title: str | None, number: str | None) -> str:
    """剥掉标题开头重复出现的番号 (可连续多次).

    只剥与 ``number`` 同一个番号的开头片段: 万一片名本身以别的编号打头, 原样保留.
    剥完为空 (标题只由番号构成) 时保留原值.
    """
    text = (title or "").strip()
    want = _number_parts(number or "")
    if not text or want is None:
        return text
    for _ in range(3):
        got = _number_parts(text)
        if got != want:
            break
        match = _LEAD_NUMBER.match(text)
        if match is None:
            break
        rest = _TITLE_SEP.sub("", text[match.end() :]).strip()
        if not rest or _imbalance(rest) > _imbalance(text):
            break
        text = rest
    return text


def strip_trailing_actors(title: str | None, actors: Iterable[object] | None, *, fuzzy: bool = False) -> str:
    """剥掉标题结尾重复的演员名 (可连续多个).

    只在**名字本身以分隔符或开头为界**时才剥, 避免切掉正文里同形的文字片段;
    名单为空或剥完为空时保留原值. 长名优先, 避免短名先命中把长名截断.

    ``fuzzy=True`` 额外接受「与某个演员名共享汉字的中文译名」: 存量数据里标题已译成中文,
    演员表仍是日文原名 (``泉りおん`` vs ``泉莉音``), 精确匹配会漏. 只在回填时启用.
    """
    text = (title or "").strip()
    names = sorted({(str(n) or "").strip() for n in actors or () if str(n).strip()}, key=len, reverse=True)
    if not text or not names:
        return text
    char_sets = [set(_CJK_CHAR.findall(name)) for name in names] if fuzzy else []
    for _ in range(6):
        # 右包裹符先摘下来, 剥完再挂回去: 《… 三田樱》 → 《…》
        core, closers = _split_closers(text)
        stripped = None
        for name in names:
            if not core.endswith(name):
                continue
            head = core[: -len(name)]
            if head and head[-1] not in _NAME_BOUNDARY:
                continue
            body = _TRAIL_SEP.sub("", head).strip()
            stripped = body + closers if body else None
            break
        if stripped is None and fuzzy:
            token = _TRAILING_TOKEN.search(core)
            if token is not None and 2 <= len(token.group(1)) <= 6:
                tail = token.group(1)
                picked = set(_CJK_CHAR.findall(tail))
                need = 1 if len(tail) <= 4 else 2
                if picked and any(len(picked & chars) >= need for chars in char_sets):
                    head = core[: token.start(1)]
                    cand = _TRAIL_SEP.sub("", head).strip()
                    if cand:
                        stripped = cand + closers
        if not stripped:
            break
        if _imbalance(stripped) > _imbalance(text):
            break
        text = stripped
    return text


def clean_title(
    title: str | None,
    number: str | None = None,
    actors: Iterable[object] | None = (),
    *,
    fuzzy: bool = False,
) -> str:
    """标题去重: 去掉开头重复的番号与结尾重复的演员名. 幂等.

    爬取后处理与存量回填共用: 站点普遍把 ``番号 + 作品名 + 出演者`` 拼成一个标题,
    与 ``number`` / ``actors`` 字段重复, 会污染语义检索与命名模板.
    """
    original = (title or "").strip()
    if not original:
        return original
    text = strip_trailing_actors(strip_leading_number(original, number), actors, fuzzy=fuzzy)
    return text or original
