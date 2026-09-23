import os
from functools import lru_cache

from dotenv import dotenv_values
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIFANYI_", env_file=".env", extra="ignore")
    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "sqlite:///./data/aifanyi.db"
    redis_url: str = "redis://localhost:6379/0"
    dispatch: Literal["database", "celery"] = "database"
    checkpoint_url: str = "sqlite:///./data/checkpoints.db"
    dev_auth: bool = False
    dev_token: str = ""
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""
    oidc_algorithms: list[str] = ["RS256"]
    hymt_base_url: str = ""
    hymt_model: str = "hy-mt2-7b"
    hymt_revision: str = "unverified"
    qwen_base_url: str = ""
    qwen_model: str = ""
    qwen_revision: str = "unverified"
    qwen_precision: str = "existing-service-unverified"
    qwen_max_model_len: int = 16384
    qwen_max_output_tokens: int = 4096
    qwen_thinking: Literal["disabled", "enabled", "provider_default"] = "provider_default"
    qwen_thinking_field: Literal["chat_template_kwargs", "enable_thinking"] = "chat_template_kwargs"
    hymt_concurrency: int = 1
    qwen_concurrency: int = 2
    model_timeout: int = 120
    sync_wait_seconds: float = 60
    lease_seconds: int = 600
    retention_days: int = 30
    event_retention_days: int = 7
    allowed_model_hosts: list[str] = []
    callback_allowed_hosts: list[str] = []
    callback_allowed_cidrs: list[str] = []
    chat_dist: str = "apps/chat/dist"

    @model_validator(mode="after")
    def validate_environment(self):
        if self.dev_auth and (self.environment == "production" or len(self.dev_token) < 24):
            raise ValueError("Development auth requires a 24+ character token and non-production environment")
        if self.environment == "production":
            if not all([self.oidc_issuer, self.oidc_audience, self.oidc_jwks_url]):
                raise ValueError("Production OIDC configuration is required")
            if not self.database_url.startswith("postgresql") or self.dispatch != "celery":
                raise ValueError("Production requires PostgreSQL and Celery")
            if not self.checkpoint_url.startswith("postgresql"):
                raise ValueError("Production requires persistent PostgreSQL checkpoints")
        if self.hymt_concurrency < 1 or self.qwen_concurrency < 1:
            raise ValueError("Model concurrency must be positive")
        return self


@lru_cache
def get_settings():
    return Settings()


def secret_value(name):
    """Resolve Secret references without copying values into profiles, snapshots, or API responses."""
    if name in os.environ:
        return os.environ[name]
    return dotenv_values(".env", interpolate=False).get(name) or ""
