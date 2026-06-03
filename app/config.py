"""Application configuration loaded from environment variables."""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Devin
    devin_api_key: str
    devin_api_base: str = "https://api.devin.ai/v1"

    # GitHub
    github_token: str
    github_repo: str  # e.g. "thesaltree/superset"
    github_webhook_secret: str

    # Storage
    database_url: str = "sqlite:////app/data/automation.db"

    # Tuning
    max_concurrent_workers: int = 3
    worker_label: str = "devin-task"


@lru_cache
def get_settings() -> Settings:
    return Settings()
