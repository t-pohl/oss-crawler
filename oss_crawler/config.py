"""Configuration loaded from .env and environment variables."""
from __future__ import annotations

import sys
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def app_dir() -> Path:
    """Directory that holds user-visible files (`.env`, `.auth.json`, downloads).

    When running from a PyInstaller-built `.exe`, this is the folder
    containing the executable, so the bundled tool is fully portable
    (drop it on a USB stick, Desktop, anywhere). Otherwise it's the
    current working directory, preserving the existing developer UX.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path.cwd()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(app_dir() / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    oss_username: str = ""
    oss_password: str = ""
    oss_base_url: str = "https://meine.online-schule.saarland"
    oss_idp_host: str = "idp.online-schule.saarland"
    headless: bool = True

    auth_state_path: Path = app_dir() / ".auth.json"

    @field_validator("oss_base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    def has_credentials(self) -> bool:
        return bool(self.oss_username and self.oss_password)


def load_settings() -> Settings:
    return Settings()
