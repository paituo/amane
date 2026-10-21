"""多入口 (mirror) 故障切换: 只在连接层失败时换域, 并记住可用入口."""

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest

from amane.crawlers.base import Crawler, CrawlerProfile
from amane.crawlers.http import HttpClient, RequestError
from amane.crawlers.models import MediaMetadata, SearchQuery
from amane.net.errors import FailureKind, FailureReason, RequestFailure, SourceError

if TYPE_CHECKING:
    from amane.enums import SiteName


def _crawler(
    mirrors: list[str],
    *,
    failing: set[str] | None = None,
    kind: FailureKind = FailureKind.TIMEOUT,
) -> Crawler:
    """构造一个主域 + mirrors 的爬虫; ``failing`` 里的入口按 ``kind`` 抛连接层错误."""
    down = failing or set()

    class _MirrorCrawler(Crawler):
        @classmethod
        def profile(cls) -> CrawlerProfile:
            return CrawlerProfile(
                name=cast("SiteName", "fake"),
                base_url="https://main.example",
                mirrors=mirrors,
            )

        async def _search(self, query: SearchQuery, options=None) -> str | None:
            self.calls.append(self.base_url)  # type: ignore[attr-defined]
            if self.base_url in down:
                raise RequestError(self.base_url, RequestFailure(kind=kind, message="boom"))
            return f"{self.base_url}/v/{query.number}"

        async def _scrape(self, url: str, options=None) -> MediaMetadata | None:
            return MediaMetadata(number="T-1", title="t", source_url=url)

    crawler = _MirrorCrawler(client=AsyncMock(spec=HttpClient))
    crawler.calls = []  # type: ignore[attr-defined]
    return crawler


class TestMirrorFailover:
    @pytest.mark.asyncio
    async def test_switches_to_mirror_on_connect_failure(self):
        crawler = _crawler(["https://m1.example", "https://m2.example"], failing={"https://main.example"})
        result = await crawler.fetch(SearchQuery("T-1"))

        assert result is not None
        assert result.source_url == "https://m1.example/v/T-1"
        assert crawler.calls == ["https://main.example", "https://m1.example"]  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_remembers_working_mirror(self):
        crawler = _crawler(["https://m1.example"], failing={"https://main.example"})
        await crawler.fetch(SearchQuery("T-1"))
        await crawler.fetch(SearchQuery("T-2"))

        # 第二次直接从可用镜像开始, 不再撞一次主域.
        assert crawler.calls == [  # type: ignore[attr-defined]
            "https://main.example",
            "https://m1.example",
            "https://m1.example",
        ]

    @pytest.mark.asyncio
    async def test_no_switch_on_http_error(self):
        """4xx/5xx 换域无意义: 同一后端的镜像会给同样的回应."""
        crawler = _crawler(["https://m1.example"], failing={"https://main.example"}, kind=FailureKind.HTTP_STATUS)
        with pytest.raises(RequestError):
            await crawler.fetch(SearchQuery("T-1"))
        assert crawler.calls == ["https://main.example"]  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_no_switch_on_source_error(self):
        """拦截页 / 年龄验证不是连接问题, 不应换域."""

        class _Blocked(Crawler):
            @classmethod
            def profile(cls) -> CrawlerProfile:
                return CrawlerProfile(
                    name=cast("SiteName", "fake"), base_url="https://main.example", mirrors=["https://m1.example"]
                )

            async def _search(self, query: SearchQuery, options=None) -> str | None:
                self.calls.append(self.base_url)  # type: ignore[attr-defined]
                raise SourceError(FailureReason.AGE_VERIFICATION, url=self.base_url)

            async def _scrape(self, url: str, options=None) -> MediaMetadata | None:
                return None

        crawler = _Blocked(client=AsyncMock(spec=HttpClient))
        crawler.calls = []  # type: ignore[attr-defined]
        with pytest.raises(SourceError):
            await crawler.fetch(SearchQuery("T-1"))
        assert crawler.calls == ["https://main.example"]  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_exhausted_mirrors_raises(self):
        crawler = _crawler(["https://m1.example"], failing={"https://main.example", "https://m1.example"})
        with pytest.raises(RequestError):
            await crawler.fetch(SearchQuery("T-1"))
        assert len(crawler.calls) == 2  # type: ignore[attr-defined]

    def test_mirrors_reach_descriptor_urls(self):
        """mirrors 必须进 descriptor.urls, 否则限速与站点代理覆盖认不出镜像 host."""
        crawler = _crawler(["https://m1.example", "https://m2.example"])
        urls = crawler.profile().to_descriptor().urls
        assert "https://m1.example" in urls
        assert "https://m2.example" in urls
        assert "https://main.example" in urls

    def test_origins_dedup_and_strip(self):
        crawler = _crawler(["https://main.example/", "https://m1.example/"])
        assert crawler._origins == ("https://main.example", "https://m1.example")
