"""检索: 逐字段分词, 关键词与向量双路索引及其融合.

边界与取舍见 ``docs/dev/search.md``.
"""

from .embedding import (
    EmbeddingError,
    EmbeddingProvider,
    FastEmbedProvider,
    OpenAICompatibleProvider,
    WarmableProvider,
    fastembed_available,
)
from .indexer import MetadataIndexer, entry_of, entry_text
from .service import SearchResult, SearchService
from .store import IndexEntry, IndexStats, KeywordHit, SearchStore, SearchStoreError, VectorHit

__all__ = [
    "EmbeddingError",
    "EmbeddingProvider",
    "FastEmbedProvider",
    "IndexEntry",
    "IndexStats",
    "KeywordHit",
    "MetadataIndexer",
    "OpenAICompatibleProvider",
    "SearchResult",
    "SearchService",
    "SearchStore",
    "SearchStoreError",
    "VectorHit",
    "WarmableProvider",
    "entry_of",
    "entry_text",
    "fastembed_available",
]
