"""迁移: tasks.retry_at 为可空列, 存量行读回 NULL, 降级可移除."""

from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from tests.helpers import alembic_config

_PREV = "9054ebacea29"


@pytest.fixture
def alembic_cfg(tmp_path: Path) -> Config:
    return alembic_config(tmp_path / "migrate.db")


def _db_url(cfg: Config) -> str:
    """取迁移库连接串; 夹具 ``alembic_config`` 必然写入 url, 空值属夹具错误, 直接断言."""
    url = cfg.get_main_option("sqlalchemy.url")
    assert url, "alembic 配置缺少 sqlalchemy.url"
    return url


def _columns(cfg: Config) -> set[str]:
    engine = create_engine(_db_url(cfg))
    try:
        return {col["name"] for col in inspect(engine).get_columns("tasks")}
    finally:
        engine.dispose()


def _insert_task(cfg: Config) -> int:
    engine = create_engine(_db_url(cfg))
    try:
        with engine.begin() as conn:
            result = conn.execute(
                text(
                    "INSERT INTO tasks (type, status, payload, retries, priority, created_at) "
                    "VALUES ('SCRAPE', 'QUEUED', '{}', 0, 0, CURRENT_TIMESTAMP)"
                )
            )
            return int(result.lastrowid or 0)
    finally:
        engine.dispose()


def _read_retry_at(cfg: Config, task_id: int) -> Any:
    engine = create_engine(_db_url(cfg))
    try:
        with engine.begin() as conn:
            return conn.execute(text("SELECT retry_at FROM tasks WHERE id = :id"), {"id": task_id}).scalar_one()
    finally:
        engine.dispose()


def test_retry_at_added_as_nullable(alembic_cfg: Config) -> None:
    command.upgrade(alembic_cfg, _PREV)
    assert "retry_at" not in _columns(alembic_cfg)

    task_id = _insert_task(alembic_cfg)
    command.upgrade(alembic_cfg, "head")

    assert "retry_at" in _columns(alembic_cfg)
    assert _read_retry_at(alembic_cfg, task_id) is None, "存量行必须回读为 NULL (立即可认领)"

    engine = create_engine(_db_url(alembic_cfg))
    try:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE tasks SET retry_at = '2026-10-22 03:00:00.000000' WHERE id = :id"),
                {"id": task_id},
            )
    finally:
        engine.dispose()
    assert _read_retry_at(alembic_cfg, task_id) is not None


def test_retry_at_downgrade_drops_column(alembic_cfg: Config) -> None:
    command.upgrade(alembic_cfg, "head")
    assert "retry_at" in _columns(alembic_cfg)

    command.downgrade(alembic_cfg, _PREV)
    assert "retry_at" not in _columns(alembic_cfg)
