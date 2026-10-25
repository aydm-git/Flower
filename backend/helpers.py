import html
import json
import os
import re
import secrets
import socket
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests
from flask import current_app
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from config import MAX_PRICE, MAX_QTY, MAX_STOCK
from models import STATUSES, LoginAttempt, Order, Product, db


PHONE_RE = re.compile(r"\+?[0-9 ()\-]{10,20}")


def is_valid_phone(phone: str) -> bool:
    digits = re.sub(r"[^0-9]", "", phone)
    return bool(PHONE_RE.fullmatch(phone)) and 10 <= len(digits) <= 15


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"[^0-9]", "", phone)
    return f"+{digits}" if phone.startswith("+") else digits


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
TODAY_CLOSED_MESSAGE = (
    "На сегодня доставка и самовывоз уже недоступны — дата перенесена на завтра. "
    "Выберите время и отправьте заявку ещё раз."
)
ISO_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def shop_now():
    return datetime.now(ZoneInfo(current_app.config["SHOP_TIMEZONE"]))


def slot_end_hour(label: str) -> int:
    return int(label.split("–")[1].split(":")[0])


def slot_options():
    options = []
    for label in DELIVERY_SLOTS:
        if label == ASAP_SLOT:
            options.append({"label": label, "asap": True, "end": ""})
        else:
            options.append({"label": label, "asap": False, "end": slot_end_hour(label)})
    return options


def parse_iso_date(value: str):
    """Строгий разбор YYYY-MM-DD: формы вроде 2026-1-5 не принимаются."""
    if not isinstance(value, str) or not ISO_DATE_RE.fullmatch(value):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def slot_error(day, label: str, now) -> str:
    """Ошибка для конкретного интервала в конкретный день ('' — интервал доступен)."""
    today = now.date()
    if label == ASAP_SLOT:
        if day != today:
            return "«Как можно скорее» доступно только на сегодня"
        closes = now.replace(hour=CLOSING_HOUR, minute=0, second=0, microsecond=0)
        if now.hour < OPENING_HOUR or now + timedelta(hours=2) > closes:
            return "Доставка за 2 часа доступна с 10:00 до 20:00 по времени магазина"
        return ""
    if day == today:
        end = now.replace(hour=slot_end_hour(label), minute=0, second=0, microsecond=0)
        if end <= now + timedelta(hours=1):
            return "Этот интервал уже недоступен. Выберите более позднее время"
    return ""


def first_delivery_date(now=None):
    """Сегодня, если остался хотя бы один интервал, иначе — завтра."""
    now = now or shop_now()
    today = now.date()
    if any(not slot_error(today, label, now) for label in DELIVERY_SLOTS):
        return today
    return today + timedelta(days=1)


def delivery_date_limits():
    now = shop_now()
    first = first_delivery_date(now)
    last = now.date() + timedelta(days=MAX_DELIVERY_DAYS)
    return first.isoformat(), last.isoformat()


def validate_delivery_slot(delivery_date: str, delivery_time: str) -> str:
    chosen = parse_iso_date(delivery_date)
    if chosen is None:
        return "Выберите дату доставки"

    now = shop_now()
    today = now.date()
    if chosen < today:
        return "Дата не может быть в прошлом"
    if chosen > today + timedelta(days=MAX_DELIVERY_DAYS):
        return f"Заказать можно не дальше чем на {MAX_DELIVERY_DAYS} дней вперёд"
    if delivery_time not in DELIVERY_SLOTS:
        return "Выберите время доставки"
    if chosen == today and first_delivery_date(now) > today:
        return TODAY_CLOSED_MESSAGE
    return slot_error(chosen, delivery_time, now)


def validate_order_form(form, cart: dict, cart_error: str = ""):
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
    data["phone"] = normalize_phone(data["phone"])

    if not is_pickup:
        if len(data["address"]) < 5:
            return data, "Укажите адрес доставки: улица, дом, квартира"
        if len(data["address"]) > 255:
            return data, "Адрес слишком длинный (максимум 255 символов)"

    error = validate_delivery_slot(data["delivery_date"], data["delivery_time"])
    if error:
        return data, error
    data["delivery_date"] = parse_iso_date(data["delivery_date"]).isoformat()

    if len(data["card_text"]) > 300:
        return data, "Текст открытки слишком длинный (максимум 300 символов)"
    if len(data["comment"]) > 500:
        return data, "Комментарий слишком длинный (максимум 500 символов)"
    if cart_error:
        return data, cart_error
    if not cart:
        return data, "Корзина пуста — добавьте букет из каталога"
    return data, ""


