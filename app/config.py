from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_token: str = ""
    telegram_bot_username: str = ""
    public_url: str = ""
    admin_chat_id: int = 0

    openai_api_key: str = ""
    chat_model: str = "gpt-4o-mini"
    embed_model: str = "text-embedding-3-small"
    # USD per one million tokens, used to estimate spend
    price_in_per_m: float = 0.15
    price_out_per_m: float = 0.60

    user_daily_limit: int = 30
    global_daily_budget_usd: float = 1.0
    max_output_tokens: int = 350
    history_turns: int = 8
    max_input_chars: int = 600

    admin_user: str = "admin"
    admin_password: str = "change-me"
    # Read-only login for people looking at the demo; set empty to disable
    demo_user: str = "demo"
    demo_password: str = "demo1234"

    db_path: str = "data/bot.db"
    knowledge_dir: str = "knowledge"
    shop_name: str = "Nova Gear"

    @property
    def session_secret(self) -> str:
        import hashlib

        return hashlib.sha256(("session" + self.admin_password + self.telegram_token).encode()).hexdigest()

    @property
    def webhook_secret(self) -> str:
        import hashlib

        return hashlib.sha256(("nova" + self.telegram_token).encode()).hexdigest()[:32]


@lru_cache
def get_settings() -> Settings:
    return Settings()
