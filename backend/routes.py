import hmac
from datetime import date

from flask import (
    Blueprint, current_app, flash, redirect, render_template, request, session, url_for,
)
from sqlalchemy import func, or_
from werkzeug.security import check_password_hash

from config import MAX_STOCK
from helpers import (
    delivery_date_limits, delete_image_file, parse_cart, place_order, register_failed_login,
    reset_attempts, save_image, slot_options, too_many_attempts, validate_order_form,
    validate_product_form,
)
from models import STATUSES, Order, Product, db, to_ru_date

shop_bp = Blueprint("shop", __name__)
auth_bp = Blueprint("auth", __name__)
admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


@shop_bp.route("/", methods=["GET", "POST"])
def home():
    error = ""
    form = {}

    if request.method == "POST":
        form = request.form
        cart = parse_cart(form.get("cart", ""))

        data, error = validate_order_form(form, cart)
        if not error:
            total, error = place_order(data, cart)
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
    products = Product.query.filter_by(is_active=True).order_by(Product.id).all()
    return (
        render_template(
            "index.html",
            products=products,
            error=error,
            form=form,
            min_date=min_date,
            max_date=max_date,
            slots=slot_options(),
        ),
        400 if error else 200,
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


@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("shop.home"))


@admin_bp.before_request
def require_login():
    if not session.get("logged_in"):
        return redirect(url_for("auth.login"))


def _back_to_orders():
    return redirect(
        url_for(
            "admin.order_list",
            status=request.form.get("f_status") or None,
            q=request.form.get("f_q") or None,
        )
    )


@admin_bp.route("")
def order_list():
    status = request.args.get("status", "")
    q = request.args.get("q", "").strip()

    query = Order.query
    if status in STATUSES:
        query = query.filter_by(status=status)
    else:
        status = ""
    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(
                Order.name.ilike(like),
                Order.phone.ilike(like),
                Order.flower.ilike(like),
                Order.address.ilike(like),
            )
        )

    stats = {
        "count": Order.query.count(),
        "new": Order.query.filter_by(status="new").count(),
        "today": Order.query.filter(
            Order.delivery_date == date.today().isoformat(),
            Order.status.in_(["new", "in_work"]),
        ).count(),
        "revenue": db.session.query(func.coalesce(func.sum(Order.total), 0))
        .filter(Order.status != "cancelled")
        .scalar(),
    }
    return render_template(
        "admin.html",
        orders=query.order_by(Order.id.desc()).all(),
        stats=stats,
        statuses=STATUSES,
        f_status=status,
        f_q=q,
        active="orders",
    )


@admin_bp.route("/orders/<int:order_id>/status", methods=["POST"])
def order_status(order_id):
    order = db.get_or_404(Order, order_id)
    new_status = request.form.get("status", "")
    if new_status in STATUSES:
        order.status = new_status
        db.session.commit()
        flash(f"Заказ №{order.id}: статус «{STATUSES[new_status]}»", "ok")
    else:
        flash("Неизвестный статус", "error")
    return _back_to_orders()


@admin_bp.route("/orders/<int:order_id>/delete", methods=["POST"])
def order_delete(order_id):
    order = db.get_or_404(Order, order_id)
    db.session.delete(order)
    db.session.commit()
    flash(f"Заказ №{order_id} удалён", "ok")
    return _back_to_orders()


PRODUCT_FORM = "admin_product_form.html"


@admin_bp.route("/products")
def product_list():
    products = Product.query.order_by(Product.id).all()
    return render_template("admin_products.html", products=products, active="products")


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
            db.session.commit()
            flash(f"Товар «{data['name']}» добавлен", "ok")
            return redirect(url_for("admin.product_list"))

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
        data, errors = validate_product_form(request.form)
        image = request.files.get("image")
        new_img_path = None

        if not errors and image and image.filename:
            new_img_path, img_error = save_image(image)
            if img_error:
                errors.append(img_error)

        if not errors:
            for key, value in data.items():
                setattr(product, key, value)
            if new_img_path:
                delete_image_file(product.img)
                product.img = new_img_path
            db.session.commit()
            flash(f"Товар «{product.name}» сохранён", "ok")
            return redirect(url_for("admin.product_list"))

        return (
            render_template(
                PRODUCT_FORM, product=product, form=request.form, errors=errors, active="products"
            ),
            400,
        )

    form = {
        "name": product.name,
        "price": product.price,
        "stock": product.stock,
        "is_active": "1" if product.is_active else "",
        "composition": product.composition,
        "description": product.description,
    }
    return render_template(PRODUCT_FORM, product=product, form=form, errors=[], active="products")


@admin_bp.route("/products/<int:pid>/stock", methods=["POST"])
def product_stock(pid):
    product = db.get_or_404(Product, pid)
    try:
        delta = int(request.form.get("delta", "0"))
    except ValueError:
        delta = 0
    delta = max(-1000, min(1000, delta))
    product.stock = max(0, min(MAX_STOCK, product.stock + delta))
    db.session.commit()
    return redirect(url_for("admin.product_list"))


@admin_bp.route("/products/<int:pid>/toggle", methods=["POST"])
def product_toggle(pid):
    product = db.get_or_404(Product, pid)
    product.is_active = not product.is_active
    db.session.commit()
    state = "показывается в каталоге" if product.is_active else "скрыт из каталога"
    flash(f"«{product.name}» {state}", "ok")
    return redirect(url_for("admin.product_list"))


@admin_bp.route("/products/<int:pid>/delete", methods=["POST"])
def product_delete(pid):
    product = db.get_or_404(Product, pid)
    name, img = product.name, product.img
    db.session.delete(product)
    db.session.commit()
    delete_image_file(img)
    flash(f"Товар «{name}» удалён", "ok")
    return redirect(url_for("admin.product_list"))