def validate_product_form(form, with_stock: bool = True):
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
    if with_stock:
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
        "is_active": form.get("is_active") == "1",
        "composition": composition,
        "description": description,
    }
    # Остаток существующего товара меняется только атомарно (кнопки ±),
    # иначе сохранение формы перезаписало бы продажи, прошедшие за это время.
    if with_stock:
        data["stock"] = stock
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


CART_BROKEN_MESSAGE = "Корзина повреждена. Обновите страницу и соберите заказ заново."


def read_cart(raw: str):
    """Возвращает (количества {id: qty}, ожидаемые цены {id: price}, ошибка)."""
    if not raw:
        return {}, {}, ""
    if len(raw) > 5000:
        return {}, {}, CART_BROKEN_MESSAGE
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return {}, {}, CART_BROKEN_MESSAGE
    if not isinstance(data, list) or len(data) > 50:
        return {}, {}, CART_BROKEN_MESSAGE

    merged, prices = {}, {}
    for item in data:
        if not isinstance(item, dict):
            return {}, {}, CART_BROKEN_MESSAGE
        pid = bounded_integer(item.get("id"), 2**63 - 1)
        qty = bounded_integer(item.get("quantity"), MAX_QTY)
        if pid is None or qty is None or merged.get(pid, 0) + qty > MAX_QTY:
            return {}, {}, CART_BROKEN_MESSAGE
        if item.get("price") is not None:
            price = bounded_integer(item.get("price"), MAX_PRICE)
            if price is None or prices.get(pid, price) != price:
                return {}, {}, CART_BROKEN_MESSAGE
            prices[pid] = price
        merged[pid] = merged.get(pid, 0) + qty
    return merged, prices, ""


def parse_cart(raw: str) -> dict:
    return read_cart(raw)[0]


ORDER_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{16,64}")


def new_order_token() -> str:
    return secrets.token_urlsafe(24)


def clean_order_token(value):
    return value if isinstance(value, str) and ORDER_TOKEN_RE.fullmatch(value) else None


def build_order(cart: dict, expected_prices: dict = None):
    expected_prices = expected_prices or {}
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
        if pid in expected_prices and expected_prices[pid] != product.price:
            return [], 0, (
                f"Цена «{product.name}» изменилась: теперь {product.price} ₽. "
                "Проверьте сумму в корзине и отправьте заявку ещё раз."
            )
        if product.stock < 1:
            return [], 0, f"«{product.name}» закончился. Уберите его из корзины."
        if qty > product.stock:
            return [], 0, f"«{product.name}»: в наличии только {product.stock} шт."
        lines.append((product, qty))
        total += product.price * qty
    return lines, total, ""


POPULAR_LIMIT = 4


def product_sales() -> dict:
    """Продано штук по каждому товару (без отменённых заказов): {id: количество}."""
    rows = db.session.execute(text(
        "SELECT CAST(json_extract(j.value, '$.product_id') AS INTEGER) AS pid, "
        "SUM(CAST(json_extract(j.value, '$.quantity') AS INTEGER)) AS qty "
        'FROM "order" AS o, json_each(o.items) AS j '
        "WHERE o.status != 'cancelled' AND o.items IS NOT NULL "
        "AND json_type(o.items) = 'array' GROUP BY pid"
    )).all()
    return {pid: qty for pid, qty in rows if pid and qty}


def by_popularity(products, sales: dict) -> list:
    """Сначала товары в наличии, затем по числу продаж; при равенстве — по порядку в каталоге."""
    return sorted(products, key=lambda p: (p.stock < 1, -sales.get(p.id, 0), p.id))


def _existing_order(client_token):
    if not client_token:
        return None
    return Order.query.filter_by(client_token=client_token).first()


