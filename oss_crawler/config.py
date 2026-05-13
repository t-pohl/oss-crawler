"""Configuration loaded from .env and environment variables."""
from __future__ import annotations

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    oss_username: str = ""
    oss_password: str = ""
    oss_base_url: str = "https://psc.online-schule.saarland"
    oss_idp_host: str = "idp.online-schule.saarland"
    headless: bool = True

    auth_state_path: Path = Path(".auth.json")

    @field_validator("oss_base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    def has_credentials(self) -> bool:
        return bool(self.oss_username and self.oss_password)


def load_settings() -> Settings:
    return Settings()
