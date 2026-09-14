from __future__ import annotations

from functools import lru_cache

from app.aws.dynamodb_db import DynamoDatabase
from app.config import get_settings
from app.db import Database
from app.services.agent_service import AgentService
from app.services.anthropic_agent_service import AnthropicAgentService
from app.services.knowledge_files import KnowledgeFileService


@lru_cache
def get_db() -> Database | DynamoDatabase:
    settings = get_settings()
    if settings.storage_backend == "dynamodb":
        return DynamoDatabase(
            settings.dynamodb_table_name,
            region_name=settings.aws_region,
            retention_days=settings.chat_history_retention_days,
        )
    db = Database(settings.sqlite_path)
    db.init_schema()
    return db


@lru_cache
def get_knowledge_file_service() -> KnowledgeFileService:
    return KnowledgeFileService()


@lru_cache
def get_agent_service() -> AgentService | AnthropicAgentService:
    settings = get_settings()
    if settings.llm_provider == "anthropic":
        return AnthropicAgentService(settings, get_db(), get_knowledge_file_service())
    return AgentService(settings, get_db(), get_knowledge_file_service())
