"""minnano 演员来源 — 一览索引构建 / 缓存与检索路径.

站内检索入口处于 Cloudflare 托管挑战下, 检索改由一览页索引承担. 用例覆盖索引的解析、
落盘重载、过期兜底 + 后台重建、冷启动非阻塞、重建去重与失败保留旧索引等.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import pytest

from amane.crawlers.actor.sites import minnano as mod
from amane.crawlers.http import HttpClient
from amane.net.errors import FailureKind, RequestError, RequestFailure

BASE = "https://www.minnano-av.com"


def _person(name: str, url: str) -> dict[str, Any]:
    return {"@type": "ListItem", "item": {"@type": "Person", "name": name, "url": url}}


def _ld(payload: Any) -> str:
    return f'<script type="application/ld+json">{json.dumps(payload, ensure_ascii=False)}</script>'


def _list_page(names: list[str], *, last_page: int = 1, page: int = 1) -> str:
    """构造一览页: ItemList 挂在 CollectionPage.mainEntity 下 (与站点一致).

    URL 按页偏移生成, 否则各页产出同一批地址, 去重后只剩一页的量.
    """
    payload = {
        "@type": "CollectionPage",
        "mainEntity": {
            "@type": "ItemList",
            "numberOfItems": len(names) * last_page,
            "itemListElement": [_person(n, f"{BASE}/actress{(page - 1) * 100 + i}.html") for i, n in enumerate(names)],
        },
    }
    pager = f'<a href="/actress_list.php?page={last_page}">{last_page}</a>'
    return _ld(payload) + pager


class _FakeWeb:
    """按 URL 子串路由到预设正文; 记录请求并可按页码注入失败."""

    def __init__(self, routes: dict[str, str], *, fail_pages: frozenset[int] = frozenset()) -> None:
        self._routes = routes
        self._fail_pages = fail_pages
        self.calls: list[str] = []

    async def get_text(self, url: str, **kwargs: object) -> str:
        self.calls.append(url)
        match = re.search(r"page=(\d+)", url)
        if match and int(match.group(1)) in self._fail_pages:
            raise RequestError(url, RequestFailure(kind=FailureKind.CURL, message="boom"))
        for pattern, body in self._routes.items():
            if pattern in url:
                return body
        return "<html></html>"


def _crawler(tmp_path: Path, routes: dict[str, str], **kw: Any) -> tuple[mod.MinnanoActorCrawler, _FakeWeb]:
    web = _FakeWeb(routes, **kw)
    client = HttpClient(web)  # type: ignore[arg-type]  # 测试替身只实现 get_text
    return mod.MinnanoActorCrawler(client=client, data_dir=tmp_path), web


def _set_cache(tmp_path: Path, entries: list[tuple[str, str]], *, age_seconds: float = 0.0) -> None:
    """写一份磁盘索引缓存; ``age_seconds > 0`` 表示已过期 (用于兜底/重建用例)."""
    payload = {
        "version": 1,
        "built_at": time.time() - age_seconds,
        "entries": [{"name": n, "url": u} for n, u in entries],
    }
    (tmp_path / "minnano_actress_index.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


async def _drain(crawler: mod.MinnanoActorCrawler) -> None:
    """等待并消费后台重建任务, 避免用例结束时任务仍 pending 的告警."""
    task = crawler._index._rebuild_task
    if task is not None and not task.done():
        await task


# (用例名, HTML, 期望条目数)
_LD_CASES: list[tuple[str, str, int]] = [
    ("顶层 ItemList", _ld({"@type": "ItemList", "itemListElement": [_person("甲", f"{BASE}/actress1.html")]}), 1),
    (
        "mainEntity 嵌套",
        _ld(
            {
                "@type": "CollectionPage",
                "mainEntity": {
                    "@type": "ItemList",
                    "itemListElement": [
                        _person("乙", f"{BASE}/actress2.html"),
                    ],
                },
            }
        ),
        1,
    ),
    (
        "@graph 包裹",
        _ld({"@graph": [{"@type": "ItemList", "itemListElement": [_person("丙", f"{BASE}/actress3.html")]}]}),
        1,
    ),
    ("节点不含 Person", _ld({"@type": "ItemList", "itemListElement": [{"@type": "Thing"}]}), 0),
    ("ItemList 无条目", _ld({"@type": "ItemList"}), 0),
    ("非法 JSON", '<script type="application/ld+json">{oops</script>', 0),
    ("无 JSON-LD", "<html><body>empty</body></html>", 0),
    (
        "顶层列表混合",
        _ld(
            [
                {"@type": "BreadcrumbList", "itemListElement": []},
                {"@type": "ItemList", "itemListElement": [_person("丁", f"{BASE}/actress4.html")]},
            ]
        ),
        1,
    ),
]


@pytest.mark.parametrize(("case", "html", "expected"), _LD_CASES, ids=[c[0] for c in _LD_CASES])
def test_parse_actress_list(case: str, html: str, expected: int) -> None:
    entries = mod.parse_minnano_actress_list(html, base_url=BASE)
    assert len(entries) == expected


def test_parse_actress_list_dedupes_by_url() -> None:
    html = _ld(
        {
            "@type": "ItemList",
            "itemListElement": [
                _person("甲", f"{BASE}/actress1.html"),
                _person("甲", f"{BASE}/actress1.html"),
                _person("乙", f"{BASE}/actress2.html"),
            ],
        }
    )
    entries = mod.parse_minnano_actress_list(html, base_url=BASE)
    assert [e.name for e in entries] == ["甲", "乙"]


def test_parse_actress_list_normalizes_name() -> None:
    html = _ld({"@type": "ItemList", "itemListElement": [_person("  甲 乙 ", f"{BASE}/actress1.html")]})
    entries = mod.parse_minnano_actress_list(html, base_url=BASE)
    assert entries[0].name == "甲 乙"


# (用例名, HTML, fallback, 期望页码)
_PAGE_CASES: list[tuple[str, str, int, int]] = [
    ("单页无分页链接", "<html></html>", 1, 1),
    ("分页链接取最大值", '<a href="?page=2">2</a><a href="?page=831">831</a>', 1, 831),
    ("无分页回退 fallback", "<html></html>", 7, 7),
    ("超过上限被钳制", '<a href="?page=99999">x</a>', 1, mod._INDEX_MAX_PAGES),
]


@pytest.mark.parametrize(("case", "html", "fallback", "expected"), _PAGE_CASES, ids=[c[0] for c in _PAGE_CASES])
def test_last_page(case: str, html: str, fallback: int, expected: int) -> None:
    assert mod._last_page(html, fallback=fallback) == expected


# (用例名, 查询名, 期望 URL 后缀); 一览页按传入顺序生成 actress{i}.html
_LOOKUP_CASES: list[tuple[str, str, str]] = [
    ("精确艺名", "甲", "/actress0.html"),
    ("大小写无关", "abc", "/actress1.html"),
    ("剥离注记括号", "丙(tenshigao)", "/actress2.html"),
    ("未收录返回 None", "不存在的人", ""),
]


@pytest.mark.parametrize(("case", "query", "suffix"), _LOOKUP_CASES, ids=[c[0] for c in _LOOKUP_CASES])
@pytest.mark.asyncio
async def test_index_lookup(case: str, query: str, suffix: str, tmp_path: Path) -> None:
    # 预置新鲜缓存: 验证索引命中路径不出网.
    _set_cache(
        tmp_path,
        [
            ("甲", f"{BASE}/actress0.html"),
            ("ABC", f"{BASE}/actress1.html"),
            ("丙(tenshigao)", f"{BASE}/actress2.html"),
        ],
    )
    crawler, _ = _crawler(tmp_path, {})
    hit = await crawler._index.lookup(query)
    if suffix:
        assert hit == f"{BASE}{suffix}"
    else:
        assert hit is None
    task = crawler._index._rebuild_task
    assert task is None or task.done(), "新鲜缓存不应触发后台重建"


@pytest.mark.asyncio
async def test_index_persists_and_reloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """构建一次之后落盘; 新实例即使只允许抓 1 页, 仍应命中深处的名字."""
    monkeypatch.setattr(mod, "_INDEX_MIN_ENTRIES", 50)
    routes = {
        f"actress_list.php?page={n}": _list_page([f"演员{n}-{i}" for i in range(30)], last_page=3, page=n)
        for n in (1, 2, 3)
    }
    crawler, _ = _crawler(tmp_path, routes)
    entries = await crawler._index._build()
    crawler._index._write_cache(entries)
    cache = tmp_path / "minnano_actress_index.json"
    assert cache.is_file()

    # 限量 1 页: 仍能命中第 3 页的名字 ⇒ 索引确实来自磁盘缓存
    monkeypatch.setattr(mod, "_INDEX_MAX_PAGES", 1)
    crawler2, web2 = _crawler(tmp_path, {"actress_list.php?page=1": _list_page(["甲"])})
    hit = await crawler2._index.lookup("演员3-7")
    assert hit == f"{BASE}/actress207.html"
    assert web2.calls == [], f"命中缓存不应出网, 实际请求 {web2.calls}"


@pytest.mark.asyncio
async def test_stale_cache_is_served_and_rebuilt_in_background(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """过期缓存仍作兜底: 命中旧名不出网, 同时后台重建新索引."""
    monkeypatch.setattr(mod, "_INDEX_MIN_ENTRIES", 2)
    _set_cache(tmp_path, [("甲", f"{BASE}/actress0.html")], age_seconds=mod._INDEX_TTL_SECONDS + 1)
    routes = {"actress_list.php?page=1": _list_page(["甲", "乙"])}
    crawler, web = _crawler(tmp_path, routes)
    # 兜底命中旧名, 不出网
    hit = await crawler._index.lookup("甲")
    assert hit == f"{BASE}/actress0.html"
    assert web.calls == [], "过期缓存兜底命中, 不应出网"
    # 后台重建已被触发
    assert crawler._index._rebuild_task is not None and not crawler._index._rebuild_task.done()
    # 重建完成后索引被替换且落盘, 收录新增的乙
    await _drain(crawler)
    assert await crawler._index.lookup("乙") == f"{BASE}/actress1.html"
    assert (tmp_path / "minnano_actress_index.json").is_file()


@pytest.mark.asyncio
async def test_cold_start_does_not_block_and_rebuilds_in_background(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无缓存冷启动: 首次查询立即返回 (不阻塞), 后台构建完成后填充索引."""
    monkeypatch.setattr(mod, "_INDEX_MIN_ENTRIES", 2)
    routes = {"actress_list.php?page=1": _list_page(["甲", "乙"])}
    crawler, web = _crawler(tmp_path, routes)
    # 冷启动占位: 首次 lookup 立即返回 None, 不抓全站
    assert await crawler._index.lookup("甲") is None
    assert web.calls == [], "冷启动不应在查询路径同步出网"
    assert crawler._index._rebuild_task is not None and not crawler._index._rebuild_task.done()
    # 后台构建完成后索引可用
    await _drain(crawler)
    assert await crawler._index.lookup("甲") == f"{BASE}/actress0.html"
    assert (tmp_path / "minnano_actress_index.json").is_file()


