import sqlite3
from datetime import datetime

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, OperationalError

db = SQLAlchemy()


def _casefold(value):
    return value.casefold() if isinstance(value, str) else value


@event.listens_for(Engine, "connect")
def _configure_sqlite(dbapi_connection, _record):
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    # Встроенные lower()/LIKE в SQLite понимают регистр только для латиницы.
    dbapi_connection.create_function("casefold", 1, _casefold, deterministic=True)
    cursor = dbapi_connection.cursor()
    try:
        # WAL позволяет читать во время записи и снижает число «database is locked».
        cursor.execute("PRAGMA journal_mode=WAL")
    finally:
        cursor.close()

STATUSES = {
    "new": "Новый",
    "in_work": "В работе",
    "delivered": "Доставлен",
    "cancelled": "Отменён",
}


def to_ru_date(iso: str) -> str:
    parts = (iso or "").split("-")
    return f"{parts[2]}.{parts[1]}.{parts[0]}" if len(parts) == 3 else ""


class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(50), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    flower = db.Column(db.Text, nullable=False)
    total = db.Column(db.BigInteger, nullable=False, default=0)
    status = db.Column(db.String(20), nullable=False, default="new", index=True)
    # NULL identifies legacy orders whose product IDs were not recorded.
    items = db.Column(db.JSON, nullable=True)
    stock_released = db.Column(db.Boolean, nullable=False, default=False)

    is_pickup = db.Column(db.Boolean, nullable=False, default=False)
    address = db.Column(db.String(255), nullable=False, default="")
    delivery_date = db.Column(db.String(10), nullable=False, default="", index=True)
    delivery_time = db.Column(db.String(50), nullable=False, default="")
    card_text = db.Column(db.String(300), nullable=False, default="")
    comment = db.Column(db.String(500), nullable=False, default="")
    # Одноразовый токен формы: повторная отправка не создаёт второй заказ.
    client_token = db.Column(db.String(64), nullable=True, unique=True, index=True)
    # Уведомление в Telegram: off — не настроено, pending — отправляется,
    # sent — доставлено во все чаты, failed — ошибка (причина в notify_error).
    notify_status = db.Column(db.String(20), nullable=False, default="")
    notify_error = db.Column(db.String(500), nullable=False, default="")
    # Письмо о заказе на почту из настроек админки: те же статусы, что и у Telegram.
    email_status = db.Column(db.String(20), nullable=False, default="")
    email_error = db.Column(db.String(500), nullable=False, default="")

    @property
    def delivery_date_ru(self) -> str:
        return to_ru_date(self.delivery_date)


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    price = db.Column(db.Integer, nullable=False)
    stock = db.Column(db.Integer, nullable=False, default=0)
    img = db.Column(db.String(255), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    description = db.Column(db.Text, nullable=False, default="")
    composition = db.Column(db.String(500), nullable=False, default="")


class LoginAttempt(db.Model):
    """Неудачные попытки входа; общие для всех процессов приложения."""

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), nullable=False, index=True)
    created_at = db.Column(db.Float, nullable=False, index=True)


class AppMeta(db.Model):
    """Служебные отметки (например, что демо-товары уже добавлены)."""

    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.Text, nullable=False, default="")


DEFAULT_PRODUCTS = [
    {
        "name": "Букет роз",
        "price": 2500,
        "stock": 20,
        "img": "images/roses.jpeg",
        "composition": "Красные розы",
        "description": (
            "Классический букет из красных роз в матовой плёнке. "
            "Подойдёт для романтического подарка, юбилея или просто знака внимания."
        ),
    },
    {
        "name": "Тюльпаны",
        "price": 1800,
        "stock": 20,
        "img": "images/tulps.jpg",
        "composition": "Розовые тюльпаны",
        "description": (
            "Нежный букет из розовых тюльпанов — лёгкий весенний подарок. "
            "Подойдёт для поздравления мамы, подруги или коллеги."
        ),
    },
    {
        "name": "Микс-букет",
        "price": 3200,
        "stock": 20,
        "img": "images/mix.jpeg",
        "composition": "Розы красные, розовые, белые и кремовые",
        "description": (
            "Яркий микс из роз разных оттенков в нежной розовой упаковке. "
            "Для тех, кто не хочет выбирать один цвет."
        ),
    },
    {
        "name": "Цветы в коробке",
        "price": 4000,
        "stock": 20,
        "img": "images/box.jpg",
        "composition": "Розы, хризантемы, мелкие цветы и зелень",
        "description": (
            "Готовая композиция в круглой розовой коробке с атласным бантом. "
            "Праздничный подарок, который выглядит нарядно без дополнительной упаковки."
        ),
    },
]


