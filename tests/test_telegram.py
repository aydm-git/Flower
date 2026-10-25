import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class TelegramTests(unittest.TestCase):
    TOKEN = "123456:SECRET-TOKEN"

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
        cls.addClassCleanup(sys.path.pop, 0)
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        with patch.dict(os.environ, {"SECRET_KEY": "telegram-tests-only"}):
            from config import Config
            with patch.multiple(Config, SQLALCHEMY_DATABASE_URI="sqlite://",
                                UPLOAD_DIR=cls.temporary.name):
                from app import create_app

            class TestConfig(Config):
                TESTING = True
                WTF_CSRF_ENABLED = False
                SQLALCHEMY_DATABASE_URI = "sqlite:///" + str(
                    Path(cls.temporary.name) / "telegram.db"
                ).replace("\\", "/")
                UPLOAD_DIR = cls.temporary.name
                TELEGRAM_BOT_TOKEN = cls.TOKEN
                TELEGRAM_CHAT_ID = "-1001, 42"
                SHOP_TIMEZONE = "Asia/Novosibirsk"

            cls.app = create_app(TestConfig)
        import helpers
        import models
        cls.helpers, cls.models, cls.db = helpers, models, models.db

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
        self.db.session.add(self.models.Product(id=1, name="Розы", price=2500, stock=5,
                                                img="images/roses.jpeg"))
        self.db.session.commit()
        self.data = dict(name="Покупатель", phone="+79991234567", is_pickup=True, address="",
                         delivery_date="2026-10-26", delivery_time="10:00–12:00",
                         card_text="", comment="")
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["logged_in"] = True
        # Отправка синхронно и без пауз между повторами.
        patcher = patch.object(self.helpers, "start_background",
                               side_effect=lambda target, *args: target(*args))
        patcher.start()
        self.addCleanup(patcher.stop)
        sleeper = patch.object(self.helpers.time, "sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)

    def order(self):
        self.db.session.expire_all()
        return self.models.Order.query.one()

    def place(self, post):
        with patch.object(self.helpers.requests, "post", side_effect=post) as mocked:
            total, error = self.helpers.place_order(self.data, {1: 1})
        self.assertEqual(error, "")
        return mocked

    def test_success_marks_order_sent(self):
        mocked = self.place(lambda *a, **k: FakeResponse(200, {"ok": True, "result": {}}))
        self.assertEqual(mocked.call_count, 2)  # два чата
        self.assertEqual(self.order().notify_status, "sent")

    def test_blocked_api_is_retried_and_reported_without_token(self):
        import requests
        with self.assertLogs(self.app.logger, level="ERROR") as logs:
            mocked = self.place(requests.exceptions.ConnectTimeout(
                f"HTTPSConnectionPool: /bot{self.TOKEN}/sendMessage timed out"))
        self.assertEqual(mocked.call_count, 6)  # 3 попытки × 2 чата
        order = self.order()
        self.assertEqual(order.notify_status, "failed")
        self.assertIn("api.telegram.org", order.notify_error)
        self.assertIn("TELEGRAM_PROXY", order.notify_error)
        self.assertNotIn("SECRET-TOKEN", order.notify_error)
        self.assertNotIn("SECRET-TOKEN", "\n".join(logs.output))

    def test_wrong_chat_is_not_retried_and_explained(self):
        def post(url, data=None, **kwargs):
            if data["chat_id"] == "42":
                return FakeResponse(400, {"ok": False, "description": "Bad Request: chat not found"})
            return FakeResponse(200, {"ok": True, "result": {}})

        with self.assertLogs(self.app.logger, level="ERROR"):
            mocked = self.place(post)
        self.assertEqual(mocked.call_count, 2)
        order = self.order()
        self.assertEqual(order.notify_status, "failed")
        self.assertIn("42", order.notify_error)
        self.assertIn("Чат не найден", order.notify_error)

    def test_supergroup_migration_names_new_chat_id(self):
        body = {"ok": False, "description": "Bad Request: group chat was upgraded to a supergroup chat",
                "parameters": {"migrate_to_chat_id": -1009876}}
        with self.assertLogs(self.app.logger, level="ERROR"):
            self.place(lambda *a, **k: FakeResponse(400, body))
        self.assertIn("-1009876", self.order().notify_error)

    def test_admin_sees_failure_and_can_resend(self):
        import requests
        with self.assertLogs(self.app.logger, level="ERROR"):
            self.place(requests.exceptions.ConnectionError("reset"))
        self.db.session.expire_all()  # статус записан фоновой отправкой в другой сессии
        page = self.client.get("/admin").get_data(as_text=True)
        self.assertIn("Telegram: не отправлено", page)
        self.assertIn("не отправлены в Telegram", page)
        order_id = self.order().id
        with patch.object(self.helpers.requests, "post",
                          return_value=FakeResponse(200, {"ok": True, "result": {}})):
            response = self.client.post(f"/admin/orders/{order_id}/notify", follow_redirects=True)
        self.assertIn("отправлен в Telegram", response.get_data(as_text=True))
        self.assertEqual(self.order().notify_status, "sent")

    def test_admin_check_reports_bot_and_chats(self):
        def post(url, data=None, **kwargs):
            if url.endswith("/getMe"):
                return FakeResponse(200, {"ok": True, "result": {"username": "flower_bot"}})
            return FakeResponse(403, {"ok": False, "description": "Forbidden: bot was kicked"})

        with patch.object(self.helpers.requests, "post", side_effect=post):
            response = self.client.post("/admin/telegram/check", follow_redirects=True)
        page = response.get_data(as_text=True)
        self.assertIn("@flower_bot", page)
        self.assertIn("не может писать", page)
        self.assertNotIn("SECRET-TOKEN", page)

    def test_socks_proxy_without_pysocks_is_explained(self):
        import requests
        error = requests.exceptions.InvalidSchema("Missing dependencies for SOCKS support.")
        with patch.dict(os.environ, {"TELEGRAM_PROXY": "socks5h://127.0.0.1:1080"}), \
                self.assertLogs(self.app.logger, level="ERROR"):
            mocked = self.place(error)
        self.assertEqual(mocked.call_count, 2)
        self.assertIn("PySocks", self.order().notify_error)

    def test_unexpected_error_still_marks_order_failed(self):
        with self.assertLogs(self.app.logger, level="ERROR"):
            self.place(RuntimeError("boom"))
        order = self.order()
        self.assertEqual(order.notify_status, "failed")
        self.assertIn("RuntimeError", order.notify_error)

    def test_proxy_error_names_the_right_proxy(self):
        import requests
        error = requests.exceptions.ProxyError("403")
        with patch.object(self.helpers.requests, "post", side_effect=error):
            _, message, _ = self.helpers.telegram_call(self.TOKEN, "getMe", {})
            self.assertIn("HTTPS_PROXY", message)
            with patch.dict(os.environ, {"TELEGRAM_PROXY": "http://proxy:3128"}):
                _, message, _ = self.helpers.telegram_call(self.TOKEN, "getMe", {})
        self.assertIn("через прокси http://proxy:3128", message)

    def test_timeout_through_proxy_blames_proxy_and_hides_credentials(self):
        import requests
        with patch.dict(os.environ, {"TELEGRAM_PROXY": "socks5h://user:pa55@10.0.0.5:1080"}), \
                patch.object(self.helpers.requests, "post",
                             side_effect=requests.exceptions.ConnectionError("SOCKS failed")):
            _, message, retryable = self.helpers.telegram_call(self.TOKEN, "getMe", {})
        self.assertIn("socks5h://10.0.0.5:1080", message)
        self.assertNotIn("pa55", message)
        self.assertNotIn("укажите прокси", message)
        self.assertTrue(retryable)

    def test_check_reports_proxy_that_is_not_running(self):
        import socket
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        import requests
        with patch.dict(os.environ, {"TELEGRAM_PROXY": f"socks5h://127.0.0.1:{port}"}), \
                patch.object(self.helpers.requests, "post",
                             side_effect=requests.exceptions.ConnectionError("refused")):
            results = self.helpers.check_telegram()
        text = " ".join(message for _, message in results)
        self.assertFalse(results[0][0])
        self.assertIn(f"127.0.0.1:{port}", text)
        self.assertIn("не отвечает", text)

    def test_check_reports_proxy_without_access_to_telegram(self):
        import socket
        import requests
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            port = server.getsockname()[1]
            with patch.dict(os.environ, {"TELEGRAM_PROXY": f"socks5h://127.0.0.1:{port}"}), \
                    patch.object(self.helpers.requests, "post",
                                 side_effect=requests.exceptions.ConnectTimeout("timeout")):
                results = self.helpers.check_telegram()
        text = " ".join(message for _, message in results)
        self.assertIn("отвечает", text)
        self.assertIn("через него нет связи с api.telegram.org", text)

    def test_disabled_integration_is_marked(self):
        self.app.config["TELEGRAM_BOT_TOKEN"] = ""
        self.addCleanup(self.app.config.update, TELEGRAM_BOT_TOKEN=self.TOKEN)
        with patch.object(self.helpers.requests, "post") as mocked:
            self.helpers.place_order(self.data, {1: 1})
        mocked.assert_not_called()
        self.assertEqual(self.order().notify_status, "off")


if __name__ == "__main__":
    unittest.main()
