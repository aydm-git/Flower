import hmac

from flask import (
    Blueprint, current_app, flash, redirect, render_template, request, session, url_for,
)
from sqlalchemy import case, func, or_, select, text, update
from werkzeug.security import check_password_hash

import mailer
from config import MAX_STOCK
from helpers import (
    CLOSING_HOUR, OPENING_HOUR, POPULAR_LIMIT, STATUS_TRANSITIONS, by_popularity,
    change_order_status, check_telegram, clean_order_token, product_sales,
    order_admin_url, resend_order_notification,
    delete_image_file, delivery_date_limits, new_order_token, parse_iso_date, place_order,
    read_cart, register_failed_login, reset_attempts, save_image, shop_now, slot_options,
    too_many_attempts, validate_order_form, validate_product_form,
)
from models import STATUSES, Order, Product, db, to_ru_date

shop_bp = Blueprint("shop", __name__)
auth_bp = Blueprint("auth", __name__)
admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

SAVE_ERROR = "Не удалось сохранить изменения. Попробуйте ещё раз."


def commit_or_flash(error_message: str = SAVE_ERROR) -> bool:
    """Коммит с откатом и понятным сообщением вместо ошибки 500 (например, «database is locked»)."""
    try:
        db.session.commit()
        return True
    except Exception:
        current_app.logger.exception("Не удалось сохранить изменения в базе")
        db.session.rollback()
        flash(error_message, "error")
        return False


@shop_bp.route("/", methods=["GET", "POST"])
def home():
    error = ""
    form = {}

    if request.method == "POST":
        form = request.form
        cart, prices, cart_error = read_cart(form.get("cart", ""))

        data, error = validate_order_form(form, cart, cart_error)
        if not error:
            total, error = place_order(
                data, cart, prices, clean_order_token(form.get("order_token"))
            )
            if not error:
                kind = "Самовывоз" if data["is_pickup"] else "Доставка"
                when = f"{to_ru_date(data['delivery_date'])}, {data['delivery_time']}"
                flash(
                    f"Спасибо, {data['name']}! Заявка принята. {kind}: {when}. "
                    f"Сумма заказа: {total} ₽. Мы скоро свяжемся с вами.",
                    "order_success",
                )
                return redirect(url_for("shop.home", _anchor="order"))

    min_date, max_date = delivery_date_limits()
    # Если сегодня интервалов уже нет, дата сама переносится на ближайший доступный день.
    chosen = parse_iso_date(form.get("delivery_date", ""))
    date_value = chosen.isoformat() if chosen and min_date <= chosen.isoformat() <= max_date else min_date
    catalog = Product.query.filter_by(is_active=True).order_by(Product.id).all()
    products = by_popularity(catalog, product_sales())[:POPULAR_LIMIT]
    return (
        render_template(
            "index.html",
            products=products,
            catalog_size=len(catalog),
            error=error,
            form=form,
            min_date=min_date,
            max_date=max_date,
            date_value=date_value,
            order_token=new_order_token(),
            slots=slot_options(),
            shop_time=shop_now().isoformat(),
            opening_hour=OPENING_HOUR,
            closing_hour=CLOSING_HOUR,
        ),
        400 if error else 200,
    )


CATALOG_SORTS = {
    "popular": "По популярности",
    "cheap": "Сначала дешевле",
    "expensive": "Сначала дороже",
    "name": "По названию",
}


@shop_bp.route("/catalog")
def catalog():
    sort = request.args.get("sort", "popular")
    if sort not in CATALOG_SORTS:
        sort = "popular"
    products = Product.query.filter_by(is_active=True).order_by(Product.id).all()
    sales = product_sales()
    popular_ids = {
        p.id for p in by_popularity(products, sales)[:POPULAR_LIMIT] if sales.get(p.id)
    }
    if sort == "popular":
        products = by_popularity(products, sales)
    else:
        keys = {
            "cheap": lambda p: (p.stock < 1, p.price, p.id),
            "expensive": lambda p: (p.stock < 1, -p.price, p.id),
            "name": lambda p: (p.stock < 1, p.name.casefold(), p.id),
        }
        products = sorted(products, key=keys[sort])
    return render_template(
        "catalog.html",
        products=products,
        popular_ids=popular_ids,
        sort=sort,
        sorts=CATALOG_SORTS,
    )


