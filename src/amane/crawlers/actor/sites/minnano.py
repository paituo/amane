"""名字行 ``名前（かな / ローマ字）``; 注记括号不并入读音.

站内检索入口 ``search_result.php`` 处于 Cloudflare 托管挑战保护: 任何 TLS 指纹的纯 HTTP 请求
一律 403 (``cf-mitigated: challenge``), 只能执行 JS 越过. 而女优一览 ``actress_list.php``
与详情页 ``actress{id}.html`` 不在保护范围内. 因此检索改由一览页构建本地索引; 站内搜索保留
为索引未命中时的回退, 覆盖索引建成之后新增的女优.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import structlog
from parsel import Selector

from amane.enums import ActorGender, SiteName
from amane.net.connectivity import ConnectivityOutcome
from amane.net.errors import SourceError
from amane.parsing import split_actor_aliases
from amane.plugins.models import SourceCapability
from amane.utils.dates import normalize_calendar_date

from ...base import CrawlerProfile
from ...parsing import extract_text, fold_name, is_same_name, normalize_name
from ..base import ActorCrawler
from ..models import ActorMetadata

if TYPE_CHECKING:
    from ...http import HttpClient

# 名字段可能带注记括号 (如 ``相川美羽(tenshigao)``); kana 排除括号字符, 以免注记并入读音.
_NAME_RE = re.compile(r"^(?P<name>.+?)\s*[（(]\s*(?P<kana>[^/（）()]+?)\s*/\s*(?P<roma>[^）)]+?)\s*[）)]\s*$")
_FIGURE_RE = re.compile(
    r"T\s*(?P<t>\d+)\s*/\s*B\s*(?P<b>\d+)\s*"
    r"(?:\(\s*(?:<[^>]+>)?(?P<cup>[A-Z]+)(?:カップ)?(?:</[^>]+>)?\s*\))?\s*"
    r"/\s*W\s*(?P<w>\d+)\s*/\s*H\s*(?P<h>\d+)",
    re.IGNORECASE,
)
_CUP_RE = re.compile(r"([A-Z]+)カップ", re.IGNORECASE)
_ACTRESS_HREF_RE = re.compile(r"actress\d+\.html", re.IGNORECASE)

_LIST_PATH = "actress_list.php"
"""女优一览入口; 不受 Cloudflare 挑战保护, 索引由此构建."""
_LD_JSON_RE = re.compile(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.DOTALL)
_PAGE_PARAM_RE = re.compile(r"page=(\d+)")
# 一览页分页抓取的并发上限; 实际出网速率仍由 RateLimiters 按 host 平滑.
_INDEX_PAGE_CONCURRENCY = 8
# 站点规模约 2.5 万名女优 / 每页 30 条 ≈ 831 页, 上限用于兜住异常分页.
_INDEX_MAX_PAGES = 2000
_INDEX_TTL_SECONDS = 7 * 24 * 3600
"""索引缓存有效期; 过期后后台重建, 以收录新增女优 (旧索引继续兜底)."""
_INDEX_MIN_ENTRIES = 500
"""低于该规模视为构建失败, 不覆盖已有缓存."""
_INDEX_KEEP_RATIO = 0.5
"""新索引不足现有规模的一半时视为残缺 (大量分页失败), 保留旧索引不替换."""
_REBUILD_RETRY_MIN_SECONDS = 300
"""后台构建失败后的首次重试间隔; 之后按次倍增到上限."""
_REBUILD_RETRY_MAX_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class _ActressEntry:
    """一览页条目: 显示名与详情页绝对地址."""

    name: str
    url: str


class _ActressIndex:
    """女优一览索引: 磁盘缓存优先, 缺失时分页构建; 并发下只构建一次.

    键是折叠后的艺名 (去注记括号 + 大小写无关), 与检索命中判定用同一套归一,
    因此索引与原先的站内搜索在「哪些名字能命中」上等价.
    """

    def __init__(
        self,
        client: HttpClient,
        *,
        base_url: str,
        cookies: dict[str, str],
        cache_path: Path | None,
        logger: structlog.stdlib.BoundLogger,
    ) -> None:
        self._client = client
        self._base_url = base_url
        self._cookies = cookies
        self._cache_path = cache_path
        self._logger = logger
        self._lock = asyncio.Lock()
        self._by_key: dict[str, str] | None = None
        # 当前索引的构建时间; None 表示还没有可用索引 (冷启动占位).
        self._built_at: float | None = None
        # 后台重建任务; 同一时刻至多一个, 去重见 _schedule_rebuild.
        self._rebuild_task: asyncio.Task[None] | None = None
        # 连续失败次数与冷却截止时间: 构建失败后不立刻重试, 避免 403 风暴.
        self._rebuild_failures = 0
        self._retry_at = 0.0

    async def lookup(self, name: str) -> str | None:
        """艺名 → 详情页地址; 未收录返回 None."""
        by_key = await self._ensure()
        key = _entry_key(name)
        if not key:
            return None
        return by_key.get(key)

    async def _ensure(self) -> dict[str, str]:
        """获取当前生效索引; 任何情况下都不阻塞查询.

        首次进入时在锁内决定初值:
        - 磁盘有缓存 (含过期) → 立即加载为兜底, 由 _refresh_if_needed 判断是否后台重建;
        - 磁盘无缓存 (冷启动) → 置空占位, 后台构建, 查询降级而非等千页抓完.

        索引一旦为内存态, 长跑进程内同样按 TTL 判过期 (见 _refresh_if_needed),
        否则进程活过 7 天就再也不会收录新增女优.
        """
        if self._by_key is None:
            async with self._lock:
                if self._by_key is None:
                    cached = self._load_cache()
                    if cached is not None:
                        self._by_key, self._built_at = cached
                    else:
                        self._by_key = {}
                        self._built_at = None
        self._refresh_if_needed()
        return self._by_key

    def _refresh_if_needed(self) -> None:
        """索引缺失或过期时调度后台重建; 有旧索引则继续兜底."""
        if self._built_at is not None and not self._is_stale(self._built_at):
            return
        self._schedule_rebuild()

    async def warmup(self) -> None:
        """启动期预热: 非阻塞地确保索引可用, 过期/缺失触发后台重建.

        冷启动无缓存时亦不阻塞, 后台构建完成后自动填充, 期间查询降级回退.
        """
        await self._ensure()

    async def _build(self) -> tuple[_ActressEntry, ...]:
        first = await self._fetch_page(1)
        if first is None:
            self._logger.warning("minnano actress index first page failed")
            return ()
        entries, last_page = first
        if last_page <= 1:
            return entries
        semaphore = asyncio.Semaphore(_INDEX_PAGE_CONCURRENCY)

        async def fetch(page: int) -> tuple[_ActressEntry, ...]:
            async with semaphore:
                result = await self._fetch_page(page)
            return () if result is None else result[0]

        pages = await asyncio.gather(*(fetch(p) for p in range(2, last_page + 1)))
        merged: list[_ActressEntry] = list(entries)
        for chunk in pages:
            merged.extend(chunk)
        return tuple(merged)

    async def _fetch_page(self, page: int) -> tuple[tuple[_ActressEntry, ...], int] | None:
        url = f"{self._base_url}/{_LIST_PATH}?page={page}"
        try:
            text = await self._client.get_html(url, cookies=self._cookies)
        except SourceError as exc:
            self._logger.warning("minnano actress index page failed", page=page, reason=str(exc.reason))
            return None
        entries = parse_minnano_actress_list(text, base_url=self._base_url)
        return entries, _last_page(text, fallback=page)

    def _load_cache(self) -> tuple[dict[str, str], float] | None:
        """磁盘缓存 → (索引, built_at); 不看 TTL, 过期也返回用于兜底.

        新鲜度判断交给调用方 (_is_stale), 这样过期缓存仍能立即提供旧名命中,
        不必等后台重建完成.
        """
        path = self._cache_path
        if path is None or not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            self._logger.warning("minnano actress index cache unreadable", path=str(path))
            return None
        if not isinstance(raw, dict):
            return None
        built_at = raw.get("built_at")
        if not isinstance(built_at, (int, float)):
            return None
        items = raw.get("entries")
        if not isinstance(items, list):
            return None
        out: dict[str, str] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            name, url = item.get("name"), item.get("url")
            if isinstance(name, str) and isinstance(url, str) and _entry_key(name):
                out.setdefault(_entry_key(name), url)
        if not out:
            return None
        return out, float(built_at)

    def _is_stale(self, built_at: float) -> bool:
        return time.time() - built_at > _INDEX_TTL_SECONDS

    def _schedule_rebuild(self) -> None:
        """后台重建索引; 同一时刻至多一个任务, 且失败后按退避冷却重试."""
        if self._rebuild_task is not None and not self._rebuild_task.done():
            return
        if time.time() < self._retry_at:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._logger.warning("minnano actress index rebuild skipped: no running loop")
            return
        self._rebuild_task = loop.create_task(self._rebuild_in_background(), name="minnano-index-rebuild")

    async def _rebuild_in_background(self) -> None:
        """后台分页构建索引; 成功则原子替换兜底索引并落盘, 失败保留旧索引."""
        try:
            entries = await self._build()
        except Exception:
            self._logger.warning("minnano actress index rebuild failed, keeping previous", exc_info=True)
            entries = ()
        if not self._is_usable(entries):
            self._logger.warning(
                "minnano actress index rebuild too small, keeping previous",
                entries=len(entries),
                previous=len(self._by_key or {}),
            )
            self._note_rebuild_failure()
            return
        built_at = time.time()
        self._by_key = {_entry_key(e.name): e.url for e in entries if _entry_key(e.name)}
        self._built_at = built_at
        self._rebuild_failures = 0
        self._retry_at = 0.0
        self._write_cache(entries, built_at=built_at)
        self._logger.info("minnano actress index rebuilt", entries=len(entries))

    def _is_usable(self, entries: tuple[_ActressEntry, ...]) -> bool:
        """新索引是否可用于替换旧索引.

        两道闸: 绝对下限 (明显不是正常站点规模), 以及相对下限 (大量分页失败会得到
        一份"成功但残缺"的索引, 用它替换完整索引等于让 2 万多名女优静默消失).
        """
        if len(entries) < _INDEX_MIN_ENTRIES:
            return False
        previous = len(self._by_key or {})
        return not previous or len(entries) >= previous * _INDEX_KEEP_RATIO

    def _note_rebuild_failure(self) -> None:
        """记录失败并按次倍增冷却: 5 分钟起步, 上限 1 小时.

        冷启动时索引是空占位, 没有旧索引可兜底; 若不冷却重试, 每次查询都会
        再起一个全站抓取任务, 把 403 放大成风暴.
        """
        self._rebuild_failures += 1
        delay = min(_REBUILD_RETRY_MIN_SECONDS * 2 ** (self._rebuild_failures - 1), _REBUILD_RETRY_MAX_SECONDS)
        self._retry_at = time.time() + delay

    def _write_cache(self, entries: tuple[_ActressEntry, ...], *, built_at: float | None = None) -> None:
        path = self._cache_path
        if path is None or len(entries) < _INDEX_MIN_ENTRIES:
            return
        payload = {
            "version": 1,
            "built_at": time.time() if built_at is None else built_at,
            "entries": [{"name": e.name, "url": e.url} for e in entries],
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError:
            self._logger.warning("minnano actress index cache write failed", path=str(path))


class MinnanoActorCrawler(ActorCrawler):
    def __init__(self, client: HttpClient, config: Any = None, *, data_dir: Path | None = None) -> None:
        super().__init__(client, config)
        self._index = _ActressIndex(
            client,
            base_url=self.base_url,
            cookies=self.cookies,
            cache_path=None if data_dir is None else data_dir / "minnano_actress_index.json",
            logger=self.logger,
        )

    @classmethod
    def profile(cls) -> CrawlerProfile:
        return CrawlerProfile(
            name=SiteName.MINNANO,
            base_url="https://www.minnano-av.com",
            capabilities=frozenset({SourceCapability.ACTOR_PROFILE}),
            genders=frozenset({ActorGender.FEMALE}),
            # 站点用 JS 注入年龄确认弹层, 仅在缺 cookie 时出现; 预置后页面不再被拦截判定命中.
            cookies={"age_verified": "1"},
        )

    async def warmup(self) -> None:
        """启动期预热本地索引: 旧缓存立即兜底, 过期/缺失后台重建, 不阻塞首查."""
        await self._index.warmup()

    async def _search(self, name: str) -> str | None:
        # 索引优先: 站内检索入口被 Cloudflare 挑战挡住, 一览页没有; 命中即不出网.
        indexed = await self._index.lookup(name)
        if indexed is not None:
            return indexed
        return await self._search_remote(name)

    async def _search_remote(self, name: str) -> str | None:
        """站内检索回退: 索引未收录时的唯一在线路径, 受挑战保护故可能 403."""
        search_url = self._search_url(name)
        text = await self.client.get_html(search_url, cookies=self.cookies)
        html = Selector(text=text)

        # 精确命中时常 302 到详情页; 跟随重定向后 HTML 已是详情.
        if html.css("div.act-profile").get():
            detail = extract_text(html, '//link[@rel="canonical"]/@href') or extract_text(
                html, '//meta[@property="og:url"]/@content'
            )
            if detail:
                return detail
            # 未命中则从头像路径回退.
            img = extract_text(html, '//div[contains(@class,"thumb")]//img[contains(@src,"p_actress")]/@src')
            if img:
                m = re.search(r"/(\d+)\.jpg", img)
                if m:
                    return f"{self.base_url}/actress{m.group(1)}.html"
            return None

        return self._pick_search_hit(html, name)

    def _search_url(self, name: str) -> str:
        return f"{self.base_url}/search_result.php?search_scope=actress&search_word={quote(name)}&search=Go"

    async def check_connectivity(self) -> ConnectivityOutcome:
        """探测女优一览入口: 检索由它构建的索引承担, 而 ``search_result.php`` 处于挑战保护,
        探测它会让整站恒报不可达."""
        return await self.client.check(self._list_url(), cookies=self.cookies)

    def _list_url(self) -> str:
        return f"{self.base_url}/{_LIST_PATH}"

    def _pick_search_hit(self, html: Selector, name: str) -> str | None:
        """行标题与查找名 (去掉注记括号后) 相等才算命中; 确认不了返回 None."""
        if not name.strip():
            return None
        rows = html.xpath('//table[contains(@class,"tbllist") and contains(@class,"actress")]//tr[td]')
        for row in rows:
            title = extract_text(row, './/h2[contains(@class,"ttl")]/a/text()')
            href = extract_text(row, './/h2[contains(@class,"ttl")]/a/@href')
            if not title or not href or not _ACTRESS_HREF_RE.search(href):
                continue
            if is_same_name(_strip_annotations(title), name):
                return urljoin(self.base_url + "/", href.split("?", 1)[0])
        return None

    async def _scrape(self, url: str) -> ActorMetadata | None:
        text = await self.client.get_html(url, cookies=self.cookies)
        return parse_minnano_detail(text, page_url=url, base_url=self.base_url)


def parse_minnano_detail(html_text: str, *, page_url: str, base_url: str) -> ActorMetadata | None:
    html = Selector(text=html_text)
    profiles = html.xpath('//div[contains(@class,"act-profile")]')
    if not profiles:
        return None
    profile = profiles[0]

    raw_name = extract_text(profile, ".//h2/text()") or extract_text(profile, ".//h2//text()")
    if not raw_name:
        return None
    name, aliases = _parse_name_line(raw_name)

    # 別名行可重复; 每行一个旧名 + 读音, 与名字行同构解析.
    for row in profile.xpath('.//tr[td/span[normalize-space()="別名"]]'):
        raw_alias = extract_text(row, "string(./td/p)") or ""
        alias_name, alias_readings = _parse_name_line(raw_alias.strip())
        if alias_name:
            aliases.append(alias_name)
            aliases.extend(alias_readings)
    aliases = _dedupe_preserve([a for a in aliases if a and a != name])

    fields = _profile_fields(profile)
    birthday = _parse_birthday(fields.get("生年月日", ""))
    birthplace = _plain(fields.get("出身地", "")) or None
    height, bust, waist, hip, cup = _parse_figure(fields.get("サイズ", ""))

    image = extract_text(html, '//meta[@property="og:image"]/@content') or extract_text(
        html, '//div[contains(@class,"thumb")]//img[contains(@src,"p_actress")]/@src'
    )
    image_urls: list[str] = []
    if image:
        image_urls.append(_stable_url(urljoin(base_url + "/", image)))

    source = (
        extract_text(html, '//link[@rel="canonical"]/@href')
        or extract_text(html, '//meta[@property="og:url"]/@content')
        or page_url
    )

    return ActorMetadata(
        name=name,
        aliases=aliases,
        birthday=birthday,
        birthplace=birthplace,
        height=height,
        bust=bust,
        waist=waist,
        hip=hip,
        cup=cup,
        image_urls=image_urls,
        source_url=source,
        provider_ids={"minnano": source} if source else {},
    )


def parse_minnano_actress_list(html_text: str, *, base_url: str) -> tuple[_ActressEntry, ...]:
    """女优一览页 → 条目; 站点在 JSON-LD 里给出 ``ItemList``, 无结构化数据时返回空.

    结构不受本项目控制, 因此逐段容错: 任一块不是预期形态就跳过, 不因局部变化让整页报废.
    """
    entries: list[_ActressEntry] = []
    for block in _LD_JSON_RE.findall(html_text):
        try:
            payload: Any = json.loads(block.strip())
        except ValueError:
            continue
        entries.extend(_entries_from_ld(payload, base_url=base_url))
    seen: set[str] = set()
    unique: list[_ActressEntry] = []
    for entry in entries:
        if entry.url in seen:
            continue
        seen.add(entry.url)
        unique.append(entry)
    return tuple(unique)


def _entries_from_ld(payload: Any, *, base_url: str) -> list[_ActressEntry]:
    """收集所有 ``ItemList`` 下的 ``Person`` 条目."""
    out: list[_ActressEntry] = []
    for node in _iter_item_lists(payload):
        for element in node.get("itemListElement") or ():
            if not isinstance(element, dict):
                continue
            item = element.get("item")
            if not isinstance(item, dict) or item.get("@type") != "Person":
                continue
            name, url = item.get("name"), item.get("url")
            if not isinstance(name, str) or not isinstance(url, str) or not name.strip():
                continue
            out.append(_ActressEntry(name=normalize_name(name), url=_absolute(url, base_url)))
    return out


def _iter_item_lists(payload: Any) -> list[dict[str, Any]]:
    """深度优先收集 ``@type == "ItemList"`` 的节点.

    站点把女优清单挂在 ``CollectionPage.mainEntity`` 下, 也常见 ``@graph`` 或顶层直挂;
    按类型遍历而非按固定路径取, 结构挪位时索引不至于整体失效.
    """
    found: list[dict[str, Any]] = []
    stack: list[Any] = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            if current.get("@type") == "ItemList":
                found.append(current)
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return found


def _last_page(html_text: str, *, fallback: int) -> int:
    """分页链接里的最大页码; 站点把越界页码钳到末页, 因此探测值可直接用作上界."""
    pages = [int(m) for m in _PAGE_PARAM_RE.findall(html_text)]
    return min(max(pages) if pages else fallback, _INDEX_MAX_PAGES)


def _entry_key(name: str) -> str:
    """索引键: 去注记括号 + 折叠; 与检索命中判定同源."""
    return fold_name(_strip_annotations(name))


def _absolute(url: str, base: str) -> str:
    return urljoin(base + "/", url)


def _parse_name_line(raw: str) -> tuple[str, list[str]]:
    text = normalize_name(raw)
    m = _NAME_RE.match(text)
    if not m:
        return text, []
    name = _strip_annotations(m.group("name").strip())
    aliases = [a.strip() for a in (m.group("kana"), m.group("roma")) if a and a.strip()]
    return name, aliases


def _strip_annotations(name: str) -> str:
    # 尾部注记括号不写入 aliases.
    while True:
        stripped, _ = split_actor_aliases(name)
        if stripped == name:
            return name
        name = stripped


def _dedupe_preserve(values: list[str]) -> list[str]:
    out: list[str] = []
    for v in values:
        if v not in out:
            out.append(v)
    return out


def _profile_fields(profile: Selector) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in profile.xpath(".//tr"):
        key = extract_text(row, "./td/span/text()")
        if not key:
            continue
        val = extract_text(row, "string(./td/p)") or extract_text(row, "string(./td)")
        if val:
            val = val.strip()
            if val.startswith(key):
                val = val[len(key) :].strip()
            out[key] = val
    return out


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _parse_birthday(raw: str) -> str | None:
    return normalize_calendar_date(raw)


def _parse_figure(raw: str) -> tuple[int | None, int | None, int | None, int | None, str | None]:
    text = unicodedata.normalize("NFKC", raw)
    text = re.sub(r"<[^>]+>", "", text)
    m = _FIGURE_RE.search(text)
    cup_m = _CUP_RE.search(text)
    cup = (m.group("cup") if m and m.group("cup") else None) or (cup_m.group(1) if cup_m else None)
    if cup:
        cup = cup.upper()
    if not m:
        return None, None, None, None, cup
    return (
        int(m.group("t")),
        int(m.group("b")),
        int(m.group("w")),
        int(m.group("h")),
        cup,
    )


def _stable_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
