"""爬虫工厂测试"""

from typing import TYPE_CHECKING, cast

import pytest

from amane.crawlers.actor import actor_registry
from amane.crawlers.actor.base import ActorCrawler
from amane.crawlers.base import Crawler, CrawlerProfile
from amane.crawlers.factory import CrawlerFactory
from amane.crawlers.registry import registry

if TYPE_CHECKING:
    from amane.enums import SiteName


class _FakeCrawler(Crawler):
    """用于测试的假爬虫 - 只记录构造次数, 不做实际获取."""

    @classmethod
    def profile(cls) -> CrawlerProfile:
        return CrawlerProfile(name=cast("SiteName", "fake_test_site"), base_url="https://fake.example.com")

    def __init__(self, client, config=None):
        super().__init__(client, config=config)
        self._constructed = True

    async def _search(self, query, options=None):
        return None

    async def _scrape(self, url, options=None):
        return None


@pytest.fixture(autouse=True)
def _register_fake_crawler():
    """注册假爬虫并在测试后清理."""
    registry.register(_FakeCrawler)
    yield
    registry._crawlers.pop("fake_test_site", None)


class TestCrawlerFactoryGet:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_returns_cached_instance_on_second_call(self, http_client):
        factory = CrawlerFactory(http_client)
        c1 = await factory.get("fake_test_site")
        c2 = await factory.get("fake_test_site")
        assert c1 is c2

    @pytest.mark.asyncio(loop_scope="function")
    async def test_returns_none_for_unregistered_name(self, http_client):
        factory = CrawlerFactory(http_client)
        result = await factory.get("nonexistent_site")
        assert result is None


class TestCrawlerFactoryGetCrawlers:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_returns_only_registered_crawlers(self, http_client):
        factory = CrawlerFactory(http_client)
        result = await factory.get_crawlers(["fake_test_site", "nonexistent"])
        assert len(result) == 1
        assert "fake_test_site" in result
        assert "nonexistent" not in result


@pytest.mark.asyncio(loop_scope="function")
async def test_javdb_film_and_actor_are_distinct_instances(http_client):
    from amane.crawlers.actor.sites.javdb import JavDBActorCrawler
    from amane.crawlers.sites.javdb import JavDBCrawler

    factory = CrawlerFactory(http_client)
    film = await factory.get("javdb")
    actor = await factory.get_actor("javdb")
    assert isinstance(film, JavDBCrawler)
    assert isinstance(actor, JavDBActorCrawler)
    assert film is not actor


@pytest.mark.asyncio(loop_scope="function")
async def test_theporndb_film_and_actor_are_distinct_instances(http_client):
    from amane.crawlers.actor.sites.theporndb import ThePornDBActorCrawler
    from amane.crawlers.sites.theporndb import ThePornDBCrawler

    factory = CrawlerFactory(http_client)
    film = await factory.get("theporndb")
    actor = await factory.get_actor("theporndb")
    assert isinstance(film, ThePornDBCrawler)
    assert isinstance(actor, ThePornDBActorCrawler)
    assert film is not actor


class _WarmActorCrawler(ActorCrawler):
    """记录 warmup 调用的演员来源替身."""

    warmed = 0

    @classmethod
    def profile(cls) -> CrawlerProfile:
        return CrawlerProfile(name=cast("SiteName", "fake_warm_actor"), base_url="https://warm.example.com")

    async def warmup(self) -> None:
        type(self).warmed += 1

    async def _search(self, name: str) -> str | None:
        return None

    async def _scrape(self, url: str) -> None:
        return None


@pytest.fixture
def _register_warm_actor():
    """注册假演员来源并在测试后清理."""
    actor_registry.register(_WarmActorCrawler)
    _WarmActorCrawler.warmed = 0
    yield
    actor_registry._classes.pop("fake_warm_actor", None)


@pytest.mark.asyncio(loop_scope="function")
async def test_connectivity_probe_does_not_trigger_warmup(http_client, _register_warm_actor) -> None:
    """探测途经 get_actor: 不应预热, 否则点一次「检测连通性」就会在后台掀起全站抓取."""
    factory = CrawlerFactory(http_client)
    await factory.get_actor("fake_warm_actor")
    assert factory._actor_warm_tasks == {}
    assert _WarmActorCrawler.warmed == 0


@pytest.mark.asyncio(loop_scope="function")
async def test_actor_scrape_triggers_warmup(http_client, _register_warm_actor) -> None:
    """真实刮削取实例时 fire-and-forget 预热; 重复取用不叠加任务."""
    factory = CrawlerFactory(http_client)
    assert await factory.get_actor_crawlers(["fake_warm_actor"])
    await factory._actor_warm_tasks["fake_warm_actor"]
    assert _WarmActorCrawler.warmed == 1

    await factory.get_actor_crawlers(["fake_warm_actor"])
    first = factory._actor_warm_tasks["fake_warm_actor"]
    await first
    assert _WarmActorCrawler.warmed == 2, "上一次已结束, 允许再预热 (索引可能又过期)"
