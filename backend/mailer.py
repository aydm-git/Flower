"""Отправка заказов на почту, которую администратор указывает в админке.

Адреса получателей хранятся в базе (таблица app_meta), а данные почтового ящика
отправителя (SMTP) — в .env, как и другие секреты.
"""
import html
import os
import re
import smtplib
import socket
import ssl
import threading
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from flask import current_app
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from models import AppMeta, Order, db

RECIPIENTS_KEY = "order_emails"
MAX_RECIPIENTS = 10
EMAIL_RETRY_DELAYS = (10, 60)  # паузы перед 2-й и 3-й попыткой, секунды
SMTP_TIMEOUT = 20
EMAIL_RE = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}"
)


# --- Получатели (задаёт администратор) ---

def parse_recipients(raw: str):
    """Разбирает строку адресов. Возвращает (список, ошибка)."""
    parts = [p.strip() for p in re.split(r"[,;\s]+", raw or "") if p.strip()]
    emails = []
    for part in parts:
        if len(part) > 254 or not EMAIL_RE.fullmatch(part):
            return [], f"Некорректный адрес: «{part[:60]}». Пример: shop@yandex.ru"
        if part.lower() not in (e.lower() for e in emails):
            emails.append(part)
    if len(emails) > MAX_RECIPIENTS:
        return [], f"Можно указать не больше {MAX_RECIPIENTS} адресов"
    return emails, ""


def get_order_emails() -> list:
    row = db.session.get(AppMeta, RECIPIENTS_KEY)
    return parse_recipients(row.value)[0] if row else []


def save_order_emails(raw: str):
    """Сохраняет адреса из формы настроек. Возвращает (список, ошибка)."""
    emails, error = parse_recipients(raw)
    if error:
        return [], error
    value = ", ".join(emails)
    row = db.session.get(AppMeta, RECIPIENTS_KEY)
    if row is None:
        db.session.add(AppMeta(key=RECIPIENTS_KEY, value=value))
    else:
        row.value = value
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        db.session.execute(
            update(AppMeta).where(AppMeta.key == RECIPIENTS_KEY).values(value=value)
        )
        db.session.commit()
    return emails, ""


# --- Настройки SMTP (из .env) ---

def normalize_password(password: str) -> str:
    """Пароль приложения Google показывается как «abcd efgh ijkl mnop» — убираем пробелы."""
    password = password.strip()
    if re.fullmatch(r"(?:[A-Za-z]{4} ){3}[A-Za-z]{4}", password):
        return password.replace(" ", "")
    return password


def env_hints(settings: dict) -> list:
    """Подсказки, если настройки почты, похоже, записаны не в тот файл."""
    env_file = current_app.config.get("ENV_FILE", "")
    hints = []
    if env_file and not os.path.exists(env_file):
        hints.append(
            f"Файл {env_file} не найден, поэтому сайт не видит настроек. "
            "Скопируйте .env.example в .env и заполните его"
        )
    if not settings["host"] and env_file:
        example = os.path.join(os.path.dirname(env_file), ".env.example")
        try:
            with open(example, encoding="utf-8") as fh:
                filled = any(re.match(r"\s*SMTP_HOST\s*=\s*\S", line) for line in fh)
        except OSError:
            filled = False
        if filled:
            hints.append(
                "Настройки почты вписаны в .env.example — этот файл сайт не читает, это только "
                f"образец, и он попадает в Git. Перенесите их в {env_file}, очистите значения в "
                ".env.example и перезапустите сайт"
            )
    return hints


def smtp_settings() -> dict:
    cfg = current_app.config
    port = cfg.get("SMTP_PORT") or 465
    security = (cfg.get("SMTP_SECURITY") or "").strip().lower()
    if security not in {"ssl", "starttls", "none"}:
        security = "ssl" if port == 465 else "starttls"
    user = (cfg.get("SMTP_USER") or "").strip()
    return {
        "host": (cfg.get("SMTP_HOST") or "").strip(),
        "port": port,
        "security": security,
        "user": user,
        "password": normalize_password(cfg.get("SMTP_PASSWORD") or ""),
        "sender": (cfg.get("SMTP_FROM") or "").strip() or user,
    }


