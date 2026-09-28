from functools import lru_cache
from ipaddress import ip_address, ip_network
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="NMA_", extra="ignore", case_sensitive=False
    )

    base_url: str = "http://localhost:8080"
    host: str = "0.0.0.0"
    port: int = 8080
    secret_key: SecretStr = SecretStr("change-me")
    token_encryption_key: SecretStr | None = None
    tidal_session_file: Path = Path("/state/tidal-session.json")

    authelia_issuer_url: str = ""
    authelia_client_id: str = ""
    authelia_client_secret: SecretStr = SecretStr("")
    authelia_users_group: str = "users"
    authelia_admin_group: str = "admins"
    dev_auth_bypass: bool = False
    dev_auth_username: str = "testuser"
    proxy_auth_enabled: bool = False
    proxy_auth_user_header: str = "Remote-User"
    proxy_auth_groups_header: str = "Remote-Groups"
    proxy_auth_trusted_sources: str = ""

    navidrome_url: str = "http://navidrome:4533"
    navidrome_user_header: str = "Remote-User"
    navidrome_client_name: str = "namuad"
    navidrome_page_size: int = 500

    tidarr_url: str = "http://tidarr:8484"
    tidarr_api_key: SecretStr = SecretStr("")
    tidarr_api_key_file: Path | None = None

    # Optional. When omitted, imports rely entirely on Navidrome's index.
    music_path: Path | None = None
    misc_folder_name: str = "misc"
    worker_interval_seconds: float = Field(default=5.0, ge=1)
    provider_timeout_seconds: float = Field(default=30.0, ge=1)
    navidrome_index_timeout_seconds: int = Field(default=300, ge=10)

    @field_validator("music_path", mode="before")
    @classmethod
    def empty_music_path_disables_filesystem_access(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    def proxy_source_is_trusted(self, host: str | None) -> bool:
        if not host:
            return False
        try:
            address = ip_address(host)
            networks = [
                ip_network(value.strip(), strict=False)
                for value in self.proxy_auth_trusted_sources.split(",")
                if value.strip()
            ]
        except ValueError:
            return False
        return any(address in network for network in networks)


@lru_cache
def get_settings() -> Settings:
    return Settings()
