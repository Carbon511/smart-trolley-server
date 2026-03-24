from flask import Flask, request, jsonify, render_template, render_template_string, make_response
from bill_generator import generate_bill
from logger import log_purchase, read_logs
import os
import re
import requests as req
import hashlib
import hmac
from datetime import datetime
from collections import defaultdict
import time as _time

app = Flask(__name__)

# ─────────────────────────────────────────────────────────────────
#  PRODUCTS
# ─────────────────────────────────────────────────────────────────
PRODUCTS = {
    "Book Record":      {"name": "Book Record",      "price": 50,  "stock": 100},
    "Brustro Pencils":  {"name": "Brustro Pencils",  "price": 120, "stock": 100},
    "Garnier Men":      {"name": "Garnier Men",       "price": 199, "stock": 100},
    "krackjack 120g":   {"name": "Krackjack 120g",   "price": 20,  "stock": 100},
    "krackjack 180g":   {"name": "Krackjack 180g",   "price": 30,  "stock": 100},
    "Sprite 1L":        {"name": "Sprite 1L",         "price": 60,  "stock": 100},
    "Sprite 400ml":     {"name": "Sprite 400ml",      "price": 30,  "stock": 100},
    "Maggi 140g":       {"name": "Maggi 140g",        "price": 25,  "stock": 100},
    "Maggi 280g":       {"name": "Maggi 280g",        "price": 50,  "stock": 100},
    "iva Liquid 500ml": {"name": "Iva Liquid 500ml",  "price": 99,  "stock": 100},
    "Globe Cream":      {"name": "Globe Cream",       "price": 85,  "stock": 100},
    "Soudal paint":     {"name": "Soudal Paint",      "price": 250, "stock": 100},
    "Dr wash 190g":     {"name": "Dr Wash 190g",      "price": 45,  "stock": 100},
    "Fresh& Fruity":    {"name": "Fresh & Fruity",    "price": 30,  "stock": 100},
}

# Expected weights in grams — used for Pi fusion matching
PRODUCT_WEIGHTS = {
    "Book Record":      300,
    "Brustro Pencils":  150,
    "Garnier Men":      120,
    "krackjack 120g":   135,
    "krackjack 180g":   195,
    "Sprite 1L":        1050,
    "Sprite 400ml":     420,
    "Maggi 140g":       155,
    "Maggi 280g":       300,
    "iva Liquid 500ml": 520,
    "Globe Cream":      80,
    "Soudal paint":     400,
    "Dr wash 190g":     210,
    "Fresh& Fruity":    100,
}

# ─────────────────────────────────────────────────────────────────
#  ENV / CREDENTIALS
# ─────────────────────────────────────────────────────────────────
TWILIO_SID     = os.environ.get('TWILIO_ACCOUNT_SID', '')
TWILIO_TOKEN   = os.environ.get('TWILIO_AUTH_TOKEN', '')
WATI_API_URL   = os.environ.get('WATI_API_URL', '')
WATI_API_TOKEN = os.environ.get('WATI_API_TOKEN', '')
RZP_KEY_ID     = os.environ.get('RAZORPAY_KEY_ID', '')
RZP_KEY_SECRET = os.environ.get('RAZORPAY_KEY_SECRET', '')
UPI_ID         = "sherinsajan784-2@oksbi"

_rzp_client = None

def get_rzp():
    global _rzp_client
    if _rzp_client is not None:
        return _rzp_client
    if RZP_KEY_ID and RZP_KEY_SECRET:
        try:
            import razorpay as rzp_module
            _rzp_client = rzp_module.Client(auth=(RZP_KEY_ID, RZP_KEY_SECRET))
            print(f"Razorpay ready: {RZP_KEY_ID[:15]}...")
        except Exception as e:
            print(f"Razorpay init error: {e}")
    return _rzp_client

print("SmartTrolley server starting...")
print(f"Razorpay key : {'set' if RZP_KEY_ID else 'MISSING'}")
print(f"Wati         : {'set' if WATI_API_URL else 'MISSING'}")
print(f"Twilio       : {'set' if TWILIO_SID else 'MISSING'}")

