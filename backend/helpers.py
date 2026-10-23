import html
import json
import os
import re
import secrets
import threading
import time
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import current_app
from sqlalchemy import update

from config import MAX_PRICE, MAX_QTY, MAX_STOCK
from models import STATUSES, Order, Product, db


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
OPENING_HOUR = 10
CLOSING_HOUR = 22


def shop_now():
    return datetime.now(ZoneInfo(current_app.config["SHOP_TIMEZONE"]))


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
    today = shop_now().date()
    return today.isoformat(), (today + timedelta(days=MAX_DELIVERY_DAYS)).isoformat()


def validate_delivery_slot(delivery_date: str, delivery_time: str) -> str:
    try:
        chosen = datetime.strptime(delivery_date, "%Y-%m-%d").date()
    except ValueError:
        return "Выберите дату доставки"

    now = shop_now()
    today = now.date()
    if chosen < today:
        return "Дата не может быть в прошлом"
    if chosen > today + timedelta(days=MAX_DELIVERY_DAYS):
        return f"Заказать можно не дальше чем на {MAX_DELIVERY_DAYS} дней вперёд"
    if delivery_time not in DELIVERY_SLOTS:
        return "Выберите время доставки"
    if delivery_time == ASAP_SLOT:
        if chosen != today:
            return "«Как можно скорее» доступно только на сегодня"
        closes = now.replace(hour=CLOSING_HOUR, minute=0, second=0, microsecond=0)
        if now.hour < OPENING_HOUR or now + timedelta(hours=2) > closes:
            return "Доставка за 2 часа доступна с 10:00 до 20:00 по времени магазина"
    elif chosen == today:
        end_hour = int(delivery_time.split("–")[1].split(":")[0])
        end = now.replace(hour=end_hour, minute=0, second=0, microsecond=0)
        if end <= now + timedelta(hours=1):
            return "Этот интервал уже недоступен. Выберите более позднее время"
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

    composition = form.get("composition", "").strip()
    if len(composition) > 500:
        errors.append("Состав слишком длинный (максимум 500 символов)")

    description = form.get("description", "").strip()
    if len(description) > 2000:
        errors.append("Описание слишком длинное (максимум 2000 символов)")

    data = {
        "name": name,
        "price": price,
        "stock": stock,
        "is_active": form.get("is_active") == "1",
        "composition": composition,
        "description": description,
    }
    return data, errors


def bounded_integer(value, maximum):
    # bool is an int subclass; floats (including inf/NaN) are never quantities.
    if type(value) is str:
        if not re.fullmatch(r"[0-9]{1,19}", value):
            return None
        value = int(value)
    if type(value) is not int or not 1 <= value <= maximum:
        return None
    return value


def parse_cart(raw: str) -> dict:
    if not raw or len(raw) > 5000:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return {}
    if not isinstance(data, list) or len(data) > 50:
        return {}

    merged = {}
    for item in data:
        if not isinstance(item, dict):
            return {}
        pid = bounded_integer(item.get("id"), 2**63 - 1)
        qty = bounded_integer(item.get("quantity"), MAX_QTY)
        if pid is None or qty is None or merged.get(pid, 0) + qty > MAX_QTY:
            return {}
        merged[pid] = merged.get(pid, 0) + qty
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
    try:
        if not cart:
            raise OrderError("Корзина пуста — добавьте букет из каталога")
        lines, total, error = build_order(cart)
        if error:
            raise OrderError(error)
        for product, qty in sorted(lines, key=lambda line: line[0].id):
            result = db.session.execute(
                update(Product).where(
                    Product.id == product.id,
                    Product.is_active.is_(True),
                    Product.stock >= qty,
                    Product.price == product.price,
                ).values(stock=Product.stock - qty),
                execution_options={"synchronize_session": False},
            )
            if result.rowcount != 1:
                raise OrderError("Остаток или цена товара изменились. Обновите страницу.")
        flower = ", ".join(f"{p.name} ({q} шт)" for p, q in lines)
        items = [
            {"product_id": p.id, "name": p.name, "quantity": q, "price": p.price}
            for p, q in lines
        ]
        now = shop_now().strftime("%Y-%m-%d %H:%M:%S")
        order = Order(date=now, flower=flower, total=total, items=items, **data)
        db.session.add(order)
        db.session.commit()
    except OrderError as exc:
        db.session.rollback()
        return 0, str(exc)
    except Exception:
        current_app.logger.exception("Не удалось сохранить заказ")
        db.session.rollback()
        return 0, "Произошла ошибка. Попробуйте позже."

    notify_new_order(order)
    return total, ""


