from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"
    supabase_url: str | None = None
    supabase_service_role_key: str | None = None
    supabase_jwt_secret: str | None = None  # legacy HS256 fallback — this project is on ES256/JWKS, unused
    gotiate_gateway_secret: str | None = None
    gotiate_backend_database_url: str | None = None
    gotiate_db_pool_min_size: int = 1
    gotiate_db_pool_max_size: int = 5


settings = Settings()