@shop_bp.route("/product/<int:pid>")
def product_page(pid):
    item = Product.query.filter_by(id=pid, is_active=True).first_or_404()
    others = (
        Product.query.filter(Product.id != pid, Product.is_active.is_(True))
        .order_by(Product.id)
        .limit(3)
        .all()
    )
    return render_template("product.html", product=item, others=others)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if session.get("logged_in"):
        return redirect(url_for("admin.order_list"))

    if request.method == "POST":
        ip = request.remote_addr or "unknown"

        if too_many_attempts(ip):
            flash("Слишком много попыток. Попробуйте через 10 минут.", "error")
            return render_template("login.html"), 429

        admin_username = current_app.config["ADMIN_USERNAME"]
        admin_hash = current_app.config["ADMIN_PASSWORD_HASH"]
        username = request.form.get("username", "")
        password = request.form.get("password", "")

        if not admin_username or not admin_hash:
            flash("Вход не настроен: задайте ADMIN_USERNAME и ADMIN_PASSWORD_HASH в .env", "error")
        else:
            user_ok = hmac.compare_digest(username.encode(), admin_username.encode())
            pass_ok = check_password_hash(admin_hash, password)
            if user_ok and pass_ok:
                session.clear()
                session.permanent = True
                session["logged_in"] = True
                reset_attempts(ip)
                return redirect(url_for("admin.order_list"))
            register_failed_login(ip)
            flash("Неверный логин или пароль", "error")

    return render_template("login.html")


@auth_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("shop.home"))


@admin_bp.before_request
def require_login():
    if not session.get("logged_in"):
        return redirect(url_for("auth.login"))


def _back_to_orders():
    page = request.form.get("f_page", type=int)
    return redirect(
        url_for(
            "admin.order_list",
            status=request.form.get("f_status") or None,
            q=request.form.get("f_q") or None,
            page=page if page and page > 1 else None,
        )
    )


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


MONTHS = [
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
]


@admin_bp.route("")
def order_list():
    status = request.args.get("status", "")
    q = request.args.get("q", "").strip()[:100]
    page = min(max(request.args.get("page", 1, type=int), 1), 100_000)

    query = Order.query
    if status in STATUSES:
        query = query.filter_by(status=status)
    else:
        status = ""
    if q:
        # casefold() регистрируется в SQLite (models.py): поиск «иванов» найдёт «Иванов».
        pattern = _like_pattern(q.casefold())
        conditions = [
            func.casefold(column).like(pattern, escape="\\")
            for column in (Order.name, Order.phone, Order.flower, Order.address)
        ]
        number = q.lstrip("№#").strip()
        if number.isdigit() and len(number) <= 18:
            conditions.append(Order.id == int(number))
        query = query.filter(or_(*conditions))

    now = shop_now()
    delivered_total = db.session.query(func.coalesce(func.sum(Order.total), 0)).filter(
        Order.status == "delivered"
    )
    stats = {
        "count": Order.query.count(),
        "new": Order.query.filter_by(status="new").count(),
        "today": Order.query.filter(
            Order.delivery_date == now.date().isoformat(),
            Order.status.in_(["new", "in_work"]),
        ).count(),
        "revenue": delivered_total.scalar(),
        "revenue_month": delivered_total.filter(
            Order.date.like(now.strftime("%Y-%m") + "-%")
        ).scalar(),
        "month_name": MONTHS[now.month - 1],
        "notify_failed": Order.query.filter_by(notify_status="failed").count(),
        "email_failed": Order.query.filter_by(email_status="failed").count(),
    }
    pagination = query.order_by(Order.id.desc()).paginate(
        page=page, per_page=current_app.config.get("ORDERS_PER_PAGE", 50), error_out=False
    )
    return render_template(
        "admin.html",
        orders=pagination.items,
        pagination=pagination,
        stats=stats,
        statuses=STATUSES,
        transitions=STATUS_TRANSITIONS,
        f_status=status,
        f_q=q,
        active="orders",
    )


@admin_bp.route("/orders/<int:order_id>/status", methods=["POST"])
def order_status(order_id):
    order = db.get_or_404(Order, order_id)
    new_status = request.form.get("status", "")
    error = change_order_status(order, new_status)
    if not error:
        flash(f"Заказ №{order_id}: статус «{STATUSES[new_status]}»", "ok")
    else:
        flash(error, "error")
    return _back_to_orders()


@admin_bp.route("/orders/<int:order_id>/notify", methods=["POST"])
def order_notify(order_id):
    order = db.get_or_404(Order, order_id)
    errors = resend_order_notification(order)
    if errors:
        for error in errors:
            flash(f"Заказ №{order_id}: {error}", "error")
    else:
        flash(f"Заказ №{order_id} отправлен в Telegram", "ok")
    return _back_to_orders()


