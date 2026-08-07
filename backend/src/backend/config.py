from pathlib import Path
from urllib.parse import quote

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# One .env at the repo root, shared with docker-compose.yml, so the password
# exists in a single file. Resolved from __file__ rather than the working
# directory — uvicorn, alembic and pytest are all launched from different
# places and a relative path would silently load nothing.
ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    # No defaults: an absent .env must fail loudly at import. A fallback
    # credential is a credential that reaches production by accident.
    postgres_user: str
    postgres_password: SecretStr
    postgres_db: str
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    cors_origins: list[str] = ["http://localhost:4200"]

    @property
    def database_url(self) -> str:
        """Assembled on access so the password is never stored pre-formatted."""
        password = quote(self.postgres_password.get_secret_value(), safe="")
        return (
            f"postgresql+psycopg://{self.postgres_user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


settings = Settings()
