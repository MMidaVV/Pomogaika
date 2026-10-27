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
    # Username собственницы для ссылки гостю в личку, без @. Не имя бота.
    telegram_owner_username: str = ""
    # Номер личного WhatsApp, на который ведёт ссылка гостя. Без плюса: 79XXXXXXXXX.
    whatsapp_owner_phone: str = ""
    # Локальный процесс whatsapp-web.js. Слушает только эту машину.
    whatsapp_base_url: str = "http://127.0.0.1:8792"
    # Ссылка на личный профиль Max: https://max.ru/u/… Гость открывает чат с вами.
    # Текст «Здравствуйте! Бронь …» в адрес не вшивается: у Max нет такого параметра.
    max_owner_link: str = ""
    # Локальный процесс Node-Max. Слушает только эту машину.
    max_base_url: str = "http://127.0.0.1:8793"
    # Номера поддержки площадок и свой номер: в гостевой телефон их не пишем.
    guest_phone_blocklist: str = ""
    telegram_cleaner_user_id: int | None = None
    cleaner_escalate_minutes: int = 10
    tokenrouter_api_key: str = ""
    tokenrouter_base_url: str = "https://api.tokenrouter.com/v1"
    tokenrouter_model: str = "z-ai/glm-5.3-flash"
    # Используется в services/llm.py. Без этого поля каждый вызов модели падал
    # с AttributeError ещё до запроса, и ответ молча скатывался в FAQ.
    tokenrouter_timeout: float = 30.0
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
    # Ответ гостю письмом (канал Яндекса). Пусто — берём доступы от IMAP:
    # у mail.ru один пароль приложения на чтение и отправку.
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    # Предохранитель: пока false, ответы гостю по почте только копятся
    # черновиками и ждут человека. Включать осознанно — это живая переписка.
    guest_mail_autosend: bool = False
    # Рубильник: false — письма с сообщениями гостя не разбираем вовсе.
    guest_mail_enabled: bool = True
    # Старое непрочитанное письмо — это история, а не новый вопрос. Разбираем
    # только свежие: иначе после простоя сервиса собственнику прилетают
    # карточки по июньским сообщениям. 0 — без ограничения по возрасту.
    guest_mail_max_age_hours: int = 48
    # Временный режим «Помогайка отвечает сама»: обычные вопросы уходят гостю
    # без кнопки собственника. Деньги, возвраты, отмены, жалобы и коды от двери
    # человеку остаются в любом случае. Выключается строкой в .env, без выкатки.
    copilot_autosend: bool = False
    tourist_tax_percent: float = 1.0
    tourist_tax_min_rub_per_night: int = 100

    @field_validator("telegram_owner_user_id", "telegram_cleaner_user_id", mode="before")
    @classmethod
    def empty_id(cls, value):
        if value in ("", None):
            return None
        return value


settings = Settings()
