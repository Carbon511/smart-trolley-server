from flask import Flask, request, jsonify, render_template
from bill_generator import generate_bill
from products import PRODUCTS
import os
import requests as req
import razorpay
import hmac
import hashlib

app = Flask(__name__)

# ── State ──
cart = []
current_weight = 0

# ── Credentials ──
TWILIO_SID     = os.environ.get('TWILIO_ACCOUNT_SID', '')
TWILIO_TOKEN   = os.environ.get('TWILIO_AUTH_TOKEN', '')
WATI_API_URL   = os.environ.get('WATI_API_URL', '')
WATI_API_TOKEN = os.environ.get('WATI_API_TOKEN', '')
RZP_KEY_ID     = os.environ.get('RAZORPAY_KEY_ID', '')
RZP_KEY_SECRET = os.environ.get('RAZORPAY_KEY_SECRET', '')
UPI_ID         = "sherinsajan784-2@oksbi"

# ── Razorpay client ──
rzp_client = None
if RZP_KEY_ID and RZP_KEY_SECRET:
    rzp_client = razorpay.Client(auth=(RZP_KEY_ID, RZP_KEY_SECRET))

# ════════════════════════════════
# BASIC ROUTES
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
    return jsonify({'items': cart, 'total': total, 'weight': current_weight})

@app.route('/add_item', methods=['POST'])
def add_item():
    data = request.json
    name = data.get('name', '').strip()
    if name in PRODUCTS:
        item = {'name': name, 'price': PRODUCTS[name]}
        cart.append(item)
        return jsonify({'status': 'added', 'item': item})
    return jsonify({'status': 'error', 'message': 'Product not found'}), 404

@app.route('/update_weight', methods=['POST'])
def update_weight():
    global current_weight
    current_weight = request.json.get('weight', 0)
    return jsonify({'status': 'ok', 'weight': current_weight})

@app.route('/remove/<int:index>', methods=['DELETE', 'GET', 'POST'])
def remove_item(index):
    if 0 <= index < len(cart):
        removed = cart.pop(index)
        return jsonify({'status': 'removed', 'item': removed})
    return jsonify({'status': 'error'}), 400

@app.route('/reset', methods=['POST', 'GET'])
def reset():
    global cart, current_weight
    cart = []
    current_weight = 0
    return jsonify({'status': 'reset'})

# ════════════════════════════════
# RAZORPAY — CREATE ORDER
# ════════════════════════════════

@app.route('/create_order', methods=['POST'])
def create_order():
    try:
        total = sum(item['price'] for item in cart)
        if total == 0:
            return jsonify({'status': 'error', 'message': 'Cart is empty'}), 400

        if rzp_client:
            # Create Razorpay order (amount in paise)
            order = rzp_client.order.create({
                'amount': total * 100,
                'currency': 'INR',
                'payment_capture': 1,
                'notes': {
                    'cart_items': str(len(cart)),
                    'upi_id': UPI_ID
                }
            })
            return jsonify({
                'status': 'success',
                'order_id': order['id'],
                'amount': total,
                'key_id': RZP_KEY_ID
            })
        else:
            # Demo mode — return fake order
            return jsonify({
                'status': 'success',
                'order_id': 'demo_' + str(total),
                'amount': total,
                'key_id': 'demo'
            })

    except Exception as e:
        print(f"Create order error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ════════════════════════════════
# RAZORPAY — VERIFY PAYMENT
# ════════════════════════════════

@app.route('/verify_payment', methods=['POST'])
def verify_payment():
    try:
        data = request.json
        order_id   = data.get('razorpay_order_id', '')
        payment_id = data.get('razorpay_payment_id', '')
        signature  = data.get('razorpay_signature', '')

        # Demo mode
        if order_id.startswith('demo_'):
            return jsonify({'status': 'success', 'verified': True, 'demo': True})

        # Verify signature
        body = order_id + "|" + payment_id
        expected = hmac.new(
            RZP_KEY_SECRET.encode(),
            body.encode(),
            hashlib.sha256
        ).hexdigest()

        if expected == signature:
            return jsonify({'status': 'success', 'verified': True})
        else:
            return jsonify({'status': 'error', 'verified': False, 'message': 'Invalid signature'}), 400

    except Exception as e:
        print(f"Verify error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ════════════════════════════════
# CHECKOUT — SEND WHATSAPP BILL
# ════════════════════════════════

@app.route('/checkout', methods=['POST'])
def checkout():
    try:
        data  = request.json
        phone = data.get('phone', '').strip()

        if not phone:
            return jsonify({'status': 'error', 'message': 'No phone'}), 400

        # Clean number
        phone = phone.replace('+91', '').replace(' ', '').replace('-', '')
        if phone.startswith('91') and len(phone) == 12:
            phone = phone[2:]
        phone = phone[-10:]

        if len(phone) != 10:
            return jsonify({'status': 'error', 'message': 'Invalid number'}), 400

        total = sum(item['price'] for item in cart)
        bill  = generate_bill(cart, total)

        # Try Wati first
        if WATI_API_URL and WATI_API_TOKEN:
            if send_via_wati(phone, bill):
                return jsonify({'status': 'success', 'method': 'wati'})

        # Fallback to Twilio
        if TWILIO_SID and TWILIO_TOKEN:
            if send_via_twilio(phone, bill):
                return jsonify({'status': 'success', 'method': 'twilio'})

        return jsonify({'status': 'error', 'message': 'Messaging failed'}), 500

    except Exception as e:
        print(f"Checkout error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ════════════════════════════════
# MESSAGING
# ════════════════════════════════

def send_via_wati(phone, bill):
    try:
        # Remove trailing slash from URL
        base = WATI_API_URL.rstrip('/')

        headers = {
            'Authorization': f'Bearer {WATI_API_TOKEN}',
            'Content-Type': 'application/json-patch+json'
        }

        # Method 1 — session message
        url = f"{base}/api/v1/sendSessionMessage/91{phone}"
        r = req.post(url, json={'messageText': bill}, headers=headers, timeout=10)
        print(f"Wati session: {r.status_code} {r.text[:200]}")
        if r.status_code == 200:
            return True

        # Method 2 — text message
        url2 = f"{base}/api/v1/sendTextMessage/91{phone}"
        r2 = req.post(url2, json={'message': bill}, headers=headers, timeout=10)
        print(f"Wati text: {r2.status_code} {r2.text[:200]}")
        if r2.status_code == 200:
            return True

        return False

    except Exception as e:
        print(f"Wati error: {e}")
        return False


def send_via_twilio(phone, bill):
    try:
        from twilio.rest import Client
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        msg = client.messages.create(
            from_='whatsapp:+14155238886',
            to=f'whatsapp:+91{phone}',
            body=bill
        )
        print(f"Twilio: {msg.sid}")
        return True
    except Exception as e:
        print(f"Twilio error: {e}")
        return False


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)