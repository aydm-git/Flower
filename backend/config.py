import os
from datetime import timedelta

from dotenv import load_dotenv

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
load_dotenv(os.path.join(BASE_DIR, ".env"))

MAX_QTY = 99
MAX_STOCK = 10_000
MAX_PRICE = 1_000_000

if not os.environ.get("SECRET_KEY"):
    raise RuntimeError("SECRET_KEY не задан. Добавь его в файл .env в корне проекта.")


class Config:
    SECRET_KEY = os.environ["SECRET_KEY"]

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)

    SQLALCHEMY_DATABASE_URI = "sqlite:///flower.db"
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    MAX_CONTENT_LENGTH = 5 * 1024 * 1024
    UPLOAD_SUBDIR = "uploads"
    UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")

    ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "")
    ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", "")

    TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
