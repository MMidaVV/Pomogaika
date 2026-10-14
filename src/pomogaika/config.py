from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(ROOT / ".env"), extra="ignore")

    database_url: str = f"sqlite:///{ROOT / 'data' / 'pomogaika.db'}"
    knowledge_path: Path = ROOT / "ops" / "kava" / "knowledge.yaml"
    faq_path: Path = ROOT / "ops" / "kava" / "faq.yaml"
    telegram_bot_token: str = ""
    telegram_owner_user_id: int | None = None
    telegram_cleaner_user_id: int | None = None
    cleaner_escalate_minutes: int = 10
    tokenrouter_api_key: str = ""
    tokenrouter_base_url: str = "https://api.tokenrouter.com/v1"
    tokenrouter_model: str = "z-ai/glm-5.3-flash"
    telegram_webapp_url: str = ""
    telegram_auth_required: bool = True
    # Турналог: ставка и минимум за сутки. Закон менялся — держим в настройках.
    # Как часто сами тянем календари площадок, минут. 0 — только по кнопке.
    ical_sync_minutes: int = 30
    # Почта площадок: письма дают имя гостя, сумму и номер брони — того, чего нет в iCal.
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    imap_poll_minutes: int = 5
    tourist_tax_percent: float = 1.0
    tourist_tax_min_rub_per_night: int = 100

    @field_validator("telegram_owner_user_id", "telegram_cleaner_user_id", mode="before")
    @classmethod
    def empty_id(cls, value):
        if value in ("", None):
            return None
        return value


settings = Settings()
