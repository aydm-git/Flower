from flask import Flask, render_template, request, session, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from werkzeug.security import check_password_hash

app = Flask(__name__)
app.secret_key = 'sumb123'
USERNAME = "aydm123"
PASSWORD_HASH = "scrypt:32768:8:1$YVFIfcPjDBHcZJTi$c44eece5ff2f6057d2ee3cc5217cf093cd9b1746efe2505b91c7153e93aafaebb24191cdc5dc575701d95345ad953d1e9f9e1caa87fccb36ea378e65ffb6ff5d"

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///flower.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(50), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    flower = db.Column(db.String(255), nullable=False)

products = [
    {'id': 1, 'name': 'Букет роз', 'price': 2500, 'img': '/static/images/roses.jpeg'},
    {'id': 2, 'name': 'Тюльпаны', 'price': 1800, 'img': '/static/images/tulps.jpg'},
    {'id': 3, 'name': 'Микс-букет', 'price': 3200, 'img': '/static/images/mix.jpeg'},
    {'id': 4, 'name': 'Цветы в коробке', 'price': 4000, 'img': '/static/images/box.jpg'},
]

with app.app_context():
    db.create_all()

@app.route("/", methods=["GET", "POST"])
def home():
    message_type = ""
    name = ""
    flower = ""

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        flower = request.form.get("flower", "").strip()

        if not name or not phone:
            message_type = "error_empty"
        else:
            try:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                new_order = Order(date=now, name=name, phone=phone, flower=flower)
                db.session.add(new_order)
                db.session.commit()
                message_type = "success"
            except Exception as e:
                print(f"[DB ERROR] {e}")
                db.session.rollback()
                message_type = "error_db"

    return render_template("index.html", products=products, message_type=message_type, name=name, flower=flower)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        if username == USERNAME and check_password_hash(PASSWORD_HASH, password):
            session["logged_in"] = True
            return redirect(url_for("admin"))
        else:
            flash("Неверный логин или пароль", "error")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.pop("logged_in", None)
    return redirect(url_for("home"))

@app.route("/admin")
def admin():
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    orders = Order.query.order_by(Order.id.desc()).all()
    
    orders_list = []
    for o in orders:
        orders_list.append({
            'id': o.id,
            'date': o.date,
            'name': o.name,
            'phone': o.phone,
            'flower': o.flower
        })

    return render_template("admin.html", orders=orders_list)

if __name__ == "__main__":
    app.run(debug=True)