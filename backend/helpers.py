import html
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

from flask import current_app

from config import MAX_PRICE, MAX_QTY, MAX_STOCK
from models import Order, Product, db


PHONE_RE = re.compile(r"^\+?[0-9\s\-\(\)]{10,20}$")


def is_valid_phone(phone: str) -> bool:
    digits = re.sub(r"\D", "", phone)
    return bool(PHONE_RE.match(phone)) and 10 <= len(digits) <= 15


ASAP_SLOT = "Как можно скорее (в течение 2 часов)"
DELIVERY_SLOTS = [
    ASAP_SLOT,
    "10:00–12:00",
    "12:00–14:00",
    "14:00–16:00",
    "16:00–18:00",
    "18:00–20:00",
    "20:00–22:00",
]
MAX_DELIVERY_DAYS = 60


def slot_options():
    options = []
    for label in DELIVERY_SLOTS:
        if label == ASAP_SLOT:
            options.append({"label": label, "asap": True, "end": ""})
        else:
            end_hour = int(label.split("–")[1].split(":")[0])
            options.append({"label": label, "asap": False, "end": end_hour})
    return options


def delivery_date_limits():
    today = date.today()
    return today.isoformat(), (today + timedelta(days=MAX_DELIVERY_DAYS)).isoformat()


def validate_delivery_slot(delivery_date: str, delivery_time: str) -> str:
    try:
        chosen = datetime.strptime(delivery_date, "%Y-%m-%d").date()
    except ValueError:
        return "Выберите дату доставки"

    today = date.today()
    if chosen < today:
        return "Дата не может быть в прошлом"
    if chosen > today + timedelta(days=MAX_DELIVERY_DAYS):
        return f"Заказать можно не дальше чем на {MAX_DELIVERY_DAYS} дней вперёд"
    if delivery_time not in DELIVERY_SLOTS:
        return "Выберите время доставки"
    if delivery_time == ASAP_SLOT and chosen != today:
        return "«Как можно скорее» доступно только на сегодня"
    return ""


def validate_order_form(form, cart: dict):
    is_pickup = form.get("is_pickup") == "1"
    data = {
        "name": form.get("name", "").strip(),
        "phone": form.get("phone", "").strip(),
        "is_pickup": is_pickup,
        "address": "" if is_pickup else form.get("address", "").strip(),
        "delivery_date": form.get("delivery_date", "").strip(),
        "delivery_time": form.get("delivery_time", "").strip(),
        "card_text": form.get("card_text", "").strip(),
        "comment": form.get("comment", "").strip(),
    }

    if not data["name"] or not data["phone"]:
        return data, "Заполните имя и телефон"
    if len(data["name"]) > 100:
        return data, "Имя слишком длинное (максимум 100 символов)"
    if not is_valid_phone(data["phone"]):
        return data, "Введите корректный номер телефона (10–15 цифр, можно +, пробелы, дефисы и скобки)"

    if not is_pickup:
        if len(data["address"]) < 5:
            return data, "Укажите адрес доставки: улица, дом, квартира"
        if len(data["address"]) > 255:
            return data, "Адрес слишком длинный (максимум 255 символов)"

    error = validate_delivery_slot(data["delivery_date"], data["delivery_time"])
    if error:
        return data, error

    if len(data["card_text"]) > 300:
        return data, "Текст открытки слишком длинный (максимум 300 символов)"
    if len(data["comment"]) > 500:
        return data, "Комментарий слишком длинный (максимум 500 символов)"
    if not cart:
        return data, "Корзина пуста — добавьте букет из каталога"
    return data, ""


def validate_product_form(form):
    errors = []

    name = form.get("name", "").strip()
    if not name:
        errors.append("Введите название")
    elif len(name) > 100:
        errors.append("Название слишком длинное (максимум 100 символов)")

    price = 0
    try:
        price = int(form.get("price", "").strip())
        if not 1 <= price <= MAX_PRICE:
            raise ValueError
    except ValueError:
        errors.append(f"Цена — целое число от 1 до {MAX_PRICE}")

    stock = 0
    try:
        stock = int(form.get("stock", "").strip())
        if not 0 <= stock <= MAX_STOCK:
            raise ValueError
    except ValueError:
        errors.append(f"Количество — целое число от 0 до {MAX_STOCK}")

    data = {
        "name": name,
        "price": price,
        "stock": stock,
        "is_active": form.get("is_active") == "1",
    }
    return data, errors


def parse_cart(raw: str) -> dict:
    if not raw or len(raw) > 5000:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, list):
        return {}

    merged = {}
    for item in data[:50]:
        if not isinstance(item, dict):
            continue
        try:
            pid = int(item.get("id"))
            qty = int(item.get("quantity"))
        except (TypeError, ValueError):
            continue
        if qty < 1:
            continue
        merged[pid] = min(merged.get(pid, 0) + qty, MAX_QTY)
    return merged


