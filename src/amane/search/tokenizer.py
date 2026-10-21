"""检索分词与查询路由.

索引端与查询端必须调用本模块的同一组函数: 两侧切分不一致时永远搜不到.

切分按**字符脚本**进行, 不按文档语言判断: 同一字段内可中日英混排, 且索引端无从得知
查询会落在哪种语言上. CJK 连写段产出 bigram, ASCII 连写段产出整词, 番号额外产出归一原子.
bigram 索引上的**短语查询**等价于子串语义, 精度靠查询侧的相邻表达, 不靠词典.
"""

import re
import unicodedata
from enum import StrEnum

from pydantic import BaseModel, Field

#: 汉字 + 平/片假名 + 半角片假名. 与 ``utils.language.KANA`` 口径一致, 此处并入汉字.
_CJK_RUN = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9d]+")
#: ASCII 字母数字段; 番号里的连字符/点/下划线是分隔符, 不参与 token.
_WORD_RUN = re.compile(r"[0-9A-Za-z]+")
#: 番号归一: 小写 + 丢弃全部非字母数字. ``SSIS-001`` / ``ssis 001`` / ``SSIS001`` 同解.
_NON_ALNUM = re.compile(r"[^a-z0-9]")
_PART = re.compile(r"[a-z]+|\d+")


#: 查询切片: CJK 连写段, 或 ASCII 块 (内部允许 ``-._/+`` 分隔, 使 ``SSIS-001`` 保持完整).
_QUERY_CHUNK = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9d]+|[0-9A-Za-z][0-9A-Za-z._/+-]*"
)


class SegmentKind(StrEnum):
    CJK = "cjk"
    WORD = "word"


class TokenField(StrEnum):
    """可检索字段. 值即 FTS5 列名, 两者必须一致."""

    NUMBER = "number"
    TITLE = "title"
    PLOT = "plot"
    ACTORS = "actors"
    TAGS = "tags"
    SERIES = "series"
    STUDIO = "studio"
    PUBLISHER = "publisher"

    @property
    def is_text(self) -> bool:
        """number 是标识符, 其余是自然语言."""
        return self is not TokenField.NUMBER


#: 查询文本会落到这些字段上 (number 单独走标识符路径).
TEXT_FIELDS: tuple[TokenField, ...] = tuple(f for f in TokenField if f.is_text)
#: 出现在 FTS5 MATCH 列过滤里的文本列集合; 短语必须落在单列内部才成立.
TEXT_COLUMN_SCOPE = "{" + " ".join(f.value for f in TEXT_FIELDS) + "}"


def normalize_number(text: str) -> str:
    """番号归一形. 只用于索引与匹配, 展示仍取原始值.

    先做 NFKC: 日文输入法打出的全角 ``ＳＳＩＳ－００１`` 与半角形式应同解.
    只归一 number 列, 文本列不做 NFKC —— 两侧必须一致, 而文本侧保持原样即可对称.
    """
    folded = unicodedata.normalize("NFKC", text).lower()
    return _NON_ALNUM.sub("", folded)


def segments(text: str) -> list[tuple[SegmentKind, str]]:
    """按字符脚本切段, 分隔符丢弃."""
    out: list[tuple[SegmentKind, str]] = []
    pos = 0
    while pos < len(text):
        cjk = _CJK_RUN.match(text, pos)
        if cjk:
            out.append((SegmentKind.CJK, cjk.group()))
            pos = cjk.end()
            continue
        word = _WORD_RUN.match(text, pos)
        if word:
            out.append((SegmentKind.WORD, word.group().lower()))
            pos = word.end()
            continue
        pos += 1
    return out


def index_number_tokens(number: str) -> list[str]:
    """number 列 token 流: 归一原子在前, 分段词在后.

    原子形服务精确查询, 分段形服务 ``001`` / ``sone`` 这类片段查询.
    分段还保留 ``ppv`` 之类前缀词, 便于 ``FC2-PPV`` 系列整体检索.
    """
    atomic = normalize_number(number)
    parts = _PART.findall(number.lower()) if number else []
    tokens = [atomic] if atomic else []
    tokens.extend(part for part in parts if part and part != atomic)
    return tokens


def index_text_tokens(text: str) -> list[str]:
    """文本列 token 流: CJK 段出 bigram, ASCII 段出整词.

    CJK 段长度 1 时直接出单字 (不为空串产 token). 单字查询仍走 LIKE 回退:
    单字 token 只能覆盖"整段就一个字"的文档, 用 MATCH 会漏掉多数命中.
    """
    tokens: list[str] = []
    for kind, chunk in segments(text):
        if kind is SegmentKind.CJK:
            tokens.extend([chunk] if len(chunk) == 1 else [chunk[i : i + 2] for i in range(len(chunk) - 1)])
        else:
            tokens.append(chunk)
    return tokens


class QueryPart(BaseModel):
    """查询的一个词项. ASCII 块内的分隔符已剥离, 番号因此保持完整."""

    raw: str
    kind: SegmentKind

    @property
    def is_number_candidate(self) -> bool:
        """ASCII 块一律先试 number 列 (番号前缀), 与其是否含数字无关.

        纯字母可能是番号前缀 (``sone`` → ``sone401``), 直接排除会漏召回;
        两条通道之间是 OR, 猜错只多一次空查询, 不产生误命中.
        """
        return self.kind is SegmentKind.WORD


