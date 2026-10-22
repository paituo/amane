#!/usr/bin/env python3
"""把历史「环境性失败」的刮削任务重新排入队列 (存量补救).

自动重排只对落地之后发生的失败生效; 本脚本处理库里已经以终态落库的那批, 判定规则与运行时同源
(``amane.handlers.retry_policy``), 因此不会把「站点可达但确实没有数据」的番号重新拖一遍.

用法::

    uv run python scripts/requeue_failed_scrapes.py            # 预演, 只打印将要重排的清单
    uv run python scripts/requeue_failed_scrapes.py --apply    # 写库
    uv run python scripts/requeue_failed_scrapes.py --apply --limit 50

写入形态: ``status=QUEUED``、``retries=0``、``retry_at=now+[0,600)s`` 随机, ``error`` / ``result`` 保留.
同一媒体文件只重排最新的一条失败任务, 其余保持 ``FAILED``; 该文件若已有排队中/运行中的任务也跳过,
避免同一文件被重复刮削 (队列没跑完时重复执行本脚本的情形).
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from amane.handlers.retry_policy import is_environmental_failure
from amane.observability.models import SiteOutcomeRecord

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_DB = _ROOT / "data" / "amane.db"
_SPREAD_S = 600
"""到点时间的随机散布上限: 让重排不在同一秒涌入队列."""


def _parse_outcomes(result_json: str | None) -> list[SiteOutcomeRecord]:
    if not result_json:
        return []
    try:
        payload = json.loads(result_json)
    except ValueError:
        return []
    rows: list[SiteOutcomeRecord] = []
    for raw in payload.get("outcomes") or []:
        try:
            rows.append(SiteOutcomeRecord.model_validate(raw))
        except ValueError:
            continue
    return rows


def _reason_key(outcomes: list[SiteOutcomeRecord]) -> str:
    """失败形态的聚合标签, 便于按原因统计."""
    if not outcomes:
        return "(无站点结果)"
    parts = sorted({f"{row.outcome.value}/{row.reason.value if row.reason else '-'}" for row in outcomes})
    return " + ".join(parts)


def _active_keys(conn: sqlite3.Connection) -> tuple[set[Any], set[Any]]:
    """已在排队/运行中的任务所占据的键, 分 ``media_file_id`` 与 ``number`` 两套."""
    media_ids: set[Any] = set()
    numbers: set[Any] = set()
    for row in conn.execute("select payload from tasks where status in ('QUEUED', 'RUNNING')"):
        payload = json.loads(row["payload"] or "{}")
        if payload.get("media_file_id") is not None:
            media_ids.add(payload["media_file_id"])
        if payload.get("number"):
            numbers.add(payload["number"])
    return media_ids, numbers


def _pick_candidates(conn: sqlite3.Connection) -> tuple[list[sqlite3.Row], Counter[str], Counter[str]]:
    """返回 (待重排行, 判定统计, 排除原因统计)."""
    pending = {row[0] for row in conn.execute("select id from media_files where status = 'PENDING'")}
    active_media, active_numbers = _active_keys(conn)
    skipped: Counter[str] = Counter()
    shapes: Counter[str] = Counter()
    latest: dict[Any, sqlite3.Row] = {}

    rows = conn.execute(
        "select id, payload, result from tasks where status = 'FAILED' and type = 'SCRAPE' order by id"
    ).fetchall()
    for row in rows:
        payload = json.loads(row["payload"] or "{}")
        media_id = payload.get("media_file_id")
        # 关联不到文件的行按番号去重; 关联了文件但文件已被别的任务刮成功的不再重排.
        if media_id is not None and media_id not in pending:
            skipped["媒体文件已不是 PENDING"] += 1
            continue
        key = media_id if media_id is not None else payload.get("number")
        # 该文件已有一条在队/运行中的任务时再排一条, 只会让同一文件被并发刮削两次.
        if key in (active_media if media_id is not None else active_numbers):
            skipped["该文件已有排队/运行中的任务"] += 1
            continue
        if not is_environmental_failure(_parse_outcomes(row["result"])):
            skipped["失败原因不可重排"] += 1
            continue
        # 同一键只保留最新一条: 旧行保持 FAILED, 作为历史留档.
        latest[key] = row

    # 形态统计放在去重之后, 否则同一文件的多次失败会重复计入, 与「待重排 N 条」对不上.
    for row in latest.values():
        shapes[_reason_key(_parse_outcomes(row["result"]))] += 1

    return sorted(latest.values(), key=lambda item: item["id"]), shapes, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=_DEFAULT_DB, help=f"数据库路径 (默认 {_DEFAULT_DB})")
    parser.add_argument("--apply", action="store_true", help="写库; 缺省为预演")
    parser.add_argument("--limit", type=int, default=0, help="最多重排多少条, 0 表示不限")
    args = parser.parse_args()

    if not args.db.exists():
        print(f"数据库不存在: {args.db}", file=sys.stderr)  # noqa: T201
        return 2

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        candidates, shapes, skipped = _pick_candidates(conn)
        if args.limit > 0:
            candidates = candidates[: args.limit]

        print(f"库: {args.db}")  # noqa: T201
        print(f"待重排: {len(candidates)} 条")  # noqa: T201
        for shape, n in shapes.most_common():
            print(f"  {n:5d}  {shape}")  # noqa: T201
        for reason, n in skipped.most_common():
            print(f"  排除 {n} 条: {reason}")  # noqa: T201
        if not args.apply:
            print("\n预演模式, 未写库. 确认无误后加 --apply 执行.")  # noqa: T201
            return 0

        now = datetime.now(UTC)
        rng = random.Random()
        payloads = [
            (
                "QUEUED",
                0,
                (now + timedelta(seconds=rng.uniform(0, _SPREAD_S))).replace(tzinfo=None).isoformat(sep=" "),
                row["id"],
            )
            for row in candidates
        ]
        # 单事务: 要么全部重排, 要么全部不动 (中途 Ctrl-C 不会留下半批).
        with conn:
            conn.executemany("update tasks set status = ?, retries = ?, retry_at = ? where id = ?", payloads)
        print(f"\n已重排 {len(payloads)} 条, 到点在 {_SPREAD_S} 秒内随机散布.")  # noqa: T201
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
