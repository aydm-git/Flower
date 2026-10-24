import io
import os
import re
import struct
import sys
import tempfile
import unittest
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo


def tiny_png() -> bytes:
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"\x00\xff\x00\x00")
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", pixels) + chunk(b"IEND", b""))


def fixed_clock(helpers, instant):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz)

    return patch.object(helpers, "datetime", FixedDatetime)


class FixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
        cls.addClassCleanup(sys.path.pop, 0)
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        with patch.dict(os.environ, {"SECRET_KEY": "fix-tests-only"}):
            from config import Config
            with patch.multiple(Config, SQLALCHEMY_DATABASE_URI="sqlite://",
                                UPLOAD_DIR=cls.temporary.name):
                from app import create_app

            class TestConfig(Config):
                TESTING = True
                WTF_CSRF_ENABLED = False
                SQLALCHEMY_DATABASE_URI = "sqlite:///" + str(
                    Path(cls.temporary.name) / "fixes.db"
                ).replace("\\", "/")
                UPLOAD_DIR = cls.temporary.name
                TELEGRAM_BOT_TOKEN = ""
                TELEGRAM_CHAT_ID = ""
                SHOP_TIMEZONE = "Asia/Novosibirsk"
                ORDERS_PER_PAGE = 50

            cls.app = create_app(TestConfig)
        import helpers
        import models
        cls.helpers, cls.models = helpers, models
        cls.Order, cls.Product, cls.db = models.Order, models.Product, models.db

        def close_database():
            with cls.app.app_context():
                models.db.session.remove()
                models.db.engine.dispose()

        cls.addClassCleanup(close_database)

    def setUp(self):
        self.context = self.app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)
        self.db.drop_all()
        self.db.create_all()
        self.db.session.add_all([
            self.Product(id=1, name="Розы", price=2500, stock=5, img="images/roses.jpeg"),
            self.Product(id=2, name="Тюльпаны", price=1800, stock=3, img="images/tulps.jpg"),
        ])
        self.db.session.commit()
        self.data = dict(name="Иванов Пётр", phone="+79991234567", is_pickup=True,
                         address="", delivery_date="2026-10-24", delivery_time="10:00–12:00",
                         card_text="", comment="")
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["logged_in"] = True

    def buy(self, cart=None, **kwargs):
        total, error = self.helpers.place_order(self.data, cart or {1: 1}, **kwargs)
        self.assertEqual(error, "")
        return self.Order.query.order_by(self.Order.id.desc()).first()

    def stock(self, pid=1):
        self.db.session.expire_all()
        return self.db.session.get(self.Product, pid).stock

    def order_form(self, **overrides):
        tomorrow = (self.helpers.shop_now().date() + timedelta(days=1)).isoformat()
        form = {"name": "Иванов", "phone": "+7 (999) 123-45-67", "is_pickup": "1",
                "delivery_date": tomorrow, "delivery_time": "10:00–12:00",
                "cart": '[{"id":1,"quantity":1,"price":2500}]'}
        form.update(overrides)
        return form

    # --- В-1: редактирование товара не затирает остаток ---
    def test_edit_form_does_not_overwrite_stock(self):
        self.buy({1: 2})  # пока «форма открыта», купили 2 букета
        response = self.client.post("/admin/products/1/edit", data={
            "name": "Розы", "price": "2500", "stock": "5", "is_active": "1",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.stock(), 3)

    # --- С-1: удалить можно только отменённый заказ ---
    def test_active_order_cannot_be_deleted(self):
        order = self.buy()
        self.client.post(f"/admin/orders/{order.id}/delete")
        self.assertIsNotNone(self.db.session.get(self.Order, order.id))
        self.assertEqual(self.stock(), 4)
        self.assertEqual(self.helpers.change_order_status(order, "cancelled"), "")
        self.client.post(f"/admin/orders/{order.id}/delete")
        self.db.session.expire_all()
        self.assertIsNone(self.db.session.get(self.Order, order.id))
        self.assertEqual(self.stock(), 5)

    # --- С-2: правила смены статусов ---
    def test_delivered_order_cannot_be_cancelled(self):
        order = self.buy()
        self.assertEqual(self.helpers.change_order_status(order, "delivered"), "")
        self.assertIn("Нельзя", self.helpers.change_order_status(order, "cancelled"))
        self.assertEqual(self.stock(), 4)
        self.assertEqual(self.helpers.change_order_status(order, "in_work"), "")

    def test_disallowed_statuses_are_disabled_in_admin(self):
        order = self.buy()
        self.helpers.change_order_status(order, "delivered")
        page = self.client.get("/admin").get_data(as_text=True)
        self.assertRegex(page, r'<option value="cancelled"\s+disabled>')

    # --- С-3: нет inline-JS с данными ---
    def test_product_name_with_quote_is_not_injected_into_js(self):
        self.db.session.get(self.Product, 1).name = "Mother's Day'); alert(1); ('"
        self.db.session.commit()
        page = self.client.get("/admin/products").get_data(as_text=True)
        self.assertNotIn("onsubmit", page)
        self.assertNotIn("onchange", page)
        self.assertIn('data-confirm="Удалить товар «Mother&#39;s Day&#39;); alert(1)', page)

    # --- С-4: дата хранится в ISO ---
    def test_non_iso_delivery_date_is_rejected_and_old_dates_are_normalized(self):
        future = self.helpers.shop_now().date() + timedelta(days=40)
        loose = f"{future.year}-{future.month}-{future.day}"
        if loose != future.isoformat():
            self.assertTrue(self.helpers.validate_delivery_slot(loose, "10:00–12:00"))
        order = self.buy()
        order.delivery_date = "2026-1-5"
        self.db.session.commit()
        self.models.normalize_delivery_dates()
        self.db.session.expire_all()
        self.assertEqual(self.db.session.get(self.Order, order.id).delivery_date, "2026-01-05")

    # --- С-5: поиск по кириллице без учёта регистра, % не шаблон ---
    def test_cyrillic_search_ignores_case_and_escapes_wildcards(self):
        order = self.buy()
        page = self.client.get("/admin", query_string={"q": "иВАНОВ"}).get_data(as_text=True)
        self.assertIn("Иванов Пётр", page)
        page = self.client.get("/admin", query_string={"q": "%"}).get_data(as_text=True)
        self.assertNotIn("Иванов Пётр", page)
        page = self.client.get("/admin", query_string={"q": f"№{order.id}"}).get_data(as_text=True)
        self.assertIn("Иванов Пётр", page)

    # --- С-6: попытки входа хранятся в базе ---
    def test_login_attempts_are_limited_through_database(self):
        from werkzeug.security import generate_password_hash
        self.app.config.update(ADMIN_USERNAME="admin",
                               ADMIN_PASSWORD_HASH=generate_password_hash("secret"))
        self.addCleanup(self.app.config.update, ADMIN_USERNAME="", ADMIN_PASSWORD_HASH="")
        client = self.app.test_client()
        for _ in range(5):
            self.assertEqual(client.post("/login", data={"username": "admin",
                                                         "password": "bad"}).status_code, 200)
        self.assertEqual(self.models.LoginAttempt.query.count(), 5)
        response = client.post("/login", data={"username": "admin", "password": "secret"})
        self.assertEqual(response.status_code, 429)

    # --- С-7: ошибка базы не превращается в 500 ---
    def test_admin_write_error_is_reported_instead_of_500(self):
        from sqlalchemy.exc import OperationalError
        error = OperationalError("UPDATE", {}, Exception("database is locked"))
        with patch.object(self.db.session, "commit", side_effect=error), \
                self.assertLogs(self.app.logger, level="ERROR"):
            response = self.client.post("/admin/products/1/toggle", data={"active": "0"},
                                        follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Не удалось сохранить", response.get_data(as_text=True))

    def test_unknown_page_uses_friendly_404(self):
        response = self.client.get("/product/999")
        self.assertEqual(response.status_code, 404)
        self.assertIn("Страница не найдена", response.get_data(as_text=True))

    # --- С-8: пагинация ---
    def test_order_list_is_paginated(self):
        for i in range(55):
            self.db.session.add(self.Order(date="2026-10-23", flower="Розы (1 шт)", total=1,
                                           **{**self.data, "name": f"Клиент {i:02d}"}))
        self.db.session.commit()
        first = self.client.get("/admin").get_data(as_text=True)
        second = self.client.get("/admin?page=2").get_data(as_text=True)
        self.assertIn("Клиент 54", first)
        self.assertNotIn("Клиент 04", first)
        self.assertIn("Клиент 04", second)
        self.assertEqual(self.client.get("/admin?page=99999999999999999999").status_code, 200)

    # --- С-9: Telegram ---
    def test_long_telegram_message_is_truncated_without_breaking_html(self):
        order = self.buy()
        order.flower = ", ".join(f"Букет & Co №{i} (99 шт)" for i in range(400))
        text = self.helpers.format_order_message(order)
        self.assertLessEqual(len(text), self.helpers.TELEGRAM_TEXT_LIMIT)
        self.assertIn("полный состав — в админке", text)
        self.assertNotRegex(text, r"&(?!amp;|lt;|gt;|quot;|#x27;)")

    def test_telegram_error_logs_status_code(self):
        import requests
        response = requests.Response()
        response.status_code = 400
        with patch.object(self.helpers.requests, "post",
                          side_effect=requests.HTTPError(response=response)), \
                self.assertLogs(self.app.logger, level="ERROR") as logs:
            self.helpers.send_telegram("t", ["1"], "x", self.app.logger)
        self.assertIn("HTTPError 400", logs.output[0])

    # --- Н-1 / Н-6: фото ---
    def test_image_with_appended_payload_is_rejected(self):
        good, bad = tiny_png(), tiny_png() + b"<script>alert(1)</script>"
        response = self.client.post("/admin/products/new", data={
            "name": "Новый", "price": "100", "stock": "1", "is_active": "1",
            "image": (io.BytesIO(bad), "x.png"),
        }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/admin/products/new", data={
            "name": "Новый", "price": "100", "stock": "1", "is_active": "1",
            "image": (io.BytesIO(good), "x.png"),
        }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 302)

    def test_old_image_is_kept_when_save_fails(self):
        upload_dir = Path(self.temporary.name) / "edit-case"
        upload_dir.mkdir(exist_ok=True)
        subdir = upload_dir.name
        (upload_dir / "old.png").write_bytes(tiny_png())
        product = self.db.session.get(self.Product, 1)
        product.img = f"{subdir}/old.png"
        self.db.session.commit()
        original_static = self.app.static_folder
        self.app.static_folder = str(upload_dir.parent)
        self.addCleanup(setattr, self.app, "static_folder", original_static)
        with patch.dict(self.app.config, {"UPLOAD_SUBDIR": subdir,
                                             "UPLOAD_DIR": str(upload_dir)}), \
                patch.object(self.db.session, "commit", side_effect=RuntimeError("fail")), \
                self.assertLogs(self.app.logger, level="ERROR"):
            response = self.client.post("/admin/products/1/edit", data={
                "name": "Розы", "price": "2500", "is_active": "1",
                "image": (io.BytesIO(tiny_png()), "n.png"),
            }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 400)
        # Старое фото на месте, новое (несохранённое) удалено.
        self.assertEqual(sorted(p.name for p in upload_dir.glob("*.png")), ["old.png"])

    # --- Н-2 / остатки ---
    def test_toggle_sets_requested_state(self):
        for _ in range(2):
            self.client.post("/admin/products/1/toggle", data={"active": "0"})
        self.db.session.expire_all()
        self.assertFalse(self.db.session.get(self.Product, 1).is_active)

    def test_stock_can_change_by_custom_amount(self):
        self.client.post("/admin/products/1/stock", data={"delta": "20"})
        self.assertEqual(self.stock(), 25)
        self.client.post("/admin/products/1/stock", data={"delta": "-100"})
        self.assertEqual(self.stock(), 0)

    # --- Н-3: изменившаяся цена ---
    def test_changed_price_rejects_order(self):
        cart, prices, error = self.helpers.read_cart('[{"id":1,"quantity":1,"price":2000}]')
        self.assertEqual((cart, prices, error), ({1: 1}, {1: 2000}, ""))
        total, error = self.helpers.place_order(self.data, cart, prices)
        self.assertIn("Цена «Розы» изменилась", error)
        self.assertEqual(self.stock(), 5)

    # --- Н-4, Н-5 ---
    def test_logout_requires_post_and_headers_are_set(self):
        self.assertEqual(self.client.get("/logout").status_code, 405)
        response = self.client.get("/")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
        self.assertNotIn("<script>\n", response.get_data(as_text=True))

    # --- Н-7, Н-8 ---
    def test_phone_is_normalized_and_whitespace_is_limited(self):
        self.assertFalse(self.helpers.is_valid_phone("+7\n999\t111 22 33"))
        response = self.client.post("/", data=self.order_form())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.Order.query.one().phone, "+79991234567")

    def test_broken_cart_has_its_own_message(self):
        response = self.client.post("/", data=self.order_form(cart='[{"id":1,"quantity":0}]'))
        self.assertEqual(response.status_code, 400)
        self.assertIn("Корзина повреждена", response.get_data(as_text=True))

    # --- Н-9: понятная причина отказа в отмене ---
    def test_cancel_over_stock_limit_explains_reason(self):
        order = self.buy()
        self.db.session.get(self.Product, 1).stock = 10_000
        self.db.session.commit()
        self.assertIn("превысит лимит", self.helpers.change_order_status(order, "cancelled"))

    # --- Н-14: повторная отправка формы ---
    def test_same_form_token_creates_one_order(self):
        token = self.helpers.new_order_token()
        form = self.order_form(order_token=token)
        self.assertEqual(self.client.post("/", data=form).status_code, 302)
        self.assertEqual(self.client.post("/", data=form).status_code, 302)
        self.assertEqual(self.Order.query.count(), 1)
        self.assertEqual(self.stock(), 4)

    # --- Перенос даты на следующий день ---
    def test_date_moves_to_tomorrow_when_today_is_closed(self):
        tz = ZoneInfo("Asia/Novosibirsk")
        evening = datetime(2026, 10, 23, 21, 30, tzinfo=tz).astimezone(timezone.utc)
        with fixed_clock(self.helpers, evening):
            self.assertEqual(self.helpers.delivery_date_limits()[0], "2026-10-24")
            self.assertEqual(self.helpers.validate_delivery_slot("2026-10-23", "20:00–22:00"),
                             self.helpers.TODAY_CLOSED_MESSAGE)
            self.assertEqual(self.helpers.validate_delivery_slot("2026-10-24", "10:00–12:00"), "")
            page = self.client.get("/").get_data(as_text=True)
            self.assertIn('value="2026-10-24"', page)
            self.assertIn('min="2026-10-24"', page)
            self.assertRegex(page, r'id="date-hint">')
            response = self.client.post("/", data=self.order_form(
                delivery_date="2026-10-23", delivery_time="20:00–22:00"))
            body = response.get_data(as_text=True)
            self.assertEqual(response.status_code, 400)
            self.assertIn("дата перенесена на завтра", body)
            self.assertIn('value="2026-10-24"', body)

    def test_date_stays_today_while_slots_remain(self):
        tz = ZoneInfo("Asia/Novosibirsk")
        afternoon = datetime(2026, 10, 23, 20, 30, tzinfo=tz).astimezone(timezone.utc)
        with fixed_clock(self.helpers, afternoon):
            self.assertEqual(self.helpers.delivery_date_limits()[0], "2026-10-23")
            page = self.client.get("/").get_data(as_text=True)
            self.assertIn('value="2026-10-23"', page)
            self.assertRegex(page, r'id="date-hint" hidden>')

    # --- Н-11, Н-12 ---
    def test_demo_products_are_seeded_once(self):
        self.db.drop_all()
        self.models.ensure_schema()
        self.models.ensure_schema()
        self.assertEqual(self.Product.query.count(), len(self.models.DEFAULT_PRODUCTS))
        self.assertEqual(self.models.AppMeta.query.count(), 1)

    def test_product_referenced_by_legacy_order_cannot_be_deleted(self):
        order = self.buy({2: 1})
        order.items = None
        self.db.session.commit()
        self.client.post("/admin/products/2/delete")
        self.db.session.expire_all()
        self.assertIsNotNone(self.db.session.get(self.Product, 2))


    # --- Каталог списком и популярные букеты ---
    def add_products(self):
        self.db.session.add_all([
            self.Product(id=3, name="Альстромерии", price=900, stock=4, img="images/mix.jpeg",
                         composition="Альстромерии, зелень"),
            self.Product(id=4, name="Пионы", price=5000, stock=0, img="images/box.jpg"),
            self.Product(id=5, name="Гортензия", price=3000, stock=10, img="images/box.jpg"),
            self.Product(id=6, name="Скрытый", price=100, stock=9, img="images/box.jpg",
                         is_active=False),
        ])
        self.db.session.commit()

    def test_catalog_lists_all_active_products(self):
        self.add_products()
        page = self.client.get("/catalog").get_data(as_text=True)
        for name in ["Розы", "Тюльпаны", "Альстромерии", "Пионы", "Гортензия"]:
            self.assertIn(name, page)
        self.assertNotIn("Скрытый", page)
        self.assertIn("Альстромерии, зелень", page)
        self.assertEqual(page.count('class="catalog-item'), 5)

    def test_catalog_sorting_keeps_sold_out_last(self):
        self.add_products()

        def order(sort):
            page = self.client.get("/catalog", query_string={"sort": sort}).get_data(as_text=True)
            names = ["Розы", "Тюльпаны", "Альстромерии", "Пионы", "Гортензия"]
            return sorted(names, key=lambda n: page.index(f">{n}</a>"))

        self.assertEqual(order("cheap"),
                         ["Альстромерии", "Тюльпаны", "Розы", "Гортензия", "Пионы"])
        self.assertEqual(order("expensive"),
                         ["Гортензия", "Розы", "Тюльпаны", "Альстромерии", "Пионы"])
        self.assertEqual(order("name"),
                         ["Альстромерии", "Гортензия", "Розы", "Тюльпаны", "Пионы"])
        self.assertEqual(self.client.get("/catalog?sort=bogus").status_code, 200)

    def test_home_shows_best_sellers_and_cancelled_orders_do_not_count(self):
        self.add_products()
        self.buy({5: 2})
        self.buy({3: 1})
        cancelled = self.buy({2: 3})
        self.assertEqual(self.helpers.change_order_status(cancelled, "cancelled"), "")
        sales = self.helpers.product_sales()
        self.assertEqual(sales, {5: 2, 3: 1})
        page = self.client.get("/").get_data(as_text=True)
        catalog = page[page.index('id="catalog"'):page.index('id="order"')]
        self.assertEqual(len(re.findall(r'class="card(?: card-soldout)?"', catalog)),
                         self.helpers.POPULAR_LIMIT)
        self.assertLess(catalog.index(">Гортензия</a>"), catalog.index(">Альстромерии</a>"))
        self.assertLess(catalog.index(">Альстромерии</a>"), catalog.index(">Розы</a>"))
        self.assertNotIn("Пионы", catalog)  # нет в наличии — уступает место
        self.assertIn("Весь каталог (5)", catalog)
        listing = self.client.get("/catalog").get_data(as_text=True)
        self.assertEqual(listing.count("badge-hit"), 2)


if __name__ == "__main__":
    unittest.main()
