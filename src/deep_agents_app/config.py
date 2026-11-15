"""All configuration comes from environment variables (or a local .env file)."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_SCHEMA = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Postgres (external service)
    database_url: str
    database_schema: str = "deep_agents"
    auto_migrate: bool = True

    # LiteLLM gateway (OpenAI-compatible)
    llm_gateway_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    default_model: str = ""
    llm_params_json: str = "{}"
    llm_timeout_seconds: float = 60
    llm_max_retries: int = 1

    # App behaviour
    app_language: str = "en"
    data_dir: Path = Path("./data")
    max_concurrent_runs: int = 3
    run_timeout_seconds: int = 3600
    allow_test_mode: bool = True
    web_blocked_hosts: str = ""
    # "process": every run in its own worker process that can be killed on cancel/timeout.
    # "thread": in the API process (tests/debugging only; a hung run cannot be killed).
    run_mode: Literal["process", "thread"] = "process"
    # Per-worker address-space limit in MB (POSIX only, 0 = off). Leave off unless you have measured it;
    # prefer container limits (mem_limit/pids_limit) for the whole service.
    worker_memory_mb: int = 0

    # Optional OAuth 2.0 / OIDC login. OFF by default: with AUTH_ENABLED=false the service
    # has no login and no access protection at all (see auth/__init__.py).
    auth_enabled: bool = False
    oauth_authorize_url: str = ""
    oauth_token_url: str = ""
    oauth_userinfo_url: str = ""
    oauth_client_id: str = ""
    oauth_client_secret: SecretStr = SecretStr("")
    oauth_scopes: str = "openid email profile"
    oauth_redirect_url: str = ""
    oauth_allowed_emails: str = ""
    session_secret: SecretStr = SecretStr("")
    session_ttl_seconds: int = 8 * 3600
    cookie_secure: bool = True

    @model_validator(mode="after")
    def _auth_complete(self) -> Settings:
        if not self.auth_enabled:
            return self
        missing = [
            name
            for name, value in {
                "OAUTH_AUTHORIZE_URL": self.oauth_authorize_url,
                "OAUTH_TOKEN_URL": self.oauth_token_url,
                "OAUTH_USERINFO_URL": self.oauth_userinfo_url,
                "OAUTH_CLIENT_ID": self.oauth_client_id,
                "OAUTH_CLIENT_SECRET": self.oauth_client_secret.get_secret_value(),
                "OAUTH_REDIRECT_URL": self.oauth_redirect_url,
                "OAUTH_ALLOWED_EMAILS": self.oauth_allowed_emails,
            }.items()
            if not value
        ]
        if missing:
            raise ValueError("AUTH_ENABLED=true requires: " + ", ".join(missing))
        if len(self.session_secret.get_secret_value()) < 32:
            raise ValueError("AUTH_ENABLED=true requires SESSION_SECRET of at least 32 characters")
        return self

    @property
    def allowed_emails(self) -> set[str]:
        return {e.strip().lower() for e in self.oauth_allowed_emails.split(",") if e.strip()}

    @field_validator("database_url")
    @classmethod
    def _database_url(cls, value: str) -> str:
        if urlsplit(value).scheme not in ("postgresql", "postgres", "postgresql+psycopg"):
            raise ValueError("DATABASE_URL must be a postgresql:// URL")
        return value

    @field_validator("database_schema")
    @classmethod
    def _schema(cls, value: str) -> str:
        if not _SCHEMA.match(value):
            raise ValueError("DATABASE_SCHEMA must be a simple lowercase identifier")
        return value

    @field_validator("llm_params_json")
    @classmethod
    def _params(cls, value: str) -> str:
        parsed = json.loads(value or "{}")
        if not isinstance(parsed, dict):
            raise ValueError("LLM_PARAMS_JSON must be a JSON object")
        return value or "{}"

    @field_validator("max_concurrent_runs", "run_timeout_seconds")
    @classmethod
    def _positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("must be at least 1")
        return value

    @field_validator("worker_memory_mb")
    @classmethod
    def _memory(cls, value: int) -> int:
        if value < 0:
            raise ValueError("must not be negative")
        return value

    def to_env(self) -> dict[str, str]:
        """This configuration as environment variables, so a worker process sees exactly the same settings."""
        env: dict[str, str] = {}
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                value = value.get_secret_value()
            env[name.upper()] = str(value).lower() if isinstance(value, bool) else str(value)
        return env

    @property
    def llm_params(self) -> dict[str, Any]:
        return json.loads(self.llm_params_json)

    @property
    def sqlalchemy_url(self) -> str:
        return _with_scheme(self.database_url, "postgresql+psycopg")

    @property
    def psycopg_url(self) -> str:
        return _with_scheme(self.database_url, "postgresql")

    @property
    def search_path_option(self) -> str:
        return f"-csearch_path={self.database_schema}"

    @property
    def blocked_host_suffixes(self) -> list[str]:
        """Hosts the web tools must never reach: configured ones plus our own backends."""
        hosts = [h.strip().lower().strip(".") for h in self.web_blocked_hosts.split(",") if h.strip()]
        for url in (self.llm_gateway_url, self.database_url):
            host = urlsplit(url).hostname
            if host:
                hosts.append(host.lower())
        return hosts


def _with_scheme(url: str, scheme: str) -> str:
    parts = urlsplit(url)
    return parts._replace(scheme=scheme).geturl()


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