class KeywordPlan(BaseModel):
    """关键词检索的执行计划.

    ``tiers`` 按序执行, 前一级有命中即不再降级 —— 番号精确优先于片段模糊.
    ``like_fallback`` 非空时表示该查询 FTS 无法表达, 调用方应改用子串扫描.
    ``number_exact`` 是该查询的番号精确表达式 (各 ASCII 词项的归一原子前缀之 AND).
    它不参与 tier 选择, 只用于事后标记"这条命中来自标识符列" —— FTS5 无法回报是哪一列命中的,
    而排序必须知道这件事: 语义近邻的分数可以任意高, 番号精确命中仍应排在最前.
    """

    tiers: list[str] = Field(default_factory=list)
    like_fallback: str | None = None
    number_exact: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.tiers and self.like_fallback is None


def _quote(term: str) -> str:
    """FTS5 词项必须加双引号: 裸 ``SSIS-001`` 里的 ``-`` 会被当成列引用."""
    return '"' + term.replace('"', '""') + '"'


def _phrase(part: QueryPart) -> str | None:
    """把词项表达成文本列上的匹配片段.

    CJK 长度 >= 2 时用 bigram 短语 (相邻 ⇒ 等价子串); 长度 1 无法用 bigram 表达, 交给 LIKE.
    ASCII 直接用整词.
    """
    if part.kind is SegmentKind.CJK:
        if len(part.raw) < 2:
            return None
        grams = [part.raw[i : i + 2] for i in range(len(part.raw) - 1)]
        return " ".join(_quote(g) for g in grams)
    return _quote(part.raw.lower())


def _number_atomic(part: QueryPart) -> str:
    """番号一级表达式: 归一原子的前缀匹配."""
    return f"{{number}} : {_quote(normalize_number(part.raw))}*"


def _number_parts(part: QueryPart) -> str:
    """番号二级表达式: 分段词 OR, 用于 ``001`` / ``sone`` 这类片段."""
    tokens = [t for t in _PART.findall(part.raw.lower()) if t]
    if not tokens:
        return ""
    return "{number} : (" + " OR ".join(_quote(t) for t in tokens) + ")"


def _split_query(text: str) -> list[QueryPart]:
    """按空白与脚本切片; ASCII 块内的 ``-._/+`` 不切断, 使番号保持完整."""
    parts: list[QueryPart] = []
    for chunk in _QUERY_CHUNK.findall(text):
        # ASCII 块去掉尾部标点: ``SSIS-001.`` 与 ``SSIS-001`` 同解.
        cleaned = chunk.rstrip("._/+-") if not _CJK_RUN.fullmatch(chunk) else chunk
        if not cleaned:
            continue
        kind = SegmentKind.CJK if _CJK_RUN.fullmatch(cleaned) else SegmentKind.WORD
        parts.append(QueryPart(raw=cleaned, kind=kind))
    return parts


def plan_keyword(query: str) -> KeywordPlan:
    """把用户输入翻成 FTS5 执行计划.

    词项之间 AND; 单个词项内部, number 通道与文本通道是 OR —— 番号候选猜错只会多一次空查询,
    而 AND 起来会让"番号片段同时要求出现在标题里"直接 0 命中.

    分级:
      一级 number 取归一原子前缀, 文本取 bigram 短语;
      二级 number 退回分段 OR (仅在存在 ASCII 词项时), 文本不变.
    """
    text = query.strip()
    if not text:
        return KeywordPlan()

    parts = _split_query(text)
    if not parts:
        return KeywordPlan()

    # 单个 CJK 单字: bigram 表达不了, 直接回退子串扫描.
    if len(parts) == 1 and parts[0].kind is SegmentKind.CJK and len(parts[0].raw) < 2:
        return KeywordPlan(like_fallback=parts[0].raw)

    tier1: list[str] = []
    tier2: list[str] = []
    atomics: list[str] = []
    has_number = False
    for part in parts:
        phrase = _phrase(part)
        text_expr = f"{TEXT_COLUMN_SCOPE} : ({phrase})" if phrase is not None else ""
        if part.is_number_candidate:
            has_number = True
            atomics.append(_number_atomic(part))
            channels1 = [_number_atomic(part)]
            if text_expr:
                channels1.append(text_expr)
            tier1.append("(" + " OR ".join(channels1) + ")")
            relaxed = _number_parts(part)
            channels2 = [relaxed] if relaxed else []
            if text_expr:
                channels2.append(text_expr)
            if channels2:
                tier2.append("(" + " OR ".join(channels2) + ")")
        elif text_expr:
            tier1.append(text_expr)
            tier2.append(text_expr)

    if not tier1:
        return KeywordPlan(like_fallback=text)

    tiers = [" AND ".join(tier1)]
    if has_number:
        relaxed = " AND ".join(tier2) if tier2 else ""
        if relaxed and relaxed != tiers[0]:
            tiers.append(relaxed)
    return KeywordPlan(tiers=tiers, number_exact=" AND ".join(atomics) if atomics else None)