@pytest.mark.asyncio
async def test_warmup_is_nonblocking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """warmup 不阻塞: 冷启动也无缓存时立即返回, 后台任务接管构建."""
    monkeypatch.setattr(mod, "_INDEX_MIN_ENTRIES", 1)
    routes = {"actress_list.php?page=1": _list_page(["甲"])}
    crawler, web = _crawler(tmp_path, routes)
    await crawler.warmup()  # 不应挂起
    assert web.calls == [], "warmup 不应同步出网"
    assert crawler._index._rebuild_task is not None and not crawler._index._rebuild_task.done()
    await _drain(crawler)
    assert await crawler._index.lookup("甲") == f"{BASE}/actress0.html"


@pytest.mark.asyncio
async def test_rebuild_deduplicated(tmp_path: Path) -> None:
    """多次查询/预热只触发一个后台重建任务."""
    _set_cache(tmp_path, [("甲", f"{BASE}/actress0.html")], age_seconds=mod._INDEX_TTL_SECONDS + 1)
    crawler, _ = _crawler(tmp_path, {"actress_list.php?page=1": _list_page(["甲", "乙"])})
    await crawler._index._ensure()
    first = crawler._index._rebuild_task
    assert first is not None
    await crawler._index._ensure()  # 再次进入: 不应新建
    assert crawler._index._rebuild_task is first
    await _drain(crawler)


