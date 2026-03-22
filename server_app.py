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
    print(f"Razorpay client ready: {RZP_KEY_ID[:12]}...")
else:
    print("WARNING: Razorpay keys not found!")

# ════════════════════════════════
# BASIC ROUTES
# ════════════════════════════════

@app.route('/')
def index():
    from flask import make_response
    resp = make_response(render_template('index.html'))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp

@app.route('/ping')
def ping():
    return jsonify({
        'status': 'alive',
        'razorpay': 'ready' if rzp_client else 'missing keys',
        'cart_items': len(cart)
    })

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
        data = request.json or {}

        # Accept amount from frontend (works in demo mode when server cart is empty)
        frontend_amount = int(data.get('amount', 0))
        server_total    = sum(item['price'] for item in cart)

        # Use server total if Pi is connected, else use frontend total
        total = server_total if server_total > 0 else frontend_amount

        print(f"Create order: server_total={server_total}, frontend_amount={frontend_amount}, using={total}")

        if total <= 0:
            return jsonify({'status': 'error', 'message': 'Cart is empty — no amount to charge'}), 400

        if rzp_client:
            order = rzp_client.order.create({
                'amount': int(total) * 100,  # paise
                'currency': 'INR',
                'payment_capture': 1,
                'notes': {
                    'source': 'SmartTrolley',
                    'items': str(len(cart))
                }
            })
            print(f"Razorpay order created: {order['id']}")
            return jsonify({
                'status': 'success',
                'order_id': order['id'],
                'amount': total,
                'key_id': RZP_KEY_ID
            })
        else:
            # Demo mode — fake order so UI still works
            print("Demo mode: returning fake order")
            return jsonify({
                'status': 'success',
                'order_id': 'demo_' + str(total),
                'amount': total,
                'key_id': 'demo'
            })

    except Exception as e:
        print(f"Create order ERROR: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ════════════════════════════════
# RAZORPAY — VERIFY PAYMENT
# ════════════════════════════════

@app.route('/verify_payment', methods=['POST'])
def verify_payment():
    try:
        data       = request.json or {}
        order_id   = data.get('razorpay_order_id', '')
        payment_id = data.get('razorpay_payment_id', '')
        signature  = data.get('razorpay_signature', '')

        print(f"Verify: order={order_id}, payment={payment_id}")

        # Demo mode
        if order_id.startswith('demo_'):
            return jsonify({'status': 'success', 'verified': True, 'demo': True})

        if not RZP_KEY_SECRET:
            return jsonify({'status': 'success', 'verified': True, 'note': 'no secret'})

        # Verify HMAC signature
        body     = order_id + "|" + payment_id
        expected = hmac.new(
            RZP_KEY_SECRET.encode('utf-8'),
            body.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

        if expected == signature:
            print(f"Payment verified: {payment_id}")
            return jsonify({'status': 'success', 'verified': True})
        else:
            print(f"Signature mismatch!")
            return jsonify({'status': 'error', 'verified': False}), 400

    except Exception as e:
        print(f"Verify ERROR: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ════════════════════════════════
# CHECKOUT — SEND WHATSAPP BILL
# ════════════════════════════════

@app.route('/checkout', methods=['POST'])
def checkout():
    try:
        data  = request.json or {}
        phone = data.get('phone', '').strip()
        items = data.get('items', cart)  # use frontend items if server cart empty
        total = data.get('total', sum(item['price'] for item in cart))

        if not phone:
            return jsonify({'status': 'error', 'message': 'No phone'}), 400

        # Clean phone number
        phone = phone.replace('+91', '').replace(' ', '').replace('-', '')
        if phone.startswith('91') and len(phone) == 12:
            phone = phone[2:]
        phone = phone[-10:]

        if len(phone) != 10:
            return jsonify({'status': 'error', 'message': 'Invalid number'}), 400

        # Use passed items or server cart
        bill_items = items if items else cart
        bill_total = int(total) if total else sum(i['price'] for i in bill_items)
        bill       = generate_bill(bill_items, bill_total)

        print(f"Sending bill to {phone}, total=₹{bill_total}, items={len(bill_items)}")

        # Try Wati first
        if WATI_API_URL and WATI_API_TOKEN:
            if send_via_wati(phone, bill):
                return jsonify({'status': 'success', 'method': 'wati'})

        # Fallback to Twilio
        if TWILIO_SID and TWILIO_TOKEN:
            if send_via_twilio(phone, bill):
                return jsonify({'status': 'success', 'method': 'twilio'})

        return jsonify({'status': 'error', 'message': 'No messaging service worked'}), 500

    except Exception as e:
        print(f"Checkout ERROR: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ════════════════════════════════
# MESSAGING
# ════════════════════════════════

def send_via_wati(phone, bill):
    try:
        base = WATI_API_URL.rstrip('/')
        headers = {
            'Authorization': f'Bearer {WATI_API_TOKEN}',
            'Content-Type': 'application/json-patch+json'
        }
        url = f"{base}/api/v1/sendSessionMessage/91{phone}"
        r = req.post(url, json={'messageText': bill}, headers=headers, timeout=10)
        print(f"Wati session: {r.status_code}")
        if r.status_code == 200:
            return True
        url2 = f"{base}/api/v1/sendTextMessage/91{phone}"
        r2 = req.post(url2, json={'message': bill}, headers=headers, timeout=10)
        print(f"Wati text: {r2.status_code}")
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
        print(f"Twilio sent: {msg.sid}")
        return True
    except Exception as e:
        print(f"Twilio error: {e}")
        return False

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)