def build_order(cart: dict):
    found = {
        p.id: p
        for p in Product.query.filter(
            Product.id.in_(list(cart.keys())), Product.is_active.is_(True)
        ).all()
    }
    lines = []
    total = 0
    for pid, qty in cart.items():
        product = found.get(pid)
        if product is None:
            return [], 0, "Один из товаров больше недоступен. Обновите страницу."
        if product.stock < 1:
            return [], 0, f"«{product.name}» закончился. Уберите его из корзины."
        if qty > product.stock:
            return [], 0, f"«{product.name}»: в наличии только {product.stock} шт."
        lines.append((product, qty))
        total += product.price * qty
    return lines, total, ""


def place_order(data: dict, cart: dict):
    lines, total, error = build_order(cart)
    if error:
        return 0, error

    try:
        for product, qty in lines:
            product.stock -= qty
        flower = ", ".join(f"{p.name} ({q} шт)" for p, q in lines)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        order = Order(date=now, flower=flower, total=total, **data)
        db.session.add(order)
        db.session.commit()
    except Exception:
        current_app.logger.exception("Не удалось сохранить заказ")
        db.session.rollback()
        return 0, "Произошла ошибка. Попробуйте позже."

    notify_new_order(order)
    return total, ""


def format_order_message(order) -> str:
    esc = html.escape
    lines = [
        f"<b>Новый заказ №{order.id}</b>",
        "",
        f"Имя: {esc(order.name)}",
        f"Телефон: {esc(order.phone)}",
        f"Букет: {esc(order.flower)}",
        f"Сумма: {order.total} ₽",
        "",
    ]
    if order.is_pickup:
        lines.append("Самовывоз")
    else:
        lines.append(f"Адрес: {esc(order.address)}")
    lines.append(f"Когда: {order.delivery_date_ru}, {esc(order.delivery_time)}")
    if order.card_text:
        lines.append(f"Открытка: {esc(order.card_text)}")
    if order.comment:
        lines.append(f"Комментарий: {esc(order.comment)}")
    return "\n".join(lines)


def send_telegram(token: str, chat_ids: list, text: str, logger) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chat_id in chat_ids:
        payload = urllib.parse.urlencode(
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            }
        ).encode()
        try:
            request = urllib.request.Request(url, data=payload)
            with urllib.request.urlopen(request, timeout=10) as response:
                response.read()
        except Exception as e:
            logger.error(
                "Telegram: не удалось отправить уведомление (%s %s)",
                type(e).__name__,
                getattr(e, "code", ""),
            )


def notify_new_order(order) -> None:
    token = current_app.config.get("TELEGRAM_BOT_TOKEN", "")
    raw_ids = current_app.config.get("TELEGRAM_CHAT_ID", "")
    chat_ids = [c.strip() for c in raw_ids.split(",") if c.strip()]
    if not token or not chat_ids:
        return
    try:
        text = format_order_message(order)
    except Exception:
        current_app.logger.exception("Не удалось собрать сообщение для Telegram")
        return
    threading.Thread(
        target=send_telegram,
        args=(token, chat_ids, text, current_app.logger),
        daemon=True,
    ).start()


def detect_image_ext(stream):
    head = stream.read(12)
    stream.seek(0)
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def save_image(file_storage):
    ext = detect_image_ext(file_storage.stream)
    if not ext:
        return None, "Фото должно быть в формате JPG, PNG или WEBP"

    subdir = current_app.config["UPLOAD_SUBDIR"]
    filename = f"{secrets.token_hex(8)}.{ext}"
    file_storage.save(os.path.join(current_app.config["UPLOAD_DIR"], filename))
    return f"{subdir}/{filename}", None


def delete_image_file(rel_path: str) -> None:
    subdir = current_app.config["UPLOAD_SUBDIR"]
    if not rel_path or not rel_path.startswith(f"{subdir}/"):
        return

    upload_dir = os.path.abspath(current_app.config["UPLOAD_DIR"])
    full = os.path.abspath(os.path.join(current_app.static_folder, rel_path))
    if not full.startswith(upload_dir + os.sep):
        return
    try:
        os.remove(full)
    except OSError:
        pass


MAX_ATTEMPTS = 5
WINDOW_SECONDS = 600
_failed_logins = {}


def too_many_attempts(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _failed_logins.get(ip, []) if now - t < WINDOW_SECONDS]
    _failed_logins[ip] = recent
    return len(recent) >= MAX_ATTEMPTS


def register_failed_login(ip: str) -> None:
    _failed_logins.setdefault(ip, []).append(time.time())


def reset_attempts(ip: str) -> None:
    _failed_logins.pop(ip, None)
