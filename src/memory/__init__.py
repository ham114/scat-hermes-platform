"""为 LangGraph 提供可切换的短期 checkpoint 和长期 Store 后端。

Checkpointer 保存 thread 级图状态，用于多轮对话、interrupt 恢复和历史查询；
Store 保存可跨 thread 读取的长期信息。二者都不替代 SCAT/UAV 业务数据库。
"""

from contextlib import AbstractAsyncContextManager

from langgraph.checkpoint.mongodb import MongoDBSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from core.settings import DatabaseType, settings
from memory.mongodb import get_mongo_saver
from memory.postgres import get_postgres_saver, get_postgres_store
from memory.sqlite import get_sqlite_saver, get_sqlite_store


def initialize_database() -> AbstractAsyncContextManager[
    AsyncSqliteSaver | AsyncPostgresSaver | MongoDBSaver
]:
    """按配置创建异步 checkpointer 上下文管理器。

    图只依赖统一 saver 接口；开发环境可用 SQLite，部署环境可切换到
    PostgreSQL 或 MongoDB，而不需要修改 Agent 的节点和边。
    """
    if settings.DATABASE_TYPE == DatabaseType.POSTGRES:
        return get_postgres_saver()
    if settings.DATABASE_TYPE == DatabaseType.MONGO:
        return get_mongo_saver()
    else:  # Default to SQLite
        return get_sqlite_saver()


def initialize_store():
    """按配置创建跨 thread 的长期 Store。

    Store 与 checkpointer 分开初始化，避免把“对话执行状态”和“用户长期
    信息”混为一种数据生命周期。
    """
    if settings.DATABASE_TYPE == DatabaseType.POSTGRES:
        return get_postgres_store()
    # TODO: Add Mongo store - https://pypi.org/project/langgraph-store-mongodb/
    else:  # Default to SQLite
        return get_sqlite_store()


__all__ = ["initialize_database", "initialize_store"]