# ─────────────────────────────────────────────────────────────────
#  LEGACY CART  (manual add_item flow)
# ─────────────────────────────────────────────────────────────────
cart = []
current_weight = 0

# ─────────────────────────────────────────────────────────────────
#  LEGACY ROUTES  (kept exactly as original)
# ─────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    resp = make_response(render_template('index.html'))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma']        = 'no-cache'
    resp.headers['Expires']       = '0'
    return resp

@app.route('/ping')
def ping():
    return jsonify({
        'status'    : 'alive',
        'razorpay'  : 'set' if RZP_KEY_ID else 'missing',
        'wati'      : 'set' if WATI_API_URL else 'missing',
        'twilio'    : 'set' if TWILIO_SID else 'missing',
        'cart_items': len(cart),
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
        item = {'name': name, 'price': PRODUCTS[name]['price']}
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

@app.route('/create_order', methods=['POST'])
def create_order():
    try:
        data            = request.json or {}
        frontend_amount = int(float(data.get('amount', 0)))
        server_total    = sum(item['price'] for item in cart)
        total           = server_total if server_total > 0 else frontend_amount
        print(f"create_order: server={server_total} frontend={frontend_amount} using={total}")
        if total <= 0:
            return jsonify({'status': 'error', 'message': 'Amount is zero'}), 400
        client = get_rzp()
        if client:
            order = client.order.create({
                'amount'         : int(total) * 100,
                'currency'       : 'INR',
                'payment_capture': 1,
            })
            print(f"Razorpay order created: {order['id']}")
            return jsonify({'status': 'success', 'order_id': order['id'],
                            'amount': total, 'key_id': RZP_KEY_ID})
        else:
            return jsonify({'status': 'success', 'order_id': f'demo_{total}',
                            'amount': total, 'key_id': 'demo'})
    except Exception as e:
        print(f"create_order ERROR: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/verify_payment', methods=['POST'])
def verify_payment():
    data = request.json or {}
    print(f"verify_payment: {data.get('razorpay_payment_id', '')}")
    return jsonify({'status': 'success', 'verified': True})

@app.route('/checkout', methods=['POST'])
def checkout():
    try:
        data       = request.json or {}
        phone      = str(data.get('phone', '')).strip()
        items      = data.get('items', None)
        total      = data.get('total', None)
        payment_id = str(data.get('payment_id', ''))
        trolley    = str(data.get('trolley', 'T-0000'))

        print(f"Raw phone received: '{phone}'")

        if not phone:
            return jsonify({'status': 'error', 'message': 'No phone'}), 400

        phone = re.sub(r'\D', '', phone)
        print(f"After removing non-digits: '{phone}'")

        if phone.startswith('91') and len(phone) == 12:
            phone = phone[2:]

        phone = phone[-10:]
        print(f"Final phone: '{phone}' length: {len(phone)}")

        if len(phone) != 10:
            return jsonify({'status': 'error', 'message': f'Invalid number: {phone}'}), 400

        bill_items = items if items else cart
        bill_total = int(float(total)) if total else sum(i['price'] for i in bill_items)
        bill       = generate_bill(bill_items, bill_total)

        print(f"Sending bill to {phone} total=Rs.{bill_total} items={len(bill_items)}")

        # Log the purchase
        log_purchase(phone, bill_items, bill_total, payment_id, trolley)

        # Deduct stock
        for item in bill_items:
            name = item.get('name')
            if name in PRODUCTS and 'stock' in PRODUCTS[name]:
                PRODUCTS[name]['stock'] = max(0, PRODUCTS[name]['stock'] - 1)

        print(f"Bill text length: {len(bill)}")

        if WATI_API_URL and WATI_API_TOKEN:
            if send_via_wati(phone, bill, cart_items=bill_items, total=bill_total,
                             trolley=trolley, payment_id=payment_id):
                return jsonify({'status': 'success', 'method': 'wati'})

        if TWILIO_SID and TWILIO_TOKEN:
            if send_via_twilio(phone, bill):
                return jsonify({'status': 'success', 'method': 'twilio'})

        return jsonify({'status': 'error', 'message': 'Both failed'}), 500

    except Exception as e:
        print(f"checkout ERROR: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


def send_via_wati(phone, bill, cart_items=None, total=0, trolley="", payment_id=""):
    try:
        base     = WATI_API_URL.rstrip('/')
        date_str = datetime.now().strftime("%d %b %Y, %I:%M %p")

        if cart_items:
            items_text = "\n".join([f"* {i.get('name','?')} - Rs.{i.get('price',0)}"
                                     for i in cart_items])
        else:
            items_text = "Items purchased"

        headers = {
            'Authorization': f'Bearer {WATI_API_TOKEN}',
            'Content-Type' : 'application/json',
        }
        for num_format in [f"91{phone}", phone]:
            url = f"{base}/api/v1/sendTemplateMessage?whatsappNumber={num_format}"
            payload = {
                "template_name"  : "smart_trolley_bill_receipt",
                "broadcast_name" : "SmartTrolley_Bill",
                "parameters": [
                    {"name": "1", "value": str(trolley or "T-0000")},
                    {"name": "2", "value": str(phone)},
                    {"name": "3", "value": str(date_str)},
                    {"name": "4", "value": str(items_text)},
                    {"name": "5", "value": str(total)},
                    {"name": "6", "value": str(payment_id or "N/A")},
                ],
            }
            r = req.post(url, json=payload, headers=headers, timeout=10)
            print(f"Wati template [{num_format}]: {r.status_code} {r.text[:300]}")
            try:
                result = r.json()
                if result.get('result') == True:
                    print(f"Wati SUCCESS with: {num_format}")
                    return True
            except Exception:
                pass

        headers2 = {
            'Authorization': f'Bearer {WATI_API_TOKEN}',
            'Content-Type' : 'application/json-patch+json',
        }
        msg_text = bill if bill else f"SmartTrolley Bill\nTotal: Rs.{total}\nThank you!"
        url2 = f"{base}/api/v1/sendSessionMessage/91{phone}"
        r2 = req.post(url2, json={'messageText': msg_text}, headers=headers2, timeout=10)
        print(f"Wati session: {r2.status_code} {r2.text[:200]}")
        if r2.status_code == 200:
            try:
                if r2.json().get('result') == True:
                    return True
            except Exception:
                pass

        return False

    except Exception as e:
        print(f"Wati error: {e}")
        return False


def send_via_twilio(phone, bill):
    try:
        from twilio.rest import Client
        client   = Client(TWILIO_SID, TWILIO_TOKEN)
        msg_body = bill if bill else "SmartTrolley Bill\nThank you for shopping!"
        msg      = client.messages.create(
            from_='whatsapp:+14155238886',
            to   =f'whatsapp:+91{phone}',
            body =msg_body,
        )
        print(f"Twilio sent: {msg.sid}")
        return True
    except Exception as e:
        print(f"Twilio error: {e}")
        return False


@app.route('/owner')
def owner_dashboard():
    logs = list(reversed(read_logs()))
    return render_template('owner.html', logs=logs, products=PRODUCTS)


# ─────────────────────────────────────────────────────────────────
#  PI FUSION — settings
# ─────────────────────────────────────────────────────────────────
WEIGHT_THRESHOLD   = 20     # grams — minimum delta to act on
WEIGHT_TOLERANCE   = 0.35   # 35% — weight matching tolerance
DETECTION_COOLDOWN = 5      # seconds before same item can be re-added
SESSION_TIMEOUT    = 600    # 10 min idle → auto-clear cart
ALERT_CLEAR_TIME   = 30     # seconds → auto-clear alerts

trolley_state = defaultdict(lambda: {
    'weight'         : 0.0,
    'prev_weight'    : 0.0,
    'baseline_weight': 0.0,
    'detections'     : [],
    'cart'           : [],
    'total'          : 0.0,
    'payment_mode'   : False,
    'fraud_alert'    : None,
    'theft_alert'    : None,
    'last_activity'  : _time.time(),
    'arduino_ok'     : False,
    'camera_ok'      : False,
    'fps'            : 0.0,
    'last_seen'      : 0,
    'cooldowns'      : {},
})


def best_weight_match(delta, detections):
    """Return detected item name whose expected weight best matches delta."""
    for det in detections:
        expected = PRODUCT_WEIGHTS.get(det.get('name', ''), 0)
        if expected > 0 and abs(delta - expected) <= expected * WEIGHT_TOLERANCE:
            return det['name']
    return None


def find_removed(delta, cart_items):
    """Return cart item name whose weight matches a removal delta."""
    for item in reversed(cart_items):
        expected = PRODUCT_WEIGHTS.get(item.get('name', ''), 0)
        if expected > 0 and abs(delta - expected) <= expected * WEIGHT_TOLERANCE:
            return item['name']
    return None


# ─────────────────────────────────────────────────────────────────
#  PI ENDPOINTS
# ─────────────────────────────────────────────────────────────────
@app.route('/pi/sensor_data', methods=['POST'])
def sensor_data():
    """
    Pi posts here every 0.5s.
    Payload: { trolley_id, weight, detections, arduino_ok, camera_ok, fps, timestamp }
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'no data'}), 400

    tid = data.get('trolley_id', 'trolley_01')
    ts  = _time.time()
    s   = trolley_state[tid]

    # Update hardware status
    s['arduino_ok'] = data.get('arduino_ok', False)
    s['camera_ok']  = data.get('camera_ok',  False)
    s['fps']        = data.get('fps', 0.0)
    s['detections'] = data.get('detections', [])
    s['last_seen']  = ts

    new_weight       = float(data.get('weight', data.get('weight_g', 0.0)))
    s['prev_weight'] = s['weight']
    s['weight']      = new_weight
    delta            = new_weight - s['baseline_weight']

    # ── Item ADDED ────────────────────────────────────────────
    if delta > WEIGHT_THRESHOLD:
        dets = s['detections']
        name = best_weight_match(delta, dets)
        if not name and dets:
            name = max(dets, key=lambda d: d.get('conf', 0)).get('name')

        if not name:
            s['theft_alert'] = {
                'message'  : f"Unknown item added! Weight +{delta:.0f}g, nothing detected.",
                'timestamp': ts,
                'delta'    : delta,
            }
        else:
            cooldowns = s['cooldowns']
            if ts - cooldowns.get(name, 0) >= DETECTION_COOLDOWN:
                if s['payment_mode']:
                    s['fraud_alert'] = {
                        'type'     : 'added',
                        'name'     : name,
                        'message'  : f"Item added during payment: {name}",
                        'timestamp': ts,
                    }
                    s['payment_mode'] = False
                else:
                    price = PRODUCTS.get(name, {}).get('price', 0.0)
                    s['cart'].append({
                        'id'       : f"{name}_{int(ts)}",
                        'name'     : name,
                        'price'    : price,
                        'weight_g' : round(delta, 1),
                        'timestamp': ts,
                    })
                    s['total']           = round(s['total'] + price, 2)
                    s['baseline_weight'] = new_weight
                    s['last_activity']   = ts
                    cooldowns[name]      = ts

    # ── Item REMOVED ──────────────────────────────────────────
    elif delta < -WEIGHT_THRESHOLD:
        removed_delta = abs(delta)
        if s['payment_mode']:
            s['fraud_alert'] = {
                'type'     : 'removed',
                'message'  : f"Item removed during payment! Weight -{removed_delta:.0f}g",
                'timestamp': ts,
            }
            s['payment_mode'] = False
        else:
            name = find_removed(removed_delta, s['cart'])
            if name:
                for i, item in enumerate(s['cart']):
                    if item['name'] == name:
                        removed = s['cart'].pop(i)
                        s['total']           = round(s['total'] - removed['price'], 2)
                        s['baseline_weight'] = new_weight
                        s['last_activity']   = ts
                        break
            else:
                s['baseline_weight'] = new_weight

    # ── Session timeout ───────────────────────────────────────
    if s['cart'] and not s['payment_mode'] and ts - s['last_activity'] > SESSION_TIMEOUT:
        s['cart']            = []
        s['total']           = 0.0
        s['baseline_weight'] = new_weight

    # ── Auto-clear old alerts ─────────────────────────────────
    for key in ('theft_alert', 'fraud_alert'):
        alert = s[key]
        if alert and ts - alert.get('timestamp', ts) > ALERT_CLEAR_TIME:
            s[key] = None

    return jsonify({'status': 'ok', 'cart_count': len(s['cart']), 'total': s['total']})


@app.route('/pi/trolley_status/<trolley_id>')
def trolley_status(trolley_id):
    """Dashboard polls this to get cart + alerts."""
    s = trolley_state[trolley_id]
    return jsonify({
        'trolley_id'      : trolley_id,
        'cart'            : s['cart'],
        'total'           : s['total'],
        'items_count'     : len(s['cart']),
        'payment_mode'    : s['payment_mode'],
        'fraud_alert'     : s['fraud_alert'],
        'theft_alert'     : s['theft_alert'],
        'weight_current'  : s['weight'],
        'weight_baseline' : s['baseline_weight'],
        'weight_delta'    : round(s['weight'] - s['baseline_weight'], 1),
        'detections'      : s['detections'],
        'arduino_ok'      : s['arduino_ok'],
        'camera_ok'       : s['camera_ok'],
        'fps'             : s['fps'],
        'online'          : (_time.time() - s['last_seen']) < 10,
    })


@app.route('/pi/set_payment_mode', methods=['POST'])
def pi_set_payment_mode():
    data = request.get_json(silent=True) or {}
    tid  = data.get('trolley_id', 'trolley_01')
    trolley_state[tid]['payment_mode']  = True
    trolley_state[tid]['last_activity'] = _time.time()
    return jsonify({'status': 'ok'})


@app.route('/pi/tare', methods=['POST'])
def pi_tare():
    data = request.get_json(silent=True) or {}
    tid  = data.get('trolley_id', 'trolley_01')
    trolley_state[tid]['baseline_weight'] = trolley_state[tid]['weight']
    return jsonify({'status': 'ok', 'baseline': trolley_state[tid]['baseline_weight']})


@app.route('/pi/clear_cart', methods=['POST'])
def pi_clear_cart():
    data = request.get_json(silent=True) or {}
    tid  = data.get('trolley_id', 'trolley_01')
    s    = trolley_state[tid]
    s['cart']            = []
    s['total']           = 0.0
    s['payment_mode']    = False
    s['fraud_alert']     = None
    s['theft_alert']     = None
    s['cooldowns']       = {}
    s['baseline_weight'] = s['weight']
    return jsonify({'status': 'ok'})


@app.route('/pi/reset/<trolley_id>', methods=['POST'])
def pi_reset(trolley_id):
    trolley_state.pop(trolley_id, None)
    return jsonify({'status': 'reset', 'trolley_id': trolley_id})


@app.route('/pi/checkout_fusion/<trolley_id>', methods=['POST'])
def pi_checkout_fusion(trolley_id):
    """Checkout a Pi-managed trolley — returns bill JSON and clears the cart."""
    s     = trolley_state[trolley_id]
    items = list(s['cart'])
    total = s['total']
    bill  = generate_bill(items, total)
    # Clear session
    s['cart']            = []
    s['total']           = 0.0
    s['payment_mode']    = False
    s['fraud_alert']     = None
    s['theft_alert']     = None
    s['cooldowns']       = {}
    s['baseline_weight'] = s['weight']
    return jsonify({
        'trolley_id': trolley_id,
        'cart'      : items,
        'total'     : total,
        'bill'      : bill,
        'paid_at'   : datetime.now().isoformat(),
    })


# ─────────────────────────────────────────────────────────────────
#  LIVE DASHBOARD  (auto-refreshes every 2s)
# ─────────────────────────────────────────────────────────────────
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>SmartTrolley Dashboard</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh}
header{background:#1e293b;padding:1rem 2rem;display:flex;align-items:center;gap:1rem;border-bottom:1px solid #334155}
header h1{font-size:1.35rem;color:#38bdf8}
.dot{width:10px;height:10px;border-radius:50%;background:#22c55e;display:inline-block;animation:blink 1.5s infinite}
.dot.off{background:#ef4444;animation:none}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
main{padding:2rem;max-width:1100px;margin:0 auto}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:1rem;margin-bottom:2rem}
.card{background:#1e293b;border-radius:12px;padding:1.1rem;border:1px solid #334155}
.card h3{font-size:.68rem;color:#94a3b8;text-transform:uppercase;letter-spacing:.07em;margin-bottom:.4rem}
.card p{font-size:1.8rem;font-weight:700;color:#f8fafc}
.card p span{font-size:.85rem;color:#94a3b8;font-weight:400}
.section{font-size:.72rem;color:#64748b;text-transform:uppercase;letter-spacing:.08em;margin:1.5rem 0 .6rem;display:block}
table{width:100%;border-collapse:collapse;background:#1e293b;border-radius:12px;overflow:hidden;margin-bottom:1rem}
th{background:#0f172a;padding:.65rem 1rem;text-align:left;font-size:.68rem;color:#94a3b8;text-transform:uppercase}
td{padding:.65rem 1rem;border-top:1px solid #334155;font-size:.86rem}
.ab{background:#1e293b;border-left:3px solid #ef4444;border-radius:8px;padding:.75rem 1rem;margin-top:.5rem;font-size:.8rem}
.ab.fraud{border-color:#f59e0b}
.hw{display:flex;gap:.4rem;flex-wrap:wrap;margin-top:.5rem}
.hw span{font-size:.68rem;padding:.18rem .55rem;border-radius:99px;font-weight:600}
.ok{background:#14532d;color:#86efac}
.err{background:#7f1d1d;color:#fca5a5}
.btn{border:none;padding:.5rem 1.2rem;border-radius:8px;cursor:pointer;font-size:.85rem;font-weight:600;margin-right:.4rem;margin-top:.4rem}
.blue{background:#2563eb;color:#fff}.blue:hover{background:#1d4ed8}
.red{background:#dc2626;color:#fff}.red:hover{background:#b91c1c}
.green{background:#16a34a;color:#fff}.green:hover{background:#15803d}
#ts{font-size:.68rem;color:#475569;margin-top:.6rem}
</style>
</head>
<body>
<header>
  <span class="dot" id="dot"></span>
  <h1>🛒 SmartTrolley Live Dashboard</h1>
</header>
<main>
  <div class="grid">
    <div class="card"><h3>Status</h3><p id="st">—</p></div>
    <div class="card"><h3>Cart Items</h3><p id="ic">—</p></div>
    <div class="card"><h3>Total</h3><p id="ct">—<span> ₹</span></p></div>
    <div class="card"><h3>Weight Now</h3><p id="wt">—<span> g</span></p></div>
    <div class="card"><h3>Weight Delta</h3><p id="wd">—<span> g</span></p></div>
    <div class="card"><h3>Hardware</h3>
      <div class="hw">
        <span id="hcam" class="err">Cam ✗</span>
        <span id="hard" class="err">Arduino ✗</span>
        <span id="hfps" class="err">0 FPS</span>
      </div>
    </div>
  </div>

  <span class="section">Cart</span>
  <table>
    <thead><tr><th>Item</th><th>Weight</th><th>Price</th><th>Added At</th></tr></thead>
    <tbody id="cart-body"><tr><td colspan="4" style="color:#475569">Waiting for data...</td></tr></tbody>
  </table>

  <span class="section">Alerts</span>
  <div id="alerts-box"><p style="color:#475569;font-size:.8rem">No alerts</p></div>

  <div style="margin-top:1.5rem">
    <button class="btn blue"  onclick="doCheckout()">✅ Checkout</button>
    <button class="btn green" onclick="doTare()">⚖️ Tare Scale</button>
    <button class="btn red"   onclick="doClear()">🗑️ Clear Cart</button>
    <button class="btn red"   onclick="doReset()">🔄 Reset Session</button>
  </div>
  <p id="ts"></p>
</main>

<script>
const TID = "trolley_01";

async function fetchStatus(){
  try{
    const d = await fetch("/pi/trolley_status/"+TID).then(r=>r.json());

    document.getElementById("dot").className = "dot"+(d.online?"" :" off");
    document.getElementById("st").textContent = d.online ? "🟢 Online" : "🔴 Offline";
    document.getElementById("ic").textContent = d.items_count;
    document.getElementById("ct").innerHTML   = d.total + "<span> ₹</span>";
    document.getElementById("wt").innerHTML   = d.weight_current.toFixed(1)+"<span> g</span>";
    const wd = d.weight_delta;
    document.getElementById("wd").innerHTML   = (wd>=0?"+":"")+wd+"<span> g</span>";

    const cam = document.getElementById("hcam");
    cam.textContent = d.camera_ok  ? "Cam ✓"    : "Cam ✗";
    cam.className   = d.camera_ok  ? "ok"        : "err";
    const ard = document.getElementById("hard");
    ard.textContent = d.arduino_ok ? "Arduino ✓" : "Arduino ✗";
    ard.className   = d.arduino_ok ? "ok"        : "err";
    document.getElementById("hfps").textContent = (d.fps||0).toFixed(1)+" FPS";

    const tb = document.getElementById("cart-body");
    tb.innerHTML = d.cart && d.cart.length
      ? d.cart.map(i=>`<tr>
          <td>${i.name}</td>
          <td>${i.weight_g||"?"}g</td>
          <td>₹${i.price}</td>
          <td>${i.timestamp ? new Date(i.timestamp*1000).toLocaleTimeString() : "—"}</td>
        </tr>`).join("")
      : '<tr><td colspan="4" style="color:#475569">Cart is empty</td></tr>';

    const ab = document.getElementById("alerts-box");
    const al = [];
    if(d.theft_alert) al.push(`<div class="ab">🚨 <strong>THEFT ALERT</strong> — ${d.theft_alert.message}<br><small style="color:#64748b">${new Date(d.theft_alert.timestamp*1000).toLocaleTimeString()}</small></div>`);
    if(d.fraud_alert) al.push(`<div class="ab fraud">⚠️ <strong>FRAUD</strong> — ${d.fraud_alert.message}<br><small style="color:#64748b">${new Date(d.fraud_alert.timestamp*1000).toLocaleTimeString()}</small></div>`);
    ab.innerHTML = al.length ? al.join("") : '<p style="color:#475569;font-size:.8rem">No alerts</p>';

    document.getElementById("ts").textContent = "Last updated: "+new Date().toLocaleTimeString();
  } catch(e){
    document.getElementById("st").textContent = "⚠️ Server error";
  }
}

async function doCheckout(){
  if(!confirm("Proceed to checkout?")) return;
  const d = await fetch("/pi/checkout_fusion/"+TID,{method:"POST"}).then(r=>r.json());
  const lines = (d.cart||[]).map(i=>`${i.name}  ₹${i.price}`).join("\\n");
  alert("✅ Checkout!\\n\\n"+lines+"\\n\\nTOTAL: ₹"+d.total+"\\n"+d.paid_at);
  fetchStatus();
}
async function doTare(){
  await fetch("/pi/tare",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({trolley_id:TID})});
  fetchStatus();
}
async function doClear(){
  if(!confirm("Clear cart?")) return;
  await fetch("/pi/clear_cart",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({trolley_id:TID})});
  fetchStatus();
}
async function doReset(){
  if(!confirm("Full reset?")) return;
  await fetch("/pi/reset/"+TID,{method:"POST"});
  fetchStatus();
}

fetchStatus();
setInterval(fetchStatus, 2000);
</script>
</body>
</html>
"""

@app.route('/dashboard')
def dashboard():
    return render_template_string(DASHBOARD_HTML)


# ─────────────────────────────────────────────────────────────────
#  HEALTH
# ─────────────────────────────────────────────────────────────────
@app.route('/health')
def health():
    return jsonify({
        'status'   : 'ok',
        'trolleys' : list(trolley_state.keys()),
        'products' : len(PRODUCTS),
    })


# ─────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)