@pytest.mark.asyncio
async def test_rebuild_failure_keeps_previous(tmp_path: Path) -> None:
    """后台重建失败 (网络): 旧索引继续兜底, 不替换为空."""
    _set_cache(tmp_path, [("甲", f"{BASE}/actress0.html")], age_seconds=mod._INDEX_TTL_SECONDS + 1)
    # 所有分页请求失败
    crawler, _web = _crawler(tmp_path, {}, fail_pages=frozenset({1}))
    await crawler._index._ensure()
    await _drain(crawler)
    # 旧索引仍在, 且未被替换
    assert await crawler._index.lookup("甲") == f"{BASE}/actress0.html"
    assert crawler._index._by_key == {"甲": f"{BASE}/actress0.html"}


@pytest.mark.asyncio
async def test_search_prefers_index_over_remote(tmp_path: Path) -> None:
    """索引命中时不出网: 站内检索入口被挑战保护, 少发一次就少一次 403."""
    _set_cache(tmp_path, [("甲", f"{BASE}/actress0.html"), ("乙", f"{BASE}/actress1.html")])
    crawler, web = _crawler(tmp_path, {})
    url = await crawler._search("甲")
    assert url == f"{BASE}/actress0.html"
    assert web.calls == [], "索引命中不应出网"


@pytest.mark.asyncio
async def test_search_falls_back_to_remote(tmp_path: Path) -> None:
    """索引未收录时走站内检索: 结果行艺名与查找名相等才算命中."""
    search_html = """
    <table class="tbllist actress"><tr><td>
      <h2 class="ttl"><a href="actress777.html">甲（か / Ko）</a></h2>
    </td></tr></table>
    """
    crawler, web = _crawler(
        tmp_path,
        {"actress_list.php?page=1": _list_page(["乙"]), "search_result.php": search_html},
    )
    url = await crawler._search("甲")
    assert url == f"{BASE}/actress777.html"
    assert any("search_result.php" in call for call in web.calls)
    await _drain(crawler)


