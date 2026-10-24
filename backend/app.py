import os
from zoneinfo import ZoneInfo

from flask import Flask, flash, redirect, render_template, session, url_for
from flask_wtf.csrf import CSRFError, CSRFProtect
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix

from config import BASE_DIR, Config
from models import db, ensure_schema
from routes import admin_bp, auth_bp, shop_bp

csrf = CSRFProtect()

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; "
    "frame-ancestors 'none'"
)


def create_app(config_class=Config):
    app = Flask(
        __name__,
        template_folder=os.path.join(BASE_DIR, "templates"),
        static_folder=os.path.join(BASE_DIR, "static"),
        instance_path=os.path.join(BASE_DIR, "instance"),
    )
    app.config.from_object(config_class)
    ZoneInfo(app.config["SHOP_TIMEZONE"])
    os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)

    proxies = app.config.get("TRUSTED_PROXIES", 0)
    if proxies:
        # Реальный IP клиента берётся из X-Forwarded-For только от доверенных прокси.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=proxies, x_proto=proxies)

    db.init_app(app)
    csrf.init_app(app)

    app.register_blueprint(shop_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        return response

    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        flash("Сессия устарела. Обновите страницу и попробуйте ещё раз.", "error")
        return redirect(url_for("admin.order_list" if session.get("logged_in") else "shop.home"))

    @app.errorhandler(RequestEntityTooLarge)
    def handle_too_large(e):
        flash("Файл слишком большой (максимум 5 МБ)", "error")
        return redirect(url_for("admin.product_list" if session.get("logged_in") else "shop.home"))

    @app.errorhandler(404)
    def handle_not_found(e):
        return render_template(
            "error.html", title="Страница не найдена",
            message="Возможно, ссылка устарела или товар больше не продаётся.",
        ), 404

    @app.errorhandler(500)
    def handle_server_error(e):
        # Flask уже записал трассировку в журнал; освобождаем сессию после сбоя.
        db.session.rollback()
        return render_template(
            "error.html", title="Что-то пошло не так",
            message="Мы уже знаем о проблеме. Обновите страницу или попробуйте чуть позже.",
        ), 500

    with app.app_context():
        ensure_schema()

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
