from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional

class Settings(BaseSettings):
    # SQLite file path
    DATABASE_URL: str = "cassie_support.db"

    # Gmail: OAuth client secrets json & token storage
    GOOGLE_CLIENT_SECRETS_FILE: str = "client_secret.json"
    GOOGLE_TOKEN_JSON: str = "gmail_token.json"
    SUPPORT_FROM_EMAIL: Optional[str] = None  

    # Polling
    GMAIL_POLL_QUERY: str = 'is:unread -category:promotions'
    GMAIL_POLL_INTERVAL_SECONDS: int = 30

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

settings = Settings()
