from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, AliasChoices

class Settings(BaseSettings):
    DATABASE_URL: str = "cassie_support.db"
    GOOGLE_CLIENT_SECRETS_FILE: str = "client_secret.json"
    GOOGLE_TOKEN_JSON: str = "gmail_token.json"
    SUPPORT_FROM_EMAIL: Optional[str] = None
    GMAIL_POLL_QUERY: str = "is:unread -category:promotions"
    GMAIL_POLL_INTERVAL_SECONDS: int = 30
    TIMEZONE: str = "Asia/Kolkata"
    BRAND_NAME: str = "Cassie"
    BRAND_HOURS: str = "Mon–Fri 9:00–17:00"
    GROQ_API_KEY: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("GROQ_API_KEY", "groq_api_key")
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
    )

settings = Settings()