class OrderError(ValueError):
    pass


def order_items(order):
    if order.items is not None:
        items = order.items
    else:
        # Only parse the exact historical format; never guess missing product IDs.
        for product in Product.query.filter(Product.name.contains(" шт), ")).all():
            if f"{product.name} (" in order.flower:
                raise OrderError("Состав старого заказа неоднозначен. Требуется ручная сверка.")
        matches = list(re.finditer(r"(.+?) \(([1-9][0-9]*) шт\)(?:, |$)", order.flower))
        if not matches or "".join(m.group(0) for m in matches) != order.flower:
            raise OrderError("Состав старого заказа не распознан. Требуется ручная сверка.")
        items = []
        for match in matches:
            name, quantity = match.groups()
            products = Product.query.filter_by(name=name).all()
            if len(products) != 1:
                raise OrderError(
                    f"Не удалось однозначно найти товар «{name}». Требуется ручная сверка."
                )
            items.append({
                "product_id": products[0].id,
                "name": name,
                "quantity": bounded_integer(quantity, MAX_QTY),
                "price": None,
            })

    if not isinstance(items, list) or not 1 <= len(items) <= 50:
        raise OrderError("Некорректный состав заказа. Требуется ручная сверка.")
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise OrderError("Некорректная позиция заказа. Требуется ручная сверка.")
        pid = bounded_integer(item.get("product_id"), 2**63 - 1)
        qty = bounded_integer(item.get("quantity"), MAX_QTY)
        if pid is None or qty is None or pid in seen:
            raise OrderError("Некорректная позиция заказа. Требуется ручная сверка.")
        seen.add(pid)
    return items


def change_order_status(order, new_status):
    try:
        if new_status not in STATUSES:
            raise OrderError("Неизвестный статус")
        release = new_status == "cancelled"
        moves_stock = release != order.stock_released
        items = order_items(order) if moves_stock else order.items
        # Claim this transition first: concurrent requests cannot return stock twice.
        result = db.session.execute(
            update(Order).where(
                Order.id == order.id,
                Order.status == order.status,
                Order.stock_released == order.stock_released,
            ).values(status=new_status, stock_released=release, items=items),
            execution_options={"synchronize_session": False},
        )
        if result.rowcount != 1:
            raise OrderError("Заказ уже изменён. Обновите страницу и повторите действие.")
        if moves_stock:
            for item in sorted(items, key=lambda item: int(item["product_id"])):
                pid, qty = int(item["product_id"]), int(item["quantity"])
                statement = update(Product).where(Product.id == pid)
                if release:
                    statement = statement.where(Product.stock <= MAX_STOCK - qty).values(
                        stock=Product.stock + qty
                    )
                else:
                    statement = statement.where(
                        Product.is_active.is_(True), Product.stock >= qty
                    ).values(stock=Product.stock - qty)
                result = db.session.execute(
                    statement, execution_options={"synchronize_session": False}
                )
                if result.rowcount != 1:
                    raise OrderError(
                        "Не удалось изменить остатки: товар удалён, недоступен или "
                        "количество выходит за допустимые границы. Требуется сверка."
                    )
        db.session.commit()
    except OrderError as exc:
        db.session.rollback()
        return str(exc)
    except Exception:
        current_app.logger.exception("Не удалось изменить статус заказа")
        db.session.rollback()
        return "Не удалось изменить статус. Попробуйте позже."
    return ""


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
    proxy_url = os.environ.get("TELEGRAM_PROXY", "").strip()
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

    for chat_id in chat_ids:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
        try:
            response = requests.post(
                url, data=payload, proxies=proxies, timeout=15
            )
            response.raise_for_status()
        except Exception as e:
            logger.error(
                "Telegram: не удалось отправить уведомление (%s %s)",
                type(e).__name__,
                getattr(e, "response", None) and e.response.status_code,
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