@admin_bp.route("/orders/<int:order_id>/email", methods=["POST"])
def order_email(order_id):
    order = db.get_or_404(Order, order_id)
    error = mailer.resend_order_email(order, order_admin_url(order_id))
    if error:
        flash(f"Заказ №{order_id}: письмо не отправлено — {error}", "error")
    else:
        flash(f"Заказ №{order_id} отправлен на почту", "ok")
    return _back_to_orders()


@admin_bp.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        raw = request.form.get("order_emails", "")
        emails, error = mailer.save_order_emails(raw)
        if error:
            flash(error, "error")
            return render_template(
                "admin_settings.html", order_emails=raw, smtp=mailer.smtp_settings(),
                smtp_problem=mailer.smtp_problem(mailer.smtp_settings()), active="settings",
            ), 400
        if emails:
            flash("Почта для заказов сохранена: " + ", ".join(emails), "ok")
        else:
            flash("Почта для заказов очищена — письма о заказах отправляться не будут", "ok")
        return redirect(url_for("admin.settings"))
    smtp = mailer.smtp_settings()
    return render_template(
        "admin_settings.html",
        order_emails=", ".join(mailer.get_order_emails()),
        smtp=smtp,
        smtp_problem=mailer.smtp_problem(smtp),
        active="settings",
    )


@admin_bp.route("/settings/test-email", methods=["POST"])
def settings_test_email():
    error = mailer.send_test_email()
    if error:
        flash(f"Тестовое письмо не отправлено: {error}", "error")
    else:
        flash("Тестовое письмо отправлено: " + ", ".join(mailer.get_order_emails()), "ok")
    return redirect(url_for("admin.settings"))


@admin_bp.route("/telegram/check", methods=["POST"])
def telegram_check():
    for ok, message in check_telegram():
        flash(f"Telegram: {message}", "ok" if ok else "error")
    return _back_to_orders()


@admin_bp.route("/orders/<int:order_id>/delete", methods=["POST"])
def order_delete(order_id):
    order = db.get_or_404(Order, order_id)
    # Удалять можно только отменённый заказ с уже возвращёнными остатками:
    # иначе зарезервированный товар пропадёт со склада, а выручка изменится задним числом.
    if order.status != "cancelled" or not order.stock_released:
        flash(
            f"Заказ №{order_id} нельзя удалить: сначала отмените его "
            "(товар вернётся на склад).",
            "error",
        )
        return _back_to_orders()
    db.session.delete(order)
    if commit_or_flash("Не удалось удалить заказ. Попробуйте ещё раз."):
        flash(f"Заказ №{order_id} удалён", "ok")
    return _back_to_orders()


PRODUCT_FORM = "admin_product_form.html"


@admin_bp.route("/products")
def product_list():
    products = Product.query.order_by(Product.id).all()
    return render_template(
        "admin_products.html", products=products, active="products", max_stock=MAX_STOCK
    )


@admin_bp.route("/products/new", methods=["GET", "POST"])
def product_new():
    if request.method == "POST":
        data, errors = validate_product_form(request.form)
        image = request.files.get("image")
        img_path = None

        if not errors and (not image or not image.filename):
            errors.append("Загрузите фото букета")
        if not errors:
            img_path, img_error = save_image(image)
            if img_error:
                errors.append(img_error)

        if not errors:
            db.session.add(Product(img=img_path, **data))
            if commit_or_flash():
                flash(f"Товар «{data['name']}» добавлен", "ok")
                return redirect(url_for("admin.product_list"))
            delete_image_file(img_path)
            errors.append(SAVE_ERROR)

        return (
            render_template(
                PRODUCT_FORM, product=None, form=request.form, errors=errors, active="products"
            ),
            400,
        )

    return render_template(
        PRODUCT_FORM,
        product=None,
        form={"is_active": "1", "stock": "10"},
        errors=[],
        active="products",
    )


