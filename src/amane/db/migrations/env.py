import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, event, pool
from sqlmodel import SQLModel

# 导入全部 table 模型以填充 metadata (含分类索引 / 用户注解表)
import amane.db.models  # noqa: F401
from amane.db.sqlite_migrate import enable_sqlite_transactional_ddl

config = context.config
if config.config_file_name is not None:
    # 迁移只是进程内的一步, 不允许按 fileConfig 默认值关闭调用方已配置好的 logger.
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _database_url() -> str:
    """CLI 模式的库路径: 与生产同源, 取 ``AMANE_DATA_DIR`` (默认 ``./data``)."""
    data_dir = Path(os.environ.get("AMANE_DATA_DIR", "data"))
    # 生产路径建库前会先建目录 (upgrade_sqlite_database), CLI 同样需要.
    data_dir.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{data_dir / 'amane.db'}"


# 应用启动经 config.attributes 传入连接, 无需 URL; 只有 CLI 在此取.
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", _database_url())

target_metadata = SQLModel.metadata

#: 检索索引表由 ``amane.search.store`` 在运行时按需创建, 属可重建的派生数据:
#: FTS5 虚表无法用 SQLModel 建模, 放进迁移只会让 autogenerate 提议删掉它们.
#: vec0 表不在本库 (见 search.store 的模块说明), 因此不在此列. 必须显式排除:
#: ``include_object`` 在反射之后才生效, 而影子表会在反射阶段被当成未知表.
_RUNTIME_TABLES = frozenset(
    {
        "metadata_fts",
        "metadata_fts_data",
        "metadata_fts_idx",
        "metadata_fts_content",
        "metadata_fts_docsize",
        "metadata_fts_config",
        "metadata_embeddings",
    }
)


def _include_object(object_: object, name: str | None, type_: str, reflected: bool, compare_to: object) -> bool:
    """迁移只管业务表; 检索索引表与 FTS/vec 影子表一律跳过."""
    del object_, reflected, compare_to
    if type_ == "table" and name is not None:
        return name not in _RUNTIME_TABLES
    return True


def run_migrations_offline() -> None:
    """以离线模式运行迁移"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        transactional_ddl=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """以在线模式运行迁移.

    SQLite 使用事务性 DDL (见 sqlite_migrate.enable_sqlite_transactional_ddl):
    单次 revision 失败时 DDL 与 alembic_version 一并回滚.
    """
    # 支持通过 config.attributes 传入已有连接 (应用启动时调用)
    connectable = config.attributes.get("connection")

    if connectable is not None:
        context.configure(
            connection=connectable,
            target_metadata=target_metadata,
            transactional_ddl=True,
            include_object=_include_object,
        )
        with context.begin_transaction():
            context.run_migrations()
    else:
        # CLI 调用 (alembic upgrade/downgrade) - 自行创建连接
        connectable = engine_from_config(
            config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool
        )

        @event.listens_for(connectable, "connect")
        def _enable_txn_ddl(dbapi_connection, _connection_record) -> None:
            enable_sqlite_transactional_ddl(dbapi_connection)

        try:
            with connectable.connect() as connection:
                context.configure(
                    connection=connection,
                    target_metadata=target_metadata,
                    transactional_ddl=True,
                    include_object=_include_object,
                )
                with context.begin_transaction():
                    context.run_migrations()
                connection.commit()
        finally:
            connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