def place_order(data: dict, cart: dict, expected_prices: dict = None, client_token=None):
    try:
        if not cart:
            raise OrderError("Корзина пуста — добавьте букет из каталога")
        existing = _existing_order(client_token)
        if existing is not None:
            # Повторная отправка той же формы (двойной клик, повтор запроса).
            return existing.total, ""
        lines, total, error = build_order(cart, expected_prices)
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
        token, chat_ids = telegram_settings()
        order = Order(
            date=now, flower=flower, total=total, items=items, client_token=client_token,
            notify_status="pending" if token and chat_ids else "off", **data,
        )
        db.session.add(order)
        db.session.commit()
    except OrderError as exc:
        db.session.rollback()
        return 0, str(exc)
    except IntegrityError:
        # Тот же токен успел сохранить параллельный запрос: остатки откатились.
        db.session.rollback()
        existing = _existing_order(client_token)
        if existing is not None:
            return existing.total, ""
        current_app.logger.exception("Не удалось сохранить заказ")
        return 0, "Произошла ошибка. Попробуйте позже."
    except Exception:
        current_app.logger.exception("Не удалось сохранить заказ")
        db.session.rollback()
        return 0, "Произошла ошибка. Попробуйте позже."

    notify_new_order(order)
    return total, ""


class OrderError(ValueError):
    pass


# Разрешённые переходы статусов. Доставленный заказ нельзя отменить
# (товар уже у покупателя), но можно вернуть «в работу» при ошибочном клике.
STATUS_TRANSITIONS = {
    "new": {"new", "in_work", "delivered", "cancelled"},
    "in_work": {"in_work", "new", "delivered", "cancelled"},
    "delivered": {"delivered", "in_work"},
    "cancelled": {"cancelled", "new"},
}


def allowed_statuses(current: str) -> set:
    return STATUS_TRANSITIONS.get(current, set(STATUSES))


def order_items(order):
    if order.items is not None:
        items = order.items
    else:
        # Only parse the exact historical format; never guess missing product IDs.
        for product in Product.query.filter(Product.name.contains(" шт), ")).all():
            if f"{product.name} (" in order.flower:
                raise OrderError("Состав старого заказа неоднозначен. Требуется ручная сверка.")
        matches = list(re.finditer(r"(.+?) \(([1-9][0-9]*) шт\)(?:, |\Z)", order.flower))
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


def _stock_failure_message(pid: int, qty: int, release: bool) -> str:
    row = db.session.execute(
        select(Product.name, Product.stock, Product.is_active).where(Product.id == pid)
    ).first()
    if row is None:
        return f"Товар (ID {pid}) из заказа удалён. Требуется ручная сверка остатков."
    name, stock, is_active = row
    if release:
        return (
            f"«{name}»: после возврата {qty} шт. остаток превысит лимит {MAX_STOCK} шт. "
            "Уменьшите остаток товара и повторите отмену."
        )
    if not is_active:
        return f"«{name}» скрыт из каталога. Включите показ товара, чтобы восстановить заказ."
    return f"«{name}»: в наличии {stock} шт., для восстановления заказа нужно {qty} шт."


def change_order_status(order, new_status):
    try:
        if new_status not in STATUSES:
            raise OrderError("Неизвестный статус")
        if new_status not in allowed_statuses(order.status):
            raise OrderError(
                f"Нельзя перевести заказ №{order.id} из статуса "
                f"«{STATUSES.get(order.status, order.status)}» в «{STATUSES[new_status]}»."
            )
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
                    raise OrderError(_stock_failure_message(pid, qty, release))
        db.session.commit()
    except OrderError as exc:
        db.session.rollback()
        return str(exc)
    except Exception:
        current_app.logger.exception("Не удалось изменить статус заказа")
        db.session.rollback()
        return "Не удалось изменить статус. Попробуйте позже."
    return ""


# Лимит Telegram — 4096 символов; оставляем запас на эмодзи (2 UTF-16 единицы).
TELEGRAM_TEXT_LIMIT = 4000
TRUNCATED_SUFFIX = "… (полный состав — в админке)"


