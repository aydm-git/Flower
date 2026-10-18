from flask import Flask, render_template, request, session, redirect, url_for, flash
import openpyxl
import os
from datetime import datetime
from werkzeug.security import check_password_hash

app = Flask(__name__)
app.secret_key = 'sumb123'
USERNAME = "aydm123"
PASSWORD_HASH = "scrypt:32768:8:1$YVFIfcPjDBHcZJTi$c44eece5ff2f6057d2ee3cc5217cf093cd9b1746efe2505b91c7153e93aafaebb24191cdc5dc575701d95345ad953d1e9f9e1caa87fccb36ea378e65ffb6ff5d"

EXCEL_FILE = "orders.xlsx"

products = [
    {'id': 1, 'name': 'Букет роз', 'price': 2500, 'img': '/static/images/roses.jpeg'},
    {'id': 2, 'name': 'Тюльпаны', 'price': 1800, 'img': '/static/images/tulps.jpg'},
    {'id': 3, 'name': 'Микс-букет', 'price': 3200, 'img': '/static/images/mix.jpeg'},
    {'id': 4, 'name': 'Цветы в коробке', 'price': 4000, 'img': '/static/images/box.jpg'},
]

def init_excel():
    if not os.path.exists(EXCEL_FILE):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Заказы"
        ws.append(["ID", "Дата", "Имя", "Телефон", "Букет"])
        wb.save(EXCEL_FILE)

def save_to_excel(name, phone, flower):
    try:
        wb = openpyxl.load_workbook(EXCEL_FILE)
        ws = wb.active
        next_id = ws.max_row
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ws.append([next_id, now, name, phone, flower])
        wb.save(EXCEL_FILE)
        return True
    except Exception as e:
        print(f"[EXCEL ERROR] {e}")
        return False

init_excel()

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
            if save_to_excel(name, phone, flower):
                message_type = "success"
            else:
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

    if not os.path.exists(EXCEL_FILE):
        return "Файл с заказами еще не создан. Сначала оформите первый заказ на сайте."

    try:
        wb = openpyxl.load_workbook(EXCEL_FILE)
        ws = wb.active
        orders = []

        for row in ws.iter_rows(min_row=2, values_only=True):
            if row[0] is not None:
                orders.append({
                    'id': row[0],
                    'date': row[1],
                    'name': row[2],
                    'phone': row[3],
                    'flower': row[4]
                })

        return render_template("admin.html", orders=orders)
    except Exception as e:
        return f"Ошибка при чтении файла: {e}"

if __name__ == "__main__":
    app.run(debug=True)