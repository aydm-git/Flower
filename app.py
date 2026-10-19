import hmac
import json
import os
import re
import secrets
import time
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, session, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFError, CSRFProtect
from sqlalchemy import inspect, text
from werkzeug.security import check_password_hash

load_dotenv()

app = Flask(__name__)

# ---------- Настройки и секреты ----------
secret_key = os.environ.get("SECRET_KEY")
if not secret_key:
    secret_key = secrets.token_hex(32)
    print("[WARNING] SECRET_KEY не задан в .env — использую временный ключ.")
app.secret_key = secret_key

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///flower.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "")
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", "")

db = SQLAlchemy(app)
csrf = CSRFProtect(app)


# ---------- Модели ----------
class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(50), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    flower = db.Column(db.String(255), nullable=False)
    total = db.Column(db.Integer, nullable=False, default=0)


# ---------- Каталог ----------
products = [
    {"id": 1, "name": "Букет роз", "price": 2500, "img": "images/roses.jpeg"},
    {"id": 2, "name": "Тюльпаны", "price": 1800, "img": "images/tulps.jpg"},
    {"id": 3, "name": "Микс-букет", "price": 3200, "img": "images/mix.jpeg"},
    {"id": 4, "name": "Цветы в коробке", "price": 4000, "img": "images/box.jpg"},
]
PRODUCTS_BY_ID = {p["id"]: p for p in products}
MAX_QTY = 99


def ensure_schema():
    """Создаёт таблицы и добавляет колонку total в старую базу, если её нет."""
    db.create_all()
    columns = {c["name"] for c in inspect(db.engine).get_columns(Order.__tablename__)}
    if "total" not in columns:
        with db.engine.begin() as conn:
            conn.execute(
                text(f'ALTER TABLE "{Order.__tablename__}" '
                     "ADD COLUMN total INTEGER NOT NULL DEFAULT 0")
            )


with app.app_context():
    ensure_schema()


# ---------- Вспомогательные функции ----------
PHONE_RE = re.compile(r"^\+?[0-9\s\-\(\)]{10,20}$")


def is_valid_phone(phone: str) -> bool:
    digits = re.sub(r"\D", "", phone)
    return bool(PHONE_RE.match(phone)) and 10 <= len(digits) <= 15


def parse_cart(raw: str):
    """Разбирает корзину из формы. Названия и цены берём ТОЛЬКО из каталога на сервере."""
    if not raw or len(raw) > 5000:
        return "", 0
    try:
        data = json.loads(raw)
    except ValueError:
        return "", 0
    if not isinstance(data, list):
        return "", 0

    merged = {}
    for item in data[:50]:
        if not isinstance(item, dict):
            continue
        try:
            pid = int(item.get("id"))
            qty = int(item.get("quantity"))
        except (TypeError, ValueError):
            continue
        if pid not in PRODUCTS_BY_ID or qty < 1:
            continue
        merged[pid] = min(merged.get(pid, 0) + qty, MAX_QTY)

    parts = []
    total = 0
    for pid, qty in merged.items():
        product = PRODUCTS_BY_ID[pid]
        parts.append(f"{product['name']} ({qty} шт)")
        total += product["price"] * qty
    return ", ".join(parts), total


# Простая защита от подбора пароля: 5 неудачных попыток за 10 минут с одного IP
FAILED_LOGINS = {}
MAX_ATTEMPTS = 5
WINDOW_SECONDS = 600


def too_many_attempts(ip: str) -> bool:
    now = time.time()
    recent = [t for t in FAILED_LOGINS.get(ip, []) if now - t < WINDOW_SECONDS]
    FAILED_LOGINS[ip] = recent
    return len(recent) >= MAX_ATTEMPTS


def register_failed_login(ip: str) -> None:
    FAILED_LOGINS.setdefault(ip, []).append(time.time())


# ---------- Маршруты ----------
@app.route("/", methods=["GET", "POST"])
def home():
    message_type = ""
    name = ""
    phone = ""

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        flower, total = parse_cart(request.form.get("cart", ""))

        if not name or not phone:
            message_type = "error_empty"
        elif len(name) > 100:
            message_type = "error_name"
        elif not is_valid_phone(phone):
            message_type = "error_phone"
        elif not flower:
            message_type = "error_cart"
        else:
            try:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                db.session.add(
                    Order(date=now, name=name, phone=phone, flower=flower, total=total)
                )
                db.session.commit()
            except Exception as e:
                print(f"[DB ERROR] {e}")
                db.session.rollback()
                message_type = "error_db"
            else:
                # POST -> redirect -> GET: обновление страницы больше не дублирует заказ
                flash(
                    f"Спасибо, {name}! Заявка принята. Сумма заказа: {total} ₽. "
                    "Мы скоро свяжемся с вами.",
                    "order_success",
                )
                return redirect(url_for("home", _anchor="order"))

    status = 400 if message_type else 200
    return (
        render_template(
            "index.html",
            products=products,
            message_type=message_type,
            name=name,
            phone=phone,
        ),
        status,
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("logged_in"):
        return redirect(url_for("admin"))

    if request.method == "POST":
        ip = request.remote_addr or "unknown"

        if too_many_attempts(ip):
            flash("Слишком много попыток. Попробуйте через 10 минут.", "error")
            return render_template("login.html"), 429

        username = request.form.get("username", "")
        password = request.form.get("password", "")

        if not ADMIN_USERNAME or not ADMIN_PASSWORD_HASH:
            flash("Вход не настроен: задайте ADMIN_USERNAME и ADMIN_PASSWORD_HASH в .env", "error")
        else:
            user_ok = hmac.compare_digest(username.encode(), ADMIN_USERNAME.encode())
            pass_ok = check_password_hash(ADMIN_PASSWORD_HASH, password)
            if user_ok and pass_ok:
                session.clear()
                session["logged_in"] = True
                FAILED_LOGINS.pop(ip, None)
                return redirect(url_for("admin"))
            register_failed_login(ip)
            flash("Неверный логин или пароль", "error")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.route("/admin")
def admin():
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    orders = Order.query.order_by(Order.id.desc()).all()
    orders_list = [
        {
            "id": o.id,
            "date": o.date,
            "name": o.name,
            "phone": o.phone,
            "flower": o.flower,
            "total": o.total,
        }
        for o in orders
    ]
    return render_template("admin.html", orders=orders_list)


@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    flash("Сессия устарела. Обновите страницу и попробуйте ещё раз.", "error")
    return redirect(url_for("home"))


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
