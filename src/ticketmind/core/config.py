from typing import Literal
from pydantic_settings import BaseSettings,SettingsConfigDict
from pydantic import PostgresDsn
from functools import lru_cache
from pydantic import Field,SecretStr, model_validator
from pathlib import Path
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


class AuthSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8",
                                      env_prefix="TICKETMIND_", extra="ignore")
    operator_token: SecretStr | None = None
    reviewer_token: SecretStr | None = None
    operator_id: str = Field(default="operator", min_length=1, max_length=64)
    reviewer_id: str = Field(default="reviewer", min_length=1, max_length=64)

    @model_validator(mode="after")
    def distinct_credentials(self):
        tokens = [token.get_secret_value() for token in (self.operator_token, self.reviewer_token) if token]
        if any(len(token) < 32 or not token.isascii() or any(c.isspace() for c in token) for token in tokens):
            raise ValueError("身份凭据必须为至少 32 个非空白 ASCII 字符")
        if len(tokens) == 2 and tokens[0] == tokens[1]:
            raise ValueError("operator 和 reviewer 必须使用独立凭据")
        if self.operator_id == self.reviewer_id:
            raise ValueError("operator 和 reviewer 必须使用不同 actor_id")
        return self


class ProcessingSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8",
                                      env_prefix="TICKETMIND_", extra="ignore")
    agent_version: str = "ticketmind-m1"
    retrieval_mode: Literal["dense"] = "dense"
    retrieval_top_k: int = Field(default=3, ge=1, le=100)
    processing_timeout_seconds: float = Field(default=90, gt=0, le=300)
    corpus_path: Path = Path(__file__).resolve().parents[3] / "data/synthetic/v2/historical_cases.jsonl"
