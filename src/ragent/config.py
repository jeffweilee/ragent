"""Central settings — loaded once at entry-point startup before any other ragent imports.

pydantic_settings reads .env into this object and validates types/required fields.
load_dotenv() is called first so that scattered os.environ.get() calls throughout
the codebase also see .env values at module-import time.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Auth / Token Exchange ---
    auth_url: str = Field(alias="AUTH_URL")
    auth_client_id: str = Field(alias="AUTH_CLIENT_ID")
    auth_client_secret: str = Field(alias="AUTH_CLIENT_SECRET")

    # --- AI clients ---
    embedding_api_url: str = Field(alias="EMBEDDING_API_URL")
    llm_api_url: str = Field(alias="LLM_API_URL")
    rerank_api_url: str = Field(default="", alias="RERANK_API_URL")

    # --- Datastores ---
    mariadb_dsn: str = Field(alias="MARIADB_DSN")
    es_hosts: str = Field(alias="ES_HOSTS")
    minio_endpoint: str = Field(alias="MINIO_ENDPOINT")
    minio_access_key: str = Field(alias="MINIO_ACCESS_KEY")
    minio_secret_key: str = Field(alias="MINIO_SECRET_KEY")

    # --- Observability ---
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_format: str = Field(default="json", alias="LOG_FORMAT")

    # --- Server ---
    ragent_host: str = Field(default="127.0.0.1", alias="RAGENT_HOST")
    ragent_port: int = Field(default=8000, alias="RAGENT_PORT")


def load_env() -> Settings:
    """Populate os.environ from .env then validate required settings.

    Must be called before importing any ragent.* module so that module-level
    os.environ.get() reads (e.g. in schemas/chat.py) see .env values.
    """
    from dotenv import load_dotenv

    load_dotenv(override=False)
    return Settings()
