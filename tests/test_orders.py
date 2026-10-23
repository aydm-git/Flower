import json
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo


class OrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
        cls.addClassCleanup(sys.path.pop, 0)
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        with patch.dict(os.environ, {"SECRET_KEY": "order-tests-only"}):
            from config import Config
            with patch.multiple(Config, SQLALCHEMY_DATABASE_URI="sqlite://",
                                UPLOAD_DIR=cls.temporary.name):
                from app import create_app

            class TestConfig(Config):
                TESTING = True
                WTF_CSRF_ENABLED = False
                SQLALCHEMY_DATABASE_URI = "sqlite:///" + str(
                    Path(cls.temporary.name) / "orders.db"
                ).replace("\\", "/")
                UPLOAD_DIR = cls.temporary.name
                TELEGRAM_BOT_TOKEN = ""
                TELEGRAM_CHAT_ID = ""
                SHOP_TIMEZONE = "Asia/Novosibirsk"

            cls.app = create_app(TestConfig)
        import helpers
        from models import Order, Product, db
        cls.helpers, cls.Order, cls.Product, cls.db = helpers, Order, Product, db

        def close_database():
            with cls.app.app_context():
                db.session.remove()
                db.engine.dispose()

        cls.addClassCleanup(close_database)

    def setUp(self):
        self.context = self.app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)
        self.db.drop_all()
        self.db.create_all()
        self.db.session.add_all([
            self.Product(id=1, name="Розы", price=2500, stock=2, img="images/roses.jpeg"),
            self.Product(id=2, name="Тюльпаны", price=1800, stock=3, img="images/tulps.jpg"),
        ])
        self.db.session.commit()
        self.data = dict(name="Покупатель", phone="+79991234567", is_pickup=True,
                         address="", delivery_date="2026-10-24", delivery_time="10:00–12:00",
                         card_text="", comment="")
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["logged_in"] = True

    def buy(self, cart=None):
        total, error = self.helpers.place_order(self.data, cart or {1: 1})
        self.assertEqual(error, "")
        return self.Order.query.order_by(self.Order.id.desc()).first()

    def stock(self, pid=1):
        self.db.session.expire_all()
        return self.db.session.get(self.Product, pid).stock

    def test_valid_cart_and_duplicate_items(self):
        self.assertEqual(self.helpers.parse_cart(
            '[{"id":"1","quantity":2},{"id":1,"quantity":"3"}]'
        ), {1: 5})

    def test_invalid_cart_numbers_reject_whole_cart(self):
        for invalid in ['1e309', '-1e309', 'NaN', 'Infinity', '1.5', 'true', 'null',
                        '0', '-1', '9223372036854775808', '"1e309"', '"1.0"', '[]', '{}']:
            for field in ["id", "quantity"]:
                with self.subTest(value=invalid, field=field):
                    item = '{"id":1,"quantity":1}'.replace(
                        '"' + field + '":1', '"' + field + '":' + invalid
                    )
                    self.assertEqual(self.helpers.parse_cart(
                        '[{"id":2,"quantity":1},' + item + ']'
                    ), {})
        self.assertEqual(self.helpers.parse_cart('[{"id":1,"quantity":100}]'), {})
        self.assertEqual(self.helpers.parse_cart(
            '[{"id":1,"quantity":99},{"id":1,"quantity":1}]'
        ), {})
        self.assertEqual(self.helpers.parse_cart('[' * 2000 + ']' * 2000), {})
        self.assertEqual(self.helpers.parse_cart(json.dumps([{"id":1,"quantity":1}] * 51)), {})

    def test_invalid_cart_http_returns_400(self):
        response = self.client.post("/", data={**self.data, "is_pickup": "1",
            "delivery_date": (self.helpers.shop_now().date() + timedelta(days=1)).isoformat(),
            "cart": '[{"id":1,"quantity":1e309}]'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.Order.query.count(), 0)

    def test_delivery_time_boundaries(self):
        now = datetime(2026, 10, 23, 11, tzinfo=ZoneInfo("Asia/Novosibirsk"))
        with patch.object(self.helpers, "shop_now", return_value=now):
            self.assertTrue(self.helpers.validate_delivery_slot("2026-10-23", "10:00–12:00"))
            self.assertEqual(self.helpers.validate_delivery_slot("2026-10-23", "12:00–14:00"), "")
            self.assertEqual(self.helpers.validate_delivery_slot("2026-10-24", "10:00–12:00"), "")
            self.assertTrue(self.helpers.validate_delivery_slot("2026-10-22", "10:00–12:00"))
            self.assertTrue(self.helpers.validate_delivery_slot("2026-12-23", "10:00–12:00"))
            self.assertTrue(self.helpers.validate_delivery_slot("2026-10-24", self.helpers.ASAP_SLOT))
            self.assertTrue(self.helpers.validate_delivery_slot("bad-date", "10:00–12:00"))
            self.assertTrue(self.helpers.validate_delivery_slot("2026-10-24", "bad-slot"))

    def test_asap_requires_time_before_closing(self):
        for hour, minute, allowed in [(9, 59, False), (10, 0, True), (20, 0, True),
                                      (20, 1, False), (23, 0, False)]:
            now = datetime(2026, 10, 23, hour, minute, tzinfo=ZoneInfo("Asia/Novosibirsk"))
            with self.subTest(hour=hour, minute=minute), patch.object(
                self.helpers, "shop_now", return_value=now
            ):
                self.assertEqual(not self.helpers.validate_delivery_slot(
                    "2026-10-23", self.helpers.ASAP_SLOT), allowed)

    def test_shop_timezone_at_midnight(self):
        instant = datetime(2026, 10, 23, 18, tzinfo=timezone.utc)

        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return instant.astimezone(tz)

        with patch.object(self.helpers, "datetime", FixedDatetime):
            self.assertEqual(self.helpers.delivery_date_limits()[0], "2026-10-24")
            self.assertEqual(self.helpers.shop_now().hour, 1)
            self.assertTrue(self.helpers.validate_delivery_slot("2026-10-23", "20:00–22:00"))
            order = self.buy()
            self.assertEqual(order.date, "2026-10-24 01:00:00")
            page = self.client.get("/").get_data(as_text=True)
            self.assertIn('data-timezone="Asia/Novosibirsk"', page)
            self.assertIn('min="2026-10-24"', page)

    def test_checkout_records_structured_items_and_total(self):
        order = self.buy({1: 2, 2: 1})
        self.assertEqual(order.total, 6800)
        self.assertEqual(order.items[0], dict(product_id=1, name="Розы", quantity=2, price=2500))
        self.assertEqual(self.stock(), 0)
        self.assertEqual(self.stock(2), 2)

    def test_two_buyers_cannot_purchase_last_item(self):
        self.db.session.get(self.Product, 1).stock = 1
        self.db.session.commit()
        barrier = threading.Barrier(2)
        original = self.helpers.build_order

        def synchronized_build(*args):
            result = original(*args)
            barrier.wait(timeout=10)
            return result

        def buy():
            with self.app.app_context():
                return self.helpers.place_order(self.data, {1: 1})

        with patch.object(self.helpers, "build_order", synchronized_build):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: buy(), range(2)))
        self.assertEqual(sum(error == "" for _, error in results), 1, results)
        self.assertEqual(self.stock(), 0)
        self.assertEqual(self.Order.query.count(), 1)

    def test_stock_conflict_rolls_back_all_items(self):
        original = self.helpers.build_order

        def changed_stock(*args):
            result = original(*args)
            self.Product.query.filter_by(id=2).update({"stock": 0})
            self.db.session.commit()
            return result

        with patch.object(self.helpers, "build_order", changed_stock):
            _, error = self.helpers.place_order(self.data, {1: 1, 2: 1})
        self.assertTrue(error)
        self.assertEqual(self.stock(), 2)
        self.assertEqual(self.Order.query.count(), 0)

    def test_failed_commit_rolls_back_stock(self):
        with patch.object(self.db.session, "commit", side_effect=RuntimeError("test failure")):
            with self.assertLogs(self.app.logger, level="ERROR"):
                _, error = self.helpers.place_order(self.data, {1: 1})
        self.assertTrue(error)
        self.assertEqual(self.stock(), 2)
        self.assertEqual(self.Order.query.count(), 0)

    def test_cancel_repeat_and_reopen(self):
        order = self.buy()
        for status, stock in [("cancelled", 2), ("cancelled", 2), ("new", 1),
                              ("in_work", 1), ("cancelled", 2)]:
            response = self.client.post(f"/admin/orders/{order.id}/status", data={"status": status})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(self.stock(), stock)
            self.assertEqual(self.db.session.get(self.Order, order.id).status, status)

    def test_concurrent_cancellations_return_stock_once(self):
        order_id = self.buy().id
        barrier = threading.Barrier(2)

        def cancel():
            with self.app.app_context():
                order = self.db.session.get(self.Order, order_id)
                barrier.wait(timeout=10)
                return self.helpers.change_order_status(order, "cancelled")

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: cancel(), range(2)))
        self.assertEqual(sum(error == "" for error in results), 1, results)
        self.assertEqual(self.stock(), 2)

    def test_reopen_without_stock_keeps_cancelled(self):
        order = self.buy({1: 1, 2: 1})
        self.assertEqual(self.helpers.change_order_status(order, "cancelled"), "")
        self.db.session.get(self.Product, 2).stock = 0
        self.db.session.commit()
        self.assertTrue(self.helpers.change_order_status(order, "new"))
        self.assertEqual(self.stock(), 2)
        self.assertEqual(order.status, "cancelled")
        self.assertTrue(order.stock_released)

    def test_cancel_uses_id_after_rename(self):
        order = self.buy()
        self.db.session.get(self.Product, 1).name = "Новое название"
        self.db.session.commit()
        self.assertEqual(self.helpers.change_order_status(order, "cancelled"), "")
        self.assertEqual(self.stock(), 2)

    def test_legacy_orders_migrate_when_cancelled(self):
        order = self.buy()
        order.items = None
        # Old cancelled orders still have reserved stock; cancel again repairs them.
        order.status = "cancelled"
        self.db.session.commit()
        self.assertEqual(self.helpers.change_order_status(order, "cancelled"), "")
        self.assertEqual(order.items[0]["product_id"], 1)
        self.assertIsNone(order.items[0]["price"])
        self.assertEqual(self.stock(), 2)
        self.assertEqual(self.helpers.change_order_status(order, "cancelled"), "")
        self.assertEqual(self.stock(), 2)

    def test_legacy_ambiguous_name_is_not_guessed(self):
        order = self.buy()
        order.items = None
        self.db.session.add(self.Product(name="Розы", price=100, stock=10, img="x"))
        self.db.session.commit()
        self.assertTrue(self.helpers.change_order_status(order, "cancelled"))
        self.assertEqual(self.stock(), 1)
        self.assertEqual(order.status, "new")

    def test_legacy_name_containing_separator_is_not_split_into_other_products(self):
        order = self.buy()
        order.items = None
        order.flower = "Розы (1 шт), Тюльпаны (1 шт)"
        self.db.session.add(self.Product(
            name="Розы (1 шт), Тюльпаны", price=100, stock=10, img="x"
        ))
        self.db.session.commit()
        self.assertTrue(self.helpers.change_order_status(order, "cancelled"))
        self.assertEqual(self.stock(), 1)
        self.assertEqual(self.stock(2), 3)

    def test_deleted_product_does_not_cause_partial_stock_return(self):
        order = self.buy({1: 1, 2: 1})
        self.db.session.delete(self.db.session.get(self.Product, 2))
        self.db.session.commit()
        self.assertTrue(self.helpers.change_order_status(order, "cancelled"))
        self.assertEqual(self.stock(), 1)
        self.assertEqual(order.status, "new")

    def test_product_with_order_cannot_be_deleted_or_have_its_id_reused(self):
        self.buy({2: 1})
        response = self.client.post("/admin/products/2/delete", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(self.db.session.get(self.Product, 2))
        self.assertIn("Товар связан с заказом", response.get_data(as_text=True))

    def test_product_without_orders_can_be_deleted(self):
        self.assertEqual(self.client.post("/admin/products/2/delete").status_code, 302)
        self.db.session.expire_all()
        self.assertIsNone(self.db.session.get(self.Product, 2))

    def test_legacy_missing_or_malformed_items_leave_order_unchanged(self):
        order = self.buy()
        order.items = None
        for flower in ["Неизвестный (1 шт)", "Розы (1e309 шт)", "Розы (1 шт), мусор"]:
            order.flower = flower
            self.db.session.commit()
            self.assertTrue(self.helpers.change_order_status(order, "cancelled"))
            self.assertEqual(self.stock(), 1)
            self.assertEqual(order.status, "new")

    def test_cancellation_limit_failure_rolls_back_all_returns(self):
        order = self.buy({1: 1, 2: 1})
        self.db.session.get(self.Product, 2).stock = 10000
        self.db.session.commit()
        self.assertTrue(self.helpers.change_order_status(order, "cancelled"))
        self.assertEqual(self.stock(), 1)
        self.assertEqual(order.status, "new")
        self.assertFalse(order.stock_released)

    def test_revenue_only_counts_delivered_orders(self):
        from flask import template_rendered
        for status, total in [("new", 100), ("in_work", 200), ("cancelled", 300), ("delivered", 400)]:
            self.db.session.add(self.Order(date="2026-10-23", flower="Розы (1 шт)",
                                          total=total, status=status, **self.data))
        self.db.session.commit()
        contexts = []

        def capture(sender, template, context, **kwargs):
            contexts.append(context)

        with template_rendered.connected_to(capture, self.app):
            self.assertEqual(self.client.get("/admin").status_code, 200)
        self.assertEqual(contexts[0]["stats"]["revenue"], 400)

    def test_schema_upgrade_preserves_old_orders(self):
        from sqlalchemy import text
        from models import ensure_schema
        order_id = self.buy().id
        self.db.session.remove()
        with self.db.engine.begin() as connection:
            connection.execute(text('ALTER TABLE "order" DROP COLUMN items'))
            connection.execute(text('ALTER TABLE "order" DROP COLUMN stock_released'))
        ensure_schema()
        ensure_schema()
        order = self.db.session.get(self.Order, order_id)
        self.assertEqual(order.total, 2500)
        self.assertIsNone(order.items)
        self.assertFalse(order.stock_released)
        self.assertEqual(self.helpers.change_order_status(order, "cancelled"), "")
        self.assertEqual(self.stock(), 2)


if __name__ == "__main__":
    unittest.main()