def smtp_problem(settings: dict) -> str:
    """Чего не хватает в .env для отправки ('' — всё задано)."""
    if not settings["host"]:
        return "Не задан SMTP_HOST в .env (например, smtp.yandex.ru)"
    if not settings["sender"]:
        return "Не задан SMTP_USER (или SMTP_FROM) в .env — адрес, с которого уходят письма"
    if settings["user"] and not settings["password"]:
        return "Не задан SMTP_PASSWORD в .env (для Яндекса, Mail.ru и Gmail — пароль приложения)"
    return ""


def email_enabled() -> bool:
    return not smtp_problem(smtp_settings()) and bool(get_order_emails())


# --- Отправка ---

def _describe_smtp_error(exc: Exception, settings: dict):
    """Понятное описание ошибки и признак «стоит повторить». Пароль в текст не попадает."""
    server = f"{settings['host']}:{settings['port']}"
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return (
            "Почтовый сервер не принял логин или пароль SMTP. Для Яндекса, Mail.ru и Gmail "
            "нужен специальный пароль приложения, а не обычный пароль от почты"
        ), False
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        refused = ", ".join(exc.recipients) if exc.recipients else "все адреса"
        return f"Почтовый сервер отклонил адреса получателей: {refused}", False
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return (
            "Почтовый сервер отклонил адрес отправителя: SMTP_FROM обычно должен совпадать "
            "с SMTP_USER"
        ), False
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return (
            f"Сервер {server} не поддерживает выбранный способ шифрования или входа — "
            "проверьте SMTP_SECURITY (465 — ssl, 587 — starttls)"
        ), False
    if isinstance(exc, smtplib.SMTPResponseException):
        temporary = 400 <= exc.smtp_code < 500
        detail = exc.smtp_error.decode("utf-8", "replace") if isinstance(
            exc.smtp_error, bytes) else str(exc.smtp_error)
        return f"Почтовый сервер ответил ошибкой {exc.smtp_code}: {detail[:150]}", temporary
    if isinstance(exc, ssl.SSLError):
        return (
            f"Ошибка шифрования при подключении к {server}: проверьте SMTP_PORT и SMTP_SECURITY "
            "(порт 465 — ssl, порт 587 — starttls)"
        ), False
    if isinstance(exc, socket.gaierror):
        return f"Почтовый сервер {settings['host']} не найден: проверьте SMTP_HOST", False
    if isinstance(exc, (smtplib.SMTPServerDisconnected, TimeoutError, OSError)):
        return (
            f"Нет связи с почтовым сервером {server} (тайм-аут или соединение отклонено): "
            "проверьте SMTP_HOST и SMTP_PORT; хостинг может блокировать исходящую почту"
        ), True
    return f"Ошибка отправки письма ({type(exc).__name__})", True


def smtp_send(settings: dict, recipients: list, subject: str, text: str, html_body: str = ""):
    """Одна попытка отправки. Возвращает (ошибка, стоит_ли_повторять)."""
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr(("Чечектер", settings["sender"]))
    message["To"] = ", ".join(recipients)
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=settings["sender"].rpartition("@")[2] or None)
    # quoted-printable вместо 8bit: письмо дойдёт и через серверы без поддержки 8BITMIME.
    message.set_content(text, cte="quoted-printable")
    if html_body:
        message.add_alternative(html_body, subtype="html")

    try:
        context = ssl.create_default_context()
        if settings["security"] == "ssl":
            server = smtplib.SMTP_SSL(settings["host"], settings["port"],
                                      timeout=SMTP_TIMEOUT, context=context)
        else:
            server = smtplib.SMTP(settings["host"], settings["port"], timeout=SMTP_TIMEOUT)
        with server:
            if settings["security"] == "starttls":
                server.starttls(context=context)
            if settings["user"]:
                server.login(settings["user"], settings["password"])
            refused = server.send_message(message)
    except Exception as exc:
        return _describe_smtp_error(exc, settings)
    if refused:
        return f"Почтовый сервер отклонил адреса получателей: {', '.join(refused)}", False
    return "", False


def send_with_retry(settings, recipients, subject, text, html_body="", retry_delays=None) -> str:
    if retry_delays is None:
        retry_delays = EMAIL_RETRY_DELAYS
    error = ""
    for delay in (0, *retry_delays):
        if delay:
            time.sleep(delay)
        error, retryable = smtp_send(settings, recipients, subject, text, html_body)
        if not error or not retryable:
            break
    return error