@pytest.mark.asyncio
async def test_build_tolerates_page_failure(tmp_path: Path) -> None:
    """个别分页失败不拖垮索引; 成功页照常收录."""
    routes = {
        "actress_list.php?page=1": _list_page(["甲", "乙"], last_page=3),
        "actress_list.php?page=3": _list_page(["丙"]),
    }
    crawler, _ = _crawler(tmp_path, routes, fail_pages=frozenset({2}))
    entries = await crawler._index._build()
    assert [e.name for e in entries] == ["甲", "乙", "丙"]


@pytest.mark.asyncio
async def test_build_returns_empty_when_first_page_fails(tmp_path: Path) -> None:
    crawler, _ = _crawler(tmp_path, {"actress_list.php?page=1": _list_page(["甲"])}, fail_pages=frozenset({1}))
    assert await crawler._index._build() == ()
    assert await crawler._index.lookup("甲") is None
    await _drain(crawler)


@pytest.mark.asyncio
async def test_failed_cold_start_backs_off_before_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """冷启动构建失败后进入冷却: 冷却期内不再起新任务, 否则每次查询都抓一遍全站."""
    monkeypatch.setattr(mod, "_INDEX_MIN_ENTRIES", 1)
    crawler, _ = _crawler(tmp_path, {}, fail_pages=frozenset({1}))

    assert await crawler._index.lookup("甲") is None
    first = crawler._index._rebuild_task
    assert first is not None
    await _drain(crawler)

    assert crawler._index._rebuild_failures == 1
    assert crawler._index._retry_at > time.time(), "失败后应置冷却截止时间"
    # 冷却期内再查询: 不应新建任务
    assert await crawler._index.lookup("甲") is None
    assert crawler._index._rebuild_task is first


