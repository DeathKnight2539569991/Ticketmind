from typing import Literal
from pydantic_settings import BaseSettings,SettingsConfigDict
from pydantic import PostgresDsn
from functools import lru_cache
from pydantic import Field,SecretStr
class Settings(BaseSettings):
    app_name:str = "TicketMind"
    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    database_url: PostgresDsn
    model_config=SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="TICKETMIND_",
        extra="ignore"
        )
@lru_cache()
def get_settings() -> Settings:
    """Return the application settings."""
    return Settings()
class QwenSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="TICKETMIND_",
        extra="ignore",
    )
    api_key: SecretStr = Field(
        validation_alias="DASHSCOPE_API_KEY",
    )
    workspace_id: str = Field(
        validation_alias="DASHSCOPE_WORKSPACE_ID",
        min_length=1,
    )
    model:str="qwen3.7-flash"
    embedding_model:str="text-embedding-v4"
class MilvusSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="TICKETMIND_MILVUS_",
        extra="ignore",
    )
    uri:str=Field(min_length=1)
    timeout_seconds:float=Field(default=10.0,gt=0.0)