# --- Письмо о заказе ---

def order_email(order, admin_url: str = ""):
    """(тема, текст, html) письма о новом заказе."""
    subject = f"Новый заказ №{order.id} — {order.total} ₽"
    rows = [
        ("Имя", order.name),
        ("Телефон", order.phone),
        ("Букет", order.flower),
        ("Сумма", f"{order.total} ₽"),
        ("Получение", "Самовывоз" if order.is_pickup else f"Доставка: {order.address}"),
        ("Когда", f"{order.delivery_date_ru}, {order.delivery_time}"),
    ]
    if order.card_text:
        rows.append(("Открытка", order.card_text))
    if order.comment:
        rows.append(("Комментарий", order.comment))

    text_lines = [f"Новый заказ №{order.id}", ""]
    text_lines += [f"{label}: {value}" for label, value in rows]
    if admin_url:
        text_lines += ["", f"Открыть в админке: {admin_url}"]

    esc = html.escape
    html_rows = "".join(
        f'<tr><td style="padding:4px 12px 4px 0;color:#777;vertical-align:top">{esc(label)}</td>'
        f'<td style="padding:4px 0">{esc(str(value))}</td></tr>'
        for label, value in rows
    )
    link = (
        f'<p><a href="{esc(admin_url)}" style="color:#e91e8c">Открыть заказ в админке</a></p>'
        if admin_url else ""
    )
    html_body = (
        '<div style="font-family:Arial,sans-serif;font-size:15px;color:#222">'
        f'<h2 style="color:#e91e8c;margin:0 0 12px">Новый заказ №{order.id}</h2>'
        f"<table>{html_rows}</table>{link}</div>"
    )
    return subject, "\n".join(text_lines), html_body


def deliver_order_email(app, order_id: int, recipients: list, subject: str, text: str,
                        html_body: str, retry_delays=None) -> str:
    """Отправляет письмо и сохраняет результат в заказе. Возвращает текст ошибки или ''."""
    with app.app_context():
        settings = smtp_settings()
        try:
            error = send_with_retry(settings, recipients, subject, text, html_body, retry_delays)
        except Exception as exc:
            error = f"внутренняя ошибка отправки ({type(exc).__name__})"
            app.logger.exception("Почта: сбой при отправке заказа №%s", order_id)
        if error:
            app.logger.error("Почта: заказ №%s не отправлен: %s", order_id, error)
        try:
            db.session.execute(
                update(Order).where(Order.id == order_id).values(
                    email_status="failed" if error else "sent", email_error=error[:500],
                ),
                execution_options={"synchronize_session": False},
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.exception("Не удалось сохранить статус письма заказа №%s", order_id)
    return error


def start_background(target, *args) -> None:
    threading.Thread(target=target, args=args, daemon=True).start()


def notify_order_email(order, admin_url: str = "") -> None:
    if order.email_status != "pending":
        return
    recipients = get_order_emails()
    try:
        subject, text, html_body = order_email(order, admin_url)
    except Exception:
        current_app.logger.exception("Не удалось собрать письмо о заказе")
        return
    start_background(
        deliver_order_email, current_app._get_current_object(), order.id,
        recipients, subject, text, html_body,
    )


def resend_order_email(order, admin_url: str = "") -> str:
    """Повторная отправка из админки: сразу, одна попытка."""
    problem = smtp_problem(smtp_settings())
    if problem:
        return problem
    recipients = get_order_emails()
    if not recipients:
        return "Не указана почта для заказов: задайте её в админке на вкладке «Настройки»"
    subject, text, html_body = order_email(order, admin_url)
    return deliver_order_email(
        current_app._get_current_object(), order.id, recipients, subject, text, html_body,
        retry_delays=(),
    )


def send_test_email() -> str:
    """Тестовое письмо на указанные адреса. Возвращает текст ошибки или ''."""
    settings = smtp_settings()
    problem = smtp_problem(settings)
    if problem:
        return problem
    recipients = get_order_emails()
    if not recipients:
        return "Сначала укажите и сохраните почту для заказов"
    error, _ = smtp_send(
        settings, recipients, "Проверка уведомлений «Чечектер»",
        "Это тестовое письмо: на этот адрес будут приходить новые заказы.",
    )
    return error