@pytest.mark.asyncio
async def test_retry_after_cooldown_recovers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """冷却到期后允许再试: 网络恢复时冷启动失败不会永久瘫痪."""
    monkeypatch.setattr(mod, "_INDEX_MIN_ENTRIES", 1)
    routes = {"actress_list.php?page=1": _list_page(["甲"])}
    crawler, web = _crawler(tmp_path, routes, fail_pages=frozenset({1}))

    await crawler._index.lookup("甲")
    await _drain(crawler)
    assert await crawler._index.lookup("甲") is None

    # 冷却到期 + 网络恢复
    crawler._index._retry_at = 0.0
    web._fail_pages = frozenset()
    await crawler._index.lookup("甲")
    await _drain(crawler)
    assert await crawler._index.lookup("甲") == f"{BASE}/actress0.html"
    assert crawler._index._rebuild_failures == 0


@pytest.mark.asyncio
async def test_in_memory_index_refreshes_after_ttl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """长跑进程: 内存索引过期也应触发后台重建, 否则 7 天后再不收录新增女优."""
    monkeypatch.setattr(mod, "_INDEX_MIN_ENTRIES", 2)
    _set_cache(tmp_path, [("甲", f"{BASE}/actress0.html"), ("乙", f"{BASE}/actress1.html")])
    routes = {"actress_list.php?page=1": _list_page(["甲", "乙", "丙"])}
    crawler, _ = _crawler(tmp_path, routes)

    assert await crawler._index.lookup("丙") is None  # 触发首次加载 (新鲜, 不重建)
    assert crawler._index._rebuild_task is None

    # 模拟进程跑过 TTL: 内存态已过期
    crawled_at = time.time() - mod._INDEX_TTL_SECONDS - 1
    crawler._index._built_at = crawled_at

    assert await crawler._index.lookup("甲") == f"{BASE}/actress0.html", "过期期间旧索引仍兜底"
    assert crawler._index._rebuild_task is not None, "内存态过期应调度后台重建"
    await _drain(crawler)
    assert await crawler._index.lookup("丙") == f"{BASE}/actress2.html"
    assert crawler._index._built_at > crawled_at


@pytest.mark.asyncio
async def test_partial_rebuild_keeps_complete_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """残缺重建 (大量分页失败) 不替换完整索引: 否则大批女优会静默消失."""
    monkeypatch.setattr(mod, "_INDEX_MIN_ENTRIES", 1)
    _set_cache(
        tmp_path,
        [(f"旧{i}", f"{BASE}/actress{i}.html") for i in range(10)],
        age_seconds=mod._INDEX_TTL_SECONDS + 1,
    )
    # 新构建只有 4 条, 不足旧索引的一半
    routes = {"actress_list.php?page=1": _list_page(["新0", "新1", "新2", "新3"])}
    crawler, _ = _crawler(tmp_path, routes)

    assert await crawler._index.lookup("旧0") == f"{BASE}/actress0.html"
    await _drain(crawler)
    assert await crawler._index.lookup("旧9") == f"{BASE}/actress9.html", "残缺重建不应替换完整索引"
    assert await crawler._index.lookup("新0") is None
    assert crawler._index._rebuild_failures == 1
