from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Matches docker-compose.yml. Override via .env — never hardcode real creds.
    database_url: str = "postgresql+psycopg://budgeter:budgeter_dev@localhost:5432/budgeter"
    cors_origins: list[str] = ["http://localhost:4200"]


settings = Settings()
