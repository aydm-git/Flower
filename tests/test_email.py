import os
import smtplib
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class FakeSMTP:
    """Подменяет smtplib.SMTP / SMTP_SSL и запоминает отправленные письма."""

    sent = []
    fail_with = None
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.tls, self.login_args = host, port, False, None
        FakeSMTP.instances.append(self)
        if isinstance(FakeSMTP.fail_with, OSError):
            raise FakeSMTP.fail_with

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.tls = True

    def login(self, user, password):
        self.login_args = (user, password)
        if isinstance(FakeSMTP.fail_with, smtplib.SMTPAuthenticationError):
            raise FakeSMTP.fail_with

    def send_message(self, message):
        if isinstance(FakeSMTP.fail_with, smtplib.SMTPRecipientsRefused):
            raise FakeSMTP.fail_with
        FakeSMTP.sent.append(message)
        return {}


class EmailTests(unittest.TestCase):
    PASSWORD = "app-pass-SECRET"

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
        cls.addClassCleanup(sys.path.pop, 0)
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        with patch.dict(os.environ, {"SECRET_KEY": "email-tests-only"}):
            from config import Config
            with patch.multiple(Config, SQLALCHEMY_DATABASE_URI="sqlite://",
                                UPLOAD_DIR=cls.temporary.name):
                from app import create_app

            class TestConfig(Config):
                TESTING = True
                WTF_CSRF_ENABLED = False
                SQLALCHEMY_DATABASE_URI = "sqlite:///" + str(
                    Path(cls.temporary.name) / "email.db"
                ).replace("\\", "/")
                UPLOAD_DIR = cls.temporary.name
                TELEGRAM_BOT_TOKEN = ""
                TELEGRAM_CHAT_ID = ""
                SMTP_HOST = "smtp.yandex.ru"
                SMTP_PORT = 465
                SMTP_SECURITY = ""
                SMTP_USER = "shop@yandex.ru"
                SMTP_PASSWORD = cls.PASSWORD
                SMTP_FROM = ""
                SHOP_TIMEZONE = "Asia/Novosibirsk"

            cls.app = create_app(TestConfig)
        import helpers
        import mailer
        import models
        cls.helpers, cls.mailer, cls.models, cls.db = helpers, mailer, models, models.db

        def close_database():
            with cls.app.app_context():
                models.db.session.remove()
                models.db.engine.dispose()

        cls.addClassCleanup(close_database)

    def setUp(self):
        self.context = self.app.test_request_context("/")
        self.context.push()
        self.addCleanup(self.context.pop)
        self.db.drop_all()
        self.db.create_all()
        self.db.session.add(self.models.Product(id=1, name="Розы & Co", price=2500, stock=5,
                                                img="images/roses.jpeg"))
        self.db.session.commit()
        self.data = dict(name="Анна <b>", phone="+79991234567", is_pickup=False,
                         address="ул. Ленина, 1", delivery_date="2026-10-26",
                         delivery_time="10:00–12:00", card_text="С днём рождения!", comment="")
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["logged_in"] = True
        FakeSMTP.sent, FakeSMTP.instances, FakeSMTP.fail_with = [], [], None
        for name in ("SMTP", "SMTP_SSL"):
            patcher = patch.object(self.mailer.smtplib, name, FakeSMTP)
            patcher.start()
            self.addCleanup(patcher.stop)
        background = patch.object(self.mailer, "start_background",
                                  side_effect=lambda target, *args: target(*args))
        background.start()
        self.addCleanup(background.stop)
        sleeper = patch.object(self.mailer.time, "sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def order(self):
        self.db.session.expire_all()
        return self.models.Order.query.one()

    def place(self):
        total, error = self.helpers.place_order(self.data, {1: 1})
        self.assertEqual(error, "")

    def test_admin_sets_recipients_in_settings(self):
        response = self.client.post("/admin/settings",
                                    data={"order_emails": "boss@mail.ru; Boss@mail.ru, team@yandex.ru"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.mailer.get_order_emails(), ["boss@mail.ru", "team@yandex.ru"])
        page = self.client.get("/admin/settings").get_data(as_text=True)
        self.assertIn("boss@mail.ru, team@yandex.ru", page)
        self.assertIn("smtp.yandex.ru:465", page)
        self.assertNotIn(self.PASSWORD, page)

    def test_invalid_recipient_is_rejected(self):
        response = self.client.post("/admin/settings",
                                    data={"order_emails": "ok@mail.ru, bad\r\nBcc: x@evil.ru"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Некорректный адрес", response.get_data(as_text=True))
        self.assertEqual(self.mailer.get_order_emails(), [])

    def test_order_is_emailed_to_admin_addresses(self):
        self.mailer.save_order_emails("boss@mail.ru, team@yandex.ru")
        self.place()
        self.assertEqual(len(FakeSMTP.sent), 1)
        message = FakeSMTP.sent[0]
        self.assertEqual(message["To"], "boss@mail.ru, team@yandex.ru")
        self.assertIn("shop@yandex.ru", message["From"])
        self.assertIn("Новый заказ №1", message["Subject"])
        text = message.get_body(("plain",)).get_content()
        self.assertIn("Анна <b>", text)
        self.assertIn("ул. Ленина, 1", text)
        self.assertIn("/admin?q=", text)
        html = message.get_body(("html",)).get_content()
        self.assertIn("Анна &lt;b&gt;", html)
        self.assertIn("Розы &amp; Co", html)
        self.assertEqual(FakeSMTP.instances[0].login_args, ("shop@yandex.ru", self.PASSWORD))
        self.assertEqual(self.order().email_status, "sent")

    def test_starttls_is_used_for_port_587(self):
        self.mailer.save_order_emails("boss@mail.ru")
        self.app.config["SMTP_PORT"] = 587
        self.addCleanup(self.app.config.update, SMTP_PORT=465)
        self.assertEqual(self.mailer.send_test_email(), "")
        self.assertTrue(FakeSMTP.instances[0].tls)

    def test_no_recipients_means_no_email(self):
        self.place()
        self.assertEqual(FakeSMTP.sent, [])
        self.assertEqual(self.order().email_status, "off")

    def test_wrong_password_is_explained_and_not_retried(self):
        self.mailer.save_order_emails("boss@mail.ru")
        FakeSMTP.fail_with = smtplib.SMTPAuthenticationError(535, b"Invalid user or password")
        with self.assertLogs(self.app.logger, level="ERROR") as logs:
            self.place()
        order = self.order()
        self.assertEqual(order.email_status, "failed")
        self.assertIn("пароль приложения", order.email_error)
        self.assertEqual(len(FakeSMTP.instances), 1)
        self.assertNotIn(self.PASSWORD, order.email_error + "\n".join(logs.output))

    def test_unreachable_server_is_retried(self):
        self.mailer.save_order_emails("boss@mail.ru")
        FakeSMTP.fail_with = TimeoutError("timed out")
        with self.assertLogs(self.app.logger, level="ERROR"):
            self.place()
        self.assertEqual(len(FakeSMTP.instances), 3)
        self.assertIn("Нет связи с почтовым сервером smtp.yandex.ru:465", self.order().email_error)

    def test_unknown_host_is_explained(self):
        self.mailer.save_order_emails("boss@mail.ru")
        FakeSMTP.fail_with = socket.gaierror("Name or service not known")
        with self.assertLogs(self.app.logger, level="ERROR"):
            self.place()
        self.assertIn("не найден", self.order().email_error)

    def test_failed_email_shown_in_admin_and_resent(self):
        self.mailer.save_order_emails("boss@mail.ru")
        FakeSMTP.fail_with = ConnectionRefusedError("refused")
        with self.assertLogs(self.app.logger, level="ERROR"):
            self.place()
        self.db.session.expire_all()
        page = self.client.get("/admin").get_data(as_text=True)
        self.assertIn("Почта: не отправлено", page)
        self.assertIn("не отправлены на почту", page)
        FakeSMTP.fail_with = None
        response = self.client.post(f"/admin/orders/{self.order().id}/email", follow_redirects=True)
        self.assertIn("отправлен на почту", response.get_data(as_text=True))
        self.assertEqual(self.order().email_status, "sent")

    def test_test_email_button(self):
        response = self.client.post("/admin/settings/test-email", follow_redirects=True)
        self.assertIn("Сначала укажите", response.get_data(as_text=True))
        self.mailer.save_order_emails("boss@mail.ru")
        response = self.client.post("/admin/settings/test-email", follow_redirects=True)
        self.assertIn("Тестовое письмо отправлено", response.get_data(as_text=True))
        self.assertEqual(FakeSMTP.sent[0]["To"], "boss@mail.ru")

    def test_missing_smtp_settings_are_reported(self):
        self.app.config["SMTP_HOST"] = ""
        self.addCleanup(self.app.config.update, SMTP_HOST="smtp.yandex.ru")
        self.mailer.save_order_emails("boss@mail.ru")
        self.assertFalse(self.mailer.email_enabled())
        page = self.client.get("/admin/settings").get_data(as_text=True)
        self.assertIn("Не задан SMTP_HOST", page)


    def test_gmail_app_password_spaces_are_removed(self):
        self.app.config["SMTP_PASSWORD"] = "abcd efgh ijkl mnop"
        self.addCleanup(self.app.config.update, SMTP_PASSWORD=self.PASSWORD)
        self.assertEqual(self.mailer.smtp_settings()["password"], "abcdefghijklmnop")
        self.app.config["SMTP_PASSWORD"] = "pass with spaces 1"
        self.assertEqual(self.mailer.smtp_settings()["password"], "pass with spaces 1")

    def test_settings_explain_env_example_mistake(self):
        root = Path(self.temporary.name) / "project"
        root.mkdir(exist_ok=True)
        (root / ".env.example").write_text("SMTP_HOST=smtp.gmail.com\n", encoding="utf-8")
        self.app.config.update(SMTP_HOST="", ENV_FILE=str(root / ".env"))
        self.addCleanup(self.app.config.update, SMTP_HOST="smtp.yandex.ru", ENV_FILE="")
        page = self.client.get("/admin/settings").get_data(as_text=True)
        self.assertIn("не найден", page)
        self.assertIn("вписаны в .env.example", page)
        self.assertIn(str(root / ".env"), page)

if __name__ == "__main__":
    unittest.main()
