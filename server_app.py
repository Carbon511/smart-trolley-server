from flask import Flask, request, jsonify, render_template
from twilio.rest import Client
from bill_generator import generate_bill
from products import PRODUCTS
import os
import requests as req

app = Flask(__name__)

# ── Cart stored in memory ──
cart = []
current_weight = 0

# ── Twilio credentials (kept as backup) ──
TWILIO_SID   = os.environ.get('TWILIO_ACCOUNT_SID', '')
TWILIO_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN', '')

# ── Wati credentials ──
WATI_API_URL   = os.environ.get('WATI_API_URL', '')
WATI_API_TOKEN = os.environ.get('WATI_API_TOKEN', '')

# ── UPI details ──
UPI_ID   = "sherinsajan784-2@oksbi"
UPI_NAME = "SmartTrolley"

# ════════════════════════════════
# ROUTES
# ════════════════════════════════

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/ping')
def ping():
    return jsonify({'status': 'alive'})

@app.route('/cart')
def get_cart():
    total = sum(item['price'] for item in cart)
    return jsonify({
        'items': cart,
        'total': total,
        'weight': current_weight
    })

@app.route('/add_item', methods=['POST'])
def add_item():
    global cart
    data = request.json
    name = data.get('name', '').strip()
    if name in PRODUCTS:
        item = {
            'name': name,
            'price': PRODUCTS[name]
        }
        cart.append(item)
        return jsonify({'status': 'added', 'item': item})
    return jsonify({'status': 'error', 'message': 'Product not found'}), 404

@app.route('/update_weight', methods=['POST'])
def update_weight():
    global current_weight
    data = request.json
    current_weight = data.get('weight', 0)
    return jsonify({'status': 'ok', 'weight': current_weight})

@app.route('/remove/<int:index>', methods=['DELETE', 'GET', 'POST'])
def remove_item(index):
    global cart
    if 0 <= index < len(cart):
        removed = cart.pop(index)
        return jsonify({'status': 'removed', 'item': removed})
    return jsonify({'status': 'error', 'message': 'Invalid index'}), 400

@app.route('/reset', methods=['POST', 'GET'])
def reset():
    global cart, current_weight
    cart = []
    current_weight = 0
    return jsonify({'status': 'reset'})

@app.route('/payment_qr')
def payment_qr():
    total = sum(item['price'] for item in cart)
    upi_string = f"upi://pay?pa={UPI_ID}&pn={UPI_NAME}&am={total}&cu=INR"
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=200x200&data={upi_string}&color=1a6b4a"
    return jsonify({'qr': qr_url, 'upi': upi_string, 'total': total})

@app.route('/checkout', methods=['POST'])
def checkout():
    global cart
    try:
        data  = request.json
        phone = data.get('phone', '').strip()

        if not phone:
            return jsonify({'status': 'error', 'message': 'No phone number provided'}), 400

        # ── Clean phone number to 10 digits ──
        phone = phone.replace('+91', '').replace(' ', '').strip()
        if phone.startswith('91') and len(phone) == 12:
            phone = phone[2:]
        phone = phone[-10:]  # take last 10 digits always

        if len(phone) != 10:
            return jsonify({'status': 'error', 'message': 'Invalid phone number'}), 400

        total = sum(item['price'] for item in cart)
        bill  = generate_bill(cart, total)

        # ── Try Wati first ──
        if WATI_API_URL and WATI_API_TOKEN:
            success = send_via_wati(phone, bill)
            if success:
                return jsonify({'status': 'success', 'method': 'wati'})

        # ── Fallback to Twilio ──
        if TWILIO_SID and TWILIO_TOKEN:
            success = send_via_twilio(phone, bill)
            if success:
                return jsonify({'status': 'success', 'method': 'twilio'})

        return jsonify({'status': 'error', 'message': 'No messaging service configured'}), 500

    except Exception as e:
        print(f"Checkout error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


# ════════════════════════════════
# MESSAGING FUNCTIONS
# ════════════════════════════════

def send_via_wati(phone, bill):
    """Send WhatsApp message via Wati.io"""
    try:
        headers = {
            'Authorization': f'Bearer {WATI_API_TOKEN}',
            'Content-Type': 'application/json-patch+json'
        }

        # Send plain text session message
        url = f"{WATI_API_URL}/api/v1/sendSessionMessage/91{phone}"
        payload = {'messageText': bill}

        r = req.post(url, json=payload, headers=headers, timeout=10)

        print(f"Wati response: {r.status_code} — {r.text}")

        if r.status_code == 200:
            return True

        # Try template message as fallback
        url2 = f"{WATI_API_URL}/api/v1/sendTemplateMessage"
        payload2 = {
            'template_name': 'hello_world',
            'broadcast_name': 'SmartTrolley',
            'receivers': [{
                'whatsappNumber': f'91{phone}',
                'customParams': [{'name': '1', 'value': bill}]
            }]
        }
        r2 = req.post(url2, json=payload2, headers=headers, timeout=10)
        print(f"Wati template response: {r2.status_code} — {r2.text}")
        return r2.status_code == 200

    except Exception as e:
        print(f"Wati error: {e}")
        return False


def send_via_twilio(phone, bill):
    """Send WhatsApp message via Twilio sandbox"""
    try:
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        message = client.messages.create(
            from_='whatsapp:+14155238886',
            to=f'whatsapp:+91{phone}',
            body=bill
        )
        print(f"Twilio SID: {message.sid}")
        return True
    except Exception as e:
        print(f"Twilio error: {e}")
        return False


# ════════════════════════════════
# RUN
# ════════════════════════════════
if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)