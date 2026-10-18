from flask import Flask, render_template, request

app = Flask(__name__)

products = [
    {'id': 1, 'name': 'Букет роз', 'price': 2500, 'img': '/static/images/roses.jpeg'},
    
    {'id': 2, 'name': 'Тюльпаны', 'price': 1800, 'img': '/static/images/tulps.jpg'},
    
    {'id': 3, 'name': 'Микс-букет', 'price': 3200, 'img': '/static/images/mix.jpeg'}, 
    
    {'id': 4, 'name': 'Цветы в коробке', 'price': 4000, 'img': '/static/images/box.jpg'},
]

@app.route("/", methods=["GET", "POST"])
def home():
    message = ""
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        flower = request.form.get("flower", "").strip()

        if not name or not phone:
            message = "Заполните имя и телефон"
        else:
            message = f"Спасибо, {name}! Заявка на «{flower}» принята."

    return render_template("index.html", products=products, message=message)

if __name__ == "__main__":
    app.run(debug=True)