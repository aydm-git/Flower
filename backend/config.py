import os
from datetime import timedelta

from dotenv import load_dotenv

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ENV_FILE = os.path.join(BASE_DIR, ".env")
load_dotenv(ENV_FILE)

MAX_QTY = 99
MAX_STOCK = 10_000
MAX_PRICE = 1_000_000

if not os.environ.get("SECRET_KEY"):
    raise RuntimeError("SECRET_KEY не задан. Добавь его в файл .env в корне проекта.")


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, "").strip()))
    except ValueError:
        return default


class Config:
    SECRET_KEY = os.environ["SECRET_KEY"]
    SHOP_TIMEZONE = os.environ.get("SHOP_TIMEZONE", "Asia/Novosibirsk")

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    # Включайте только при работе через HTTPS, иначе браузер не отправит cookie.
    SESSION_COOKIE_SECURE = env_flag("SESSION_COOKIE_SECURE")
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)

    # Количество доверенных обратных прокси (nginx и т. п.) перед приложением.
    # 0 — приложение доступно напрямую, заголовки X-Forwarded-* игнорируются.
    TRUSTED_PROXIES = env_int("TRUSTED_PROXIES", 0)

    SQLALCHEMY_DATABASE_URI = "sqlite:///flower.db"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Ждём освобождения блокировки SQLite, а не падаем сразу с «database is locked».
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"timeout": 15}}

    MAX_CONTENT_LENGTH = 5 * 1024 * 1024
    UPLOAD_SUBDIR = "uploads"
    UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")

    ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "")
    ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", "")

    # Почтовый ящик, с которого уходят письма о заказах. Адреса получателей
    # задаются в админке (вкладка «Настройки»).
    ENV_FILE = ENV_FILE
    SMTP_HOST = os.environ.get("SMTP_HOST", "")
    SMTP_PORT = env_int("SMTP_PORT", 465) or 465
    SMTP_SECURITY = os.environ.get("SMTP_SECURITY", "")
    SMTP_USER = os.environ.get("SMTP_USER", "")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    SMTP_FROM = os.environ.get("SMTP_FROM", "")

    ORDERS_PER_PAGE = 50
