from flask import Flask, request, jsonify, render_template, make_response
from bill_generator import generate_bill
from products import PRODUCTS
import os
import requests as req
import hashlib
import hmac

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

# ── Razorpay — import only if keys exist ──
rzp_client = None
try:
    if RZP_KEY_ID and RZP_KEY_SECRET:
        import razorpay
        rzp_client = razorpay.Client(auth=(RZP_KEY_ID, RZP_KEY_SECRET))
        print(f"Razorpay ready: {RZP_KEY_ID[:15]}...")
    else:
        print("Razorpay keys not set — demo mode")
except Exception as e:
    print(f"Razorpay init error: {e}")

# ════════════════════════════
# ROUTES
# ════════════════════════════

@app.route('/')
def index():
    resp = make_response(render_template('index.html'))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp

@app.route('/ping')
def ping():
    return jsonify({
        'status': 'alive',
        'razorpay': 'ready' if rzp_client else 'demo',
        'cart_items': len(cart)
    })

@app.route('/cart')
def get_cart():
    total = sum(item['price'] for item in cart)
    return jsonify({'items': cart, 'total': total, 'weight': current_weight})

@app.route('/add_item', methods=['POST'])
def add_item():
    data = request.json or {}
    name = data.get('name', '').strip()
    if name in PRODUCTS:
        item = {'name': name, 'price': PRODUCTS[name]}
        cart.append(item)
        return jsonify({'status': 'added', 'item': item})
    return jsonify({'status': 'error', 'message': 'Product not found'}), 404

@app.route('/update_weight', methods=['POST'])
def update_weight():
    global current_weight
    data = request.json or {}
    current_weight = data.get('weight', 0)
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

@app.route('/payment_qr')
def payment_qr():
    total = sum(item['price'] for item in cart)
    upi = f"upi://pay?pa={UPI_ID}&pn=SmartTrolley&am={total}&cu=INR"
    return jsonify({'qr': upi, 'total': total})

# ════════════════════════════
# RAZORPAY CREATE ORDER
# ════════════════════════════

@app.route('/create_order', methods=['POST'])
def create_order():
    try:
        data = request.json or {}

        # Accept amount from frontend — works even when server cart is empty (demo mode)
        frontend_amount = int(float(data.get('amount', 0)))
        server_total    = sum(item['price'] for item in cart)
        total           = server_total if server_total > 0 else frontend_amount

        print(f"create_order: server={server_total} frontend={frontend_amount} using={total}")

        if total <= 0:
            return jsonify({'status': 'error', 'message': 'Amount is zero'}), 400

        if rzp_client:
            order = rzp_client.order.create({
                'amount': int(total) * 100,
                'currency': 'INR',
                'payment_capture': 1
            })
            print(f"Order created: {order['id']}")
            return jsonify({
                'status': 'success',
                'order_id': order['id'],
                'amount': total,
                'key_id': RZP_KEY_ID
            })
        else:
            # Demo mode
            return jsonify({
                'status': 'success',
                'order_id': f'demo_{total}',
                'amount': total,
                'key_id': 'demo'
            })

    except Exception as e:
        print(f"create_order error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ════════════════════════════
# RAZORPAY VERIFY PAYMENT
# ════════════════════════════

@app.route('/verify_payment', methods=['POST'])
def verify_payment():
    try:
        data       = request.json or {}
        order_id   = data.get('razorpay_order_id', '')
        payment_id = data.get('razorpay_payment_id', '')
        signature  = data.get('razorpay_signature', '')

        print(f"verify: order={order_id} payment={payment_id}")

        if order_id.startswith('demo_'):
            return jsonify({'status': 'success', 'verified': True, 'demo': True})

        if not RZP_KEY_SECRET:
            return jsonify({'status': 'success', 'verified': True})

        body     = f"{order_id}|{payment_id}"
        expected = hmac.new(
            bytes(RZP_KEY_SECRET, 'utf-8'),
            bytes(body, 'utf-8'),
            hashlib.sha256
        ).hexdigest()

        if expected == signature:
            print(f"Payment verified: {payment_id}")
            return jsonify({'status': 'success', 'verified': True})
        else:
            print("Signature mismatch")
            return jsonify({'status': 'error', 'verified': False}), 400

    except Exception as e:
        print(f"verify_payment error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ════════════════════════════
# CHECKOUT — WHATSAPP BILL
# ════════════════════════════

@app.route('/checkout', methods=['POST'])
def checkout():
    try:
        data  = request.json or {}
        phone = data.get('phone', '').strip()
        items = data.get('items', None)
        total = data.get('total', None)

        if not phone:
            return jsonify({'status': 'error', 'message': 'No phone'}), 400

        # Clean phone
        phone = phone.replace('+91','').replace(' ','').replace('-','')
        if phone.startswith('91') and len(phone) == 12:
            phone = phone[2:]
        phone = phone[-10:]

        if len(phone) != 10:
            return jsonify({'status': 'error', 'message': 'Invalid number'}), 400

        # Use frontend items if server cart is empty (demo mode)
        bill_items = items if items else cart
        bill_total = int(float(total)) if total else sum(i['price'] for i in bill_items)
        bill       = generate_bill(bill_items, bill_total)

        print(f"Sending bill to +91{phone} — ₹{bill_total} — {len(bill_items)} items")

        # Try Wati first
        if WATI_API_URL and WATI_API_TOKEN:
            if send_via_wati(phone, bill):
                return jsonify({'status': 'success', 'method': 'wati'})

        # Fallback Twilio
        if TWILIO_SID and TWILIO_TOKEN:
            if send_via_twilio(phone, bill):
                return jsonify({'status': 'success', 'method': 'twilio'})

        return jsonify({'status': 'error', 'message': 'Messaging failed'}), 500

    except Exception as e:
        print(f"checkout error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ════════════════════════════
# MESSAGING
# ════════════════════════════

def send_via_wati(phone, bill):
    try:
        base    = WATI_API_URL.rstrip('/')
        headers = {
            'Authorization': f'Bearer {WATI_API_TOKEN}',
            'Content-Type': 'application/json-patch+json'
        }
        r = req.post(
            f"{base}/api/v1/sendSessionMessage/91{phone}",
            json={'messageText': bill},
            headers=headers, timeout=10
        )
        print(f"Wati: {r.status_code}")
        if r.status_code == 200:
            return True
        r2 = req.post(
            f"{base}/api/v1/sendTextMessage/91{phone}",
            json={'message': bill},
            headers=headers, timeout=10
        )
        print(f"Wati fallback: {r2.status_code}")
        return r2.status_code == 200
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