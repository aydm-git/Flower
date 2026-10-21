import os

from flask import Flask, flash, redirect, session, url_for
from flask_wtf.csrf import CSRFError, CSRFProtect
from werkzeug.exceptions import RequestEntityTooLarge

from config import BASE_DIR, Config
from models import db, ensure_schema
from routes import admin_bp, auth_bp, shop_bp

csrf = CSRFProtect()


def create_app(config_class=Config):
    app = Flask(
        __name__,
        template_folder=os.path.join(BASE_DIR, "templates"),
        static_folder=os.path.join(BASE_DIR, "static"),
        instance_path=os.path.join(BASE_DIR, "instance"),
    )
    app.config.from_object(config_class)
    os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)

    db.init_app(app)
    csrf.init_app(app)

    app.register_blueprint(shop_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)

    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        flash("Сессия устарела. Обновите страницу и попробуйте ещё раз.", "error")
        return redirect(url_for("admin.order_list" if session.get("logged_in") else "shop.home"))

    @app.errorhandler(RequestEntityTooLarge)
    def handle_too_large(e):
        flash("Файл слишком большой (максимум 5 МБ)", "error")
        return redirect(url_for("admin.product_list" if session.get("logged_in") else "shop.home"))

    with app.app_context():
        ensure_schema()

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
