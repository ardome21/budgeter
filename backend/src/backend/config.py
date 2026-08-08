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

    # Plaid. Absent means the linked-accounts feature is simply off — unlike the
    # database, the app is entirely usable without it, so these do have
    # defaults and the router reports "not configured" rather than failing at
    # import and taking every other screen down with it.
    plaid_client_id: str = ""
    plaid_secret: SecretStr = SecretStr("")
    plaid_env: str = "sandbox"

    # Encrypts Plaid access tokens at rest. A Plaid access token is a
    # long-lived read key to a real bank account, which is a different class of
    # secret from anything else here, and the database is backed up and copied
    # around like ordinary data. Generate with:
    #   python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    plaid_token_key: SecretStr = SecretStr("")

    @property
    def plaid_configured(self) -> bool:
        return bool(
            self.plaid_client_id
            and self.plaid_secret.get_secret_value()
            and self.plaid_token_key.get_secret_value()
        )

    @property
    def database_url(self) -> str:
        """Assembled on access so the password is never stored pre-formatted."""
        password = quote(self.postgres_password.get_secret_value(), safe="")
        return (
            f"postgresql+psycopg://{self.postgres_user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


settings = Settings()