@admin_bp.route("/products/<int:pid>/edit", methods=["GET", "POST"])
def product_edit(pid):
    product = db.get_or_404(Product, pid)

    if request.method == "POST":
        # Остаток здесь не меняется: он мог уменьшиться от покупок, пока форма была открыта.
        data, errors = validate_product_form(request.form, with_stock=False)
        image = request.files.get("image")
        new_img_path = None

        if not errors and image and image.filename:
            new_img_path, img_error = save_image(image)
            if img_error:
                errors.append(img_error)

        if not errors:
            old_img = product.img
            for key, value in data.items():
                setattr(product, key, value)
            if new_img_path:
                product.img = new_img_path
            if commit_or_flash():
                # Старый файл удаляем только после успешного сохранения.
                if new_img_path:
                    delete_image_file(old_img)
                flash(f"Товар «{data['name']}» сохранён", "ok")
                return redirect(url_for("admin.product_list"))
            delete_image_file(new_img_path)
            errors.append(SAVE_ERROR)
            product = db.get_or_404(Product, pid)

        return (
            render_template(
                PRODUCT_FORM, product=product, form=request.form, errors=errors, active="products"
            ),
            400,
        )

    form = {
        "name": product.name,
        "price": product.price,
        "is_active": "1" if product.is_active else "",
        "composition": product.composition,
        "description": product.description,
    }
    return render_template(PRODUCT_FORM, product=product, form=form, errors=[], active="products")


@admin_bp.route("/products/<int:pid>/stock", methods=["POST"])
def product_stock(pid):
    product = db.get_or_404(Product, pid)
    try:
        delta = int(request.form.get("delta", "0").strip())
    except ValueError:
        delta = 0
    if delta == 0:
        flash("Укажите, на сколько изменить остаток (например, 5 или -3).", "error")
        return redirect(url_for("admin.product_list"))
    delta = max(-MAX_STOCK, min(MAX_STOCK, delta))
    adjusted = Product.stock + delta
    db.session.execute(
        update(Product).where(Product.id == product.id).values(stock=case(
            (adjusted < 0, 0), (adjusted > MAX_STOCK, MAX_STOCK), else_=adjusted
        )), execution_options={"synchronize_session": False},
    )
    if commit_or_flash():
        stock = db.session.execute(select(Product.stock).where(Product.id == pid)).scalar()
        flash(f"«{product.name}»: остаток {stock} шт.", "ok")
    return redirect(url_for("admin.product_list"))


@admin_bp.route("/products/<int:pid>/toggle", methods=["POST"])
def product_toggle(pid):
    product = db.get_or_404(Product, pid)
    # Форма передаёт желаемое состояние: два клика подряд не «переключат обратно».
    target = request.form.get("active")
    if target not in {"0", "1"}:
        flash("Не удалось определить нужное состояние товара.", "error")
        return redirect(url_for("admin.product_list"))
    db.session.execute(
        update(Product).where(Product.id == pid).values(is_active=target == "1"),
        execution_options={"synchronize_session": False},
    )
    if commit_or_flash():
        state = "показывается в каталоге" if target == "1" else "скрыт из каталога"
        flash(f"«{product.name}» {state}", "ok")
    return redirect(url_for("admin.product_list"))


def product_has_orders(product) -> bool:
    """Проверка связей средствами SQLite, без загрузки всех заказов в память."""
    by_id = db.session.execute(text(
        'SELECT 1 FROM "order" AS o, json_each(o.items) AS j '
        "WHERE o.items IS NOT NULL AND json_type(o.items) = 'array' "
        "AND CAST(json_extract(j.value, '$.product_id') AS INTEGER) = :pid LIMIT 1"
    ), {"pid": product.id}).first()
    if by_id:
        return True
    # Старые заказы без items: состав записан только строкой «Название (N шт)».
    legacy = db.session.execute(text(
        'SELECT 1 FROM "order" WHERE (items IS NULL OR json_type(items) = \'null\') '
        "AND instr(flower, :needle) > 0 LIMIT 1"
    ), {"needle": f"{product.name} ("}).first()
    return bool(legacy)


@admin_bp.route("/products/<int:pid>/delete", methods=["POST"])
def product_delete(pid):
    product = db.get_or_404(Product, pid)
    # Serialize with checkout before checking references. Keep IDs needed for returns.
    db.session.execute(
        update(Product).where(Product.id == pid).values(is_active=Product.is_active),
        execution_options={"synchronize_session": False},
    )
    if product_has_orders(product):
        db.session.rollback()
        flash("Товар связан с заказом. Скройте его из каталога вместо удаления.", "error")
        return redirect(url_for("admin.product_list"))
    name, img = product.name, product.img
    db.session.delete(product)
    if commit_or_flash("Не удалось удалить товар. Попробуйте ещё раз."):
        delete_image_file(img)
        flash(f"Товар «{name}» удалён", "ok")
    return redirect(url_for("admin.product_list"))