def _order_message(order, flower: str) -> str:
    esc = html.escape
    lines = [
        f"<b>Новый заказ №{order.id}</b>",
        "",
        f"Имя: {esc(order.name)}",
        f"Телефон: {esc(order.phone)}",
        f"Букет: {esc(flower)}",
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


def format_order_message(order) -> str:
    flower = order.flower or ""
    text = _order_message(order, flower)
    if len(text) <= TELEGRAM_TEXT_LIMIT:
        return text
    # Обрезаем исходный текст (а не экранированный), чтобы не разрезать HTML-сущность.
    budget = len(flower) - (len(text) - TELEGRAM_TEXT_LIMIT) - len(TRUNCATED_SUFFIX)
    while budget > 0:
        text = _order_message(order, flower[:budget].rstrip() + TRUNCATED_SUFFIX)
        if len(text) <= TELEGRAM_TEXT_LIMIT:
            return text
        budget -= len(text) - TELEGRAM_TEXT_LIMIT
    return _order_message(order, TRUNCATED_SUFFIX.lstrip("… "))


TELEGRAM_RETRY_DELAYS = (5, 30)  # паузы перед 2-й и 3-й попыткой, секунды
NO_CONNECTION_MESSAGE = (
    "Нет связи с api.telegram.org (тайм-аут или обрыв соединения). Вероятно, Telegram "
    "заблокирован провайдером сервера — укажите прокси в TELEGRAM_PROXY."
)


PROXY_DEFAULT_PORTS = {"http": 80, "https": 443, "socks4": 1080, "socks4a": 1080,
                       "socks5": 1080, "socks5h": 1080}


def proxy_address(proxy_url: str):
    """(хост, порт, адрес для показа без логина и пароля) или None, если адрес не разобрать."""
    try:
        parts = urlsplit(proxy_url)
        port = parts.port or PROXY_DEFAULT_PORTS.get(parts.scheme.lower())
    except ValueError:
        return None
    if not parts.hostname or not port:
        return None
    return parts.hostname, port, f"{parts.scheme}://{parts.hostname}:{port}"


def proxy_through_message(proxy_url: str) -> str:
    address = proxy_address(proxy_url)
    shown = address[2] if address else "из TELEGRAM_PROXY"
    return (
        f"Нет связи с api.telegram.org через прокси {shown} (тайм-аут или обрыв соединения): "
        "прокси не запущен или сам не имеет доступа к Telegram"
    )


def probe_proxy(proxy_url: str) -> str:
    """Проверяет, что по адресу прокси вообще кто-то отвечает. '' — порт открыт."""
    address = proxy_address(proxy_url)
    if address is None:
        return "Неверный адрес прокси в TELEGRAM_PROXY"
    host, port, shown = address
    try:
        with socket.create_connection((host, port), timeout=5):
            return ""
    except OSError:
        return (
            f"Прокси {shown} не отвечает: по этому адресу ничего не запущено или порт закрыт. "
            "Запустите прокси-программу или укажите в TELEGRAM_PROXY адрес рабочего прокси"
        )


def telegram_settings():
    token = current_app.config.get("TELEGRAM_BOT_TOKEN", "").strip()
    raw_ids = current_app.config.get("TELEGRAM_CHAT_ID", "")
    chat_ids = [c.strip() for c in raw_ids.split(",") if c.strip()]
    return token, chat_ids


def telegram_call(token: str, method: str, payload: dict):
    """Один запрос к Bot API. Возвращает (result, ошибка, стоит_ли_повторять).

    Текст ошибки понятен администратору и никогда не содержит токен бота.
    """
    url = f"https://api.telegram.org/bot{token}/{method}"
    proxy_url = os.environ.get("TELEGRAM_PROXY", "").strip()
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    try:
        response = requests.post(url, data=payload, proxies=proxies, timeout=15)
    except requests.exceptions.InvalidSchema as exc:
        if "socks" in str(exc).lower():
            return None, (
                "Для SOCKS-прокси не установлен PySocks: выполните "
                "pip install -r requirements.txt"
            ), False
        return None, "Неверный адрес прокси в TELEGRAM_PROXY", False
    except (requests.exceptions.InvalidURL, requests.exceptions.MissingSchema):
        return None, "Неверный адрес прокси в TELEGRAM_PROXY", False
    except requests.exceptions.ProxyError:
        if proxy_url:
            return None, proxy_through_message(proxy_url), True
        return None, (
            "Системный прокси (переменная окружения HTTPS_PROXY) не пропустил соединение "
            "с api.telegram.org — укажите рабочий прокси в TELEGRAM_PROXY"
        ), True
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
        # Ошибки SOCKS-прокси приходят как ConnectionError, а не ProxyError.
        return None, proxy_through_message(proxy_url) if proxy_url else NO_CONNECTION_MESSAGE, True
    except requests.exceptions.RequestException as exc:
        return None, f"Ошибка запроса к Telegram ({type(exc).__name__})", True

    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    if response.status_code == 200 and body.get("ok"):
        return body.get("result"), "", False

    code = response.status_code
    description = str(body.get("description") or "")[:200]
    parameters = body.get("parameters") if isinstance(body.get("parameters"), dict) else {}
    if parameters.get("migrate_to_chat_id"):
        return None, (
            "Группа стала супергруппой: замените ID чата в TELEGRAM_CHAT_ID "
            f"на {parameters['migrate_to_chat_id']}"
        ), False
    if code in (401, 404):
        return None, "Неверный TELEGRAM_BOT_TOKEN: Telegram не узнал бота", False
    if code == 400 and "chat not found" in description.lower():
        return None, (
            "Чат не найден: проверьте TELEGRAM_CHAT_ID; бот должен быть добавлен в группу, "
            "а в личном чате нужно сначала нажать /start"
        ), False
    if code == 403:
        return None, (
            f"Бот не может писать в этот чат (заблокирован или удалён из группы): {description}"
        ), False
    if code == 429:
        return None, "Telegram временно ограничил частоту отправки", True
    if code >= 500:
        return None, f"Сервер Telegram временно недоступен (код {code})", True
    return None, f"Telegram отклонил сообщение (код {code}): {description}", False


def send_message(token: str, chat_id: str, text: str, retry_delays=None) -> str:
    """Отправка в один чат с повторами при временных сбоях. Возвращает текст ошибки или ''."""
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": json.dumps({"is_disabled": True}),
    }
    if retry_delays is None:
        retry_delays = TELEGRAM_RETRY_DELAYS
    error = ""
    for delay in (0, *retry_delays):
        if delay:
            time.sleep(delay)
        _, error, retryable = telegram_call(token, "sendMessage", payload)
        if not error or not retryable:
            break
    return error