def add_column_if_missing(model, column_name: str, ddl: str) -> bool:
    columns = {c["name"] for c in inspect(db.engine).get_columns(model.__tablename__)}
    if column_name in columns:
        return False
    try:
        with db.engine.begin() as conn:
            conn.execute(text(f'ALTER TABLE "{model.__tablename__}" ADD COLUMN {ddl}'))
        return True
    except OperationalError as e:
        if "duplicate column name" in str(e).lower():
            return False  # другой worker уже добавил колонку
        raise

def ensure_schema() -> None:
    first_products_run = not inspect(db.engine).has_table(Product.__tablename__)
    db.create_all()
    new_order_columns = {
        "total": "total INTEGER NOT NULL DEFAULT 0",
        "status": "status VARCHAR(20) NOT NULL DEFAULT 'new'",
        "items": "items JSON",
        "stock_released": "stock_released BOOLEAN NOT NULL DEFAULT 0",
        "is_pickup": "is_pickup BOOLEAN NOT NULL DEFAULT 0",
        "address": "address VARCHAR(255) NOT NULL DEFAULT ''",
        "delivery_date": "delivery_date VARCHAR(10) NOT NULL DEFAULT ''",
        "delivery_time": "delivery_time VARCHAR(50) NOT NULL DEFAULT ''",
        "card_text": "card_text VARCHAR(300) NOT NULL DEFAULT ''",
        "comment": "comment VARCHAR(500) NOT NULL DEFAULT ''",
        "client_token": "client_token VARCHAR(64)",
        "notify_status": "notify_status VARCHAR(20) NOT NULL DEFAULT ''",
        "notify_error": "notify_error VARCHAR(500) NOT NULL DEFAULT ''",
        "email_status": "email_status VARCHAR(20) NOT NULL DEFAULT ''",
        "email_error": "email_error VARCHAR(500) NOT NULL DEFAULT ''",
    }
    for column_name, ddl in new_order_columns.items():
        add_column_if_missing(Order, column_name, ddl)

    with db.engine.begin() as conn:
        conn.execute(text('CREATE INDEX IF NOT EXISTS ix_order_status ON "order" (status)'))
        conn.execute(text(
            'CREATE INDEX IF NOT EXISTS ix_order_delivery_date ON "order" (delivery_date)'
        ))
        conn.execute(text(
            'CREATE UNIQUE INDEX IF NOT EXISTS ix_order_client_token ON "order" (client_token)'
        ))
    normalize_delivery_dates()

    texts_added = add_column_if_missing(
        Product, "description", "description TEXT NOT NULL DEFAULT ''"
    )
    add_column_if_missing(
        Product, "composition", "composition VARCHAR(500) NOT NULL DEFAULT ''"
    )
    if texts_added:
        for item in DEFAULT_PRODUCTS:
            Product.query.filter_by(name=item["name"], description="", composition="").update(
                {"description": item["description"], "composition": item["composition"]}
            )
        db.session.commit()

    if first_products_run:
        # Отметка с уникальным ключом: при одновременном старте нескольких
        # процессов демо-товары добавит только один из них.
        db.session.add(AppMeta(key="demo_products", value="seeded"))
        for item in DEFAULT_PRODUCTS:
            db.session.add(Product(**item))
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()


def normalize_delivery_dates() -> None:
    """Приводит даты вида 2026-1-5 (старые версии их принимали) к ISO 2026-01-05."""
    with db.engine.begin() as conn:
        rows = conn.execute(text(
            'SELECT id, delivery_date FROM "order" WHERE delivery_date != \'\' AND '
            "delivery_date NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"
        )).all()
        for order_id, value in rows:
            try:
                fixed = datetime.strptime(value.strip(), "%Y-%m-%d").date().isoformat()
            except ValueError:
                continue
            conn.execute(
                text('UPDATE "order" SET delivery_date = :d WHERE id = :id'),
                {"d": fixed, "id": order_id},
            )
