from flask import Flask, render_template, request

app = Flask(__name__)

products = [
    {'id': 1, 'name': 'Букет роз', 'price': 2500, 'img': 'https://images.unsplash.com/photo-1548586196-aa5803b77379?w=500'},
    {'id': 2, 'name': 'Тюльпаны', 'price': 1800, 'img': 'https://images.unsplash.com/photo-1490750967868-88aa4486c946?w=500'},
    {'id': 3, 'name': 'Микс-букет', 'price': 3200, 'img': 'https://images.unsplash.com/photo-1526047932273-341f2a7631f9?w=500'},
    {'id': 4, 'name': 'Цветы в коробке', 'price': 4000, 'img': 'https://images.unsplash.com/photo-1561181286-d3fee7d55364?w=500'},
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

@app.route("/about")
def about():
    return render_template("about.html")

if __name__ == "__main__":
    app.run(debug=True)