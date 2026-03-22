from flask import Flask, render_template, jsonify, request
from bill_generator import generate_bill
from twilio.rest import Client
import os

app = Flask(__name__)

cart = []
weight_data = 0

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/add_item", methods=["POST"])
def add_item():
    item = request.json
    cart.append(item)
    print("Item added:", item["name"])
    return jsonify({"status": "added"})

@app.route("/update_weight", methods=["POST"])
def update_weight():
    global weight_data
    weight_data = request.json.get("weight", 0)
    return jsonify({"status": "updated"})

@app.route("/cart")
def get_cart():
    total = sum(item["price"] for item in cart)
    return jsonify({"items": cart, "total": total, "weight": weight_data})

@app.route("/remove/<int:index>")
def remove(index):
    if index < len(cart):
        cart.pop(index)
    return jsonify({"status": "removed"})

@app.route("/reset")
def reset():
    cart.clear()
    return jsonify({"status": "cart reset"})

@app.route("/payment_qr")
def payment_qr():
    total = sum(item["price"] for item in cart)
    upi_link = f"upi://pay?pa=sherinsajan784-2@oksbi&pn=SmartTrolley&am={total}&cu=INR"
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={upi_link}&color=1a6b4a"
    return jsonify({"qr": qr_url, "amount": total})

@app.route("/checkout", methods=["POST"])
def checkout():
    phone = request.json["phone"]
    total = sum(item["price"] for item in cart)
    bill_text = generate_bill(cart, total)
    send_whatsapp(phone, bill_text)
    cart.clear()
    return jsonify({"status": "success", "total": total})

def send_whatsapp(phone, bill_text):
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    client = Client(account_sid, auth_token)
    client.messages.create(
        body=bill_text,
        from_='whatsapp:+14155238886',
        to='whatsapp:+91' + phone
    )

@app.route("/ping")
def ping():
    return jsonify({"status": "alive"})

if __name__ == "__main__":
    print("Starting Smart Trolley Server...")
    app.run(host="0.0.0.0", port=10000, debug=False)