def deliver_order_notification(app, order_id: int, token: str, chat_ids: list, text: str,
                               retry_delays=None) -> list:
    """Отправляет заказ во все чаты и сохраняет результат в заказе. Возвращает список ошибок."""
    errors = []
    for chat_id in chat_ids:
        try:
            error = send_message(token, chat_id, text, retry_delays)
        except Exception as exc:
            # Любой сбой должен попасть в статус заказа, а не оставить его «отправляется».
            error = f"внутренняя ошибка отправки ({type(exc).__name__})"
            app.logger.exception("Telegram: сбой при отправке заказа №%s", order_id)
        if error:
            errors.append(f"чат {chat_id}: {error}")
            app.logger.error("Telegram: заказ №%s не отправлен в чат %s: %s", order_id, chat_id, error)
    with app.app_context():
        try:
            db.session.execute(
                update(Order).where(Order.id == order_id).values(
                    notify_status="failed" if errors else "sent",
                    notify_error="; ".join(errors)[:500],
                ),
                execution_options={"synchronize_session": False},
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.exception("Не удалось сохранить статус уведомления заказа №%s", order_id)
    return errors


def start_background(target, *args) -> None:
    threading.Thread(target=target, args=args, daemon=True).start()


def notify_new_order(order) -> None:
    token, chat_ids = telegram_settings()
    if not token or not chat_ids:
        return
    try:
        text = format_order_message(order)
    except Exception:
        current_app.logger.exception("Не удалось собрать сообщение для Telegram")
        return
    start_background(
        deliver_order_notification,
        current_app._get_current_object(), order.id, token, chat_ids, text,
    )


def resend_order_notification(order) -> list:
    """Повторная отправка из админки: сразу, одна попытка на чат."""
    token, chat_ids = telegram_settings()
    if not token or not chat_ids:
        return ["Уведомления не настроены: задайте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env"]
    return deliver_order_notification(
        current_app._get_current_object(), order.id, token, chat_ids,
        format_order_message(order), retry_delays=(),
    )


def check_telegram() -> list:
    """Диагностика для админки: [(успех, сообщение), ...]."""
    token, chat_ids = telegram_settings()
    if not token:
        return [(False, "Не задан TELEGRAM_BOT_TOKEN в .env")]
    if not chat_ids:
        return [(False, "Не задан TELEGRAM_CHAT_ID в .env")]
    proxy_url = os.environ.get("TELEGRAM_PROXY", "").strip()
    address = proxy_address(proxy_url) if proxy_url else None
    proxy = f"через прокси {address[2]}" if address else (
        "через прокси TELEGRAM_PROXY" if proxy_url else "без прокси"
    )
    me, error, _ = telegram_call(token, "getMe", {})
    if error:
        if proxy_url and error == proxy_through_message(proxy_url):
            # Уточняем: прокси не запущен или запущен, но не видит Telegram.
            probe_error = probe_proxy(proxy_url)
            if probe_error:
                return [(False, f"Бот недоступен: {probe_error}")]
            return [(False, (
                f"Бот недоступен: прокси {address[2] if address else ''} отвечает, но через него "
                "нет связи с api.telegram.org. Похоже, прокси находится в стране, где Telegram "
                "заблокирован, или не пропускает такие соединения — нужен другой прокси (за рубежом)"
            ))]
        return [(False, f"Бот недоступен ({proxy}): {error}")]
    username = me.get("username", "?") if isinstance(me, dict) else "?"
    results = [(True, f"Связь с Telegram есть ({proxy}), бот @{username}")]
    for chat_id in chat_ids:
        error = send_message(
            token, chat_id, "Проверка уведомлений «Чечектер»: этот чат получает новые заказы.",
            retry_delays=(),
        )
        results.append((not error, f"Чат {chat_id}: " + (error or "тестовое сообщение отправлено")))
    return results


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


PNG_TRAILER = b"\x00\x00\x00\x00IEND\xaeB`\x82"


def has_valid_structure(stream, ext: str) -> bool:
    """Проверяет конец файла: отсекает «полиглоты» с HTML/скриптом после картинки."""
    try:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        if ext == "webp":
            stream.seek(4)
            declared = int.from_bytes(stream.read(4), "little")
            return declared + 8 == size
        stream.seek(max(0, size - 64))
        tail = stream.read()
        if ext == "png":
            return tail.endswith(PNG_TRAILER)
        return tail.rstrip(b"\x00\r\n").endswith(b"\xff\xd9")
    finally:
        stream.seek(0)


def save_image(file_storage):
    ext = detect_image_ext(file_storage.stream)
    if not ext:
        return None, "Фото должно быть в формате JPG, PNG или WEBP"
    if not has_valid_structure(file_storage.stream, ext):
        return None, "Файл фото повреждён или содержит посторонние данные"

    subdir = current_app.config["UPLOAD_SUBDIR"]
    filename = f"{secrets.token_hex(8)}.{ext}"
    try:
        file_storage.save(os.path.join(current_app.config["UPLOAD_DIR"], filename))
    except OSError:
        current_app.logger.exception("Не удалось сохранить фото")
        return None, "Не удалось сохранить фото. Попробуйте ещё раз."
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


def _attempt_key(ip: str) -> str:
    return f"ip:{ip}"[:100]


# Попытки хранятся в базе: лимит общий для всех процессов и переживает перезапуск.
def too_many_attempts(ip: str) -> bool:
    db.session.query(LoginAttempt).filter(
        LoginAttempt.created_at < time.time() - WINDOW_SECONDS
    ).delete(synchronize_session=False)
    db.session.commit()
    return LoginAttempt.query.filter_by(key=_attempt_key(ip)).count() >= MAX_ATTEMPTS


def register_failed_login(ip: str) -> None:
    db.session.add(LoginAttempt(key=_attempt_key(ip), created_at=time.time()))
    db.session.commit()


def reset_attempts(ip: str) -> None:
    LoginAttempt.query.filter_by(key=_attempt_key(ip)).delete(synchronize_session=False)
    db.session.commit()
