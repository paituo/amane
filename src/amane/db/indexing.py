"""业务写入 → 检索索引的通知契约.

放在 ``db`` 侧是为了固定依赖方向: ``db`` 不认识 ``search``, 由 ``search`` 实现本协议、
在组合根注入. 回调一律同步且非阻塞 —— 索引是派生数据, 写事务不能等它.
"""

from collections.abc import Iterable
from typing import Protocol, runtime_checkable


@runtime_checkable
class MetadataIndexSink(Protocol):
    """影片行的写入已提交 / 已删除. 实现方须自行排队, 不得在此阻塞或抛错."""

    def notify_write(self, metadata_ids: Iterable[int]) -> None: ...

    def notify_delete(self, metadata_ids: Iterable[int]) -> None: ...
