from flask import Flask, request, jsonify, render_template, make_response
from bill_generator import generate_bill
from products import PRODUCTS
import os
import requests as req
import hashlib
import hmac
from datetime import datetime

app = Flask(__name__)

cart = []
current_weight = 0

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
print(f"Razorpay key: {'set' if RZP_KEY_ID else 'MISSING'}")
print(f"Wati: {'set' if WATI_API_URL else 'MISSING'}")
print(f"Twilio: {'set' if TWILIO_SID else 'MISSING'}")

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
        'razorpay': 'set' if RZP_KEY_ID else 'missing',
        'wati': 'set' if WATI_API_URL else 'missing',
        'twilio': 'set' if TWILIO_SID else 'missing',
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

@app.route('/create_order', methods=['POST'])
def create_order():
    try:
        data = request.json or {}
        frontend_amount = int(float(data.get('amount', 0)))
        server_total = sum(item['price'] for item in cart)
        total = server_total if server_total > 0 else frontend_amount
        print(f"create_order: server={server_total} frontend={frontend_amount} using={total}")
        if total <= 0:
            return jsonify({'status': 'error', 'message': 'Amount is zero'}), 400
        client = get_rzp()
        if client:
            order = client.order.create({
                'amount': int(total) * 100,
                'currency': 'INR',
                'payment_capture': 1
            })
            print(f"Razorpay order created: {order['id']}")
            return jsonify({
                'status': 'success',
                'order_id': order['id'],
                'amount': total,
                'key_id': RZP_KEY_ID
            })
        else:
            return jsonify({
                'status': 'success',
                'order_id': f'demo_{total}',
                'amount': total,
                'key_id': 'demo'
            })
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
        data = request.json or {}
        phone = str(data.get('phone', '')).strip()
        items = data.get('items', None)
        total = data.get('total', None)
        payment_id = str(data.get('payment_id', ''))
        trolley = str(data.get('trolley', 'T-0000'))

        print(f"Raw phone received: '{phone}'")

        if not phone:
            return jsonify({'status': 'error', 'message': 'No phone'}), 400

        # Clean phone — remove all non-digits
        import re
        phone = re.sub(r'\D', '', phone)
        print(f"After removing non-digits: '{phone}'")

        # Remove 91 prefix if present
        if phone.startswith('91') and len(phone) == 12:
            phone = phone[2:]

        # Take last 10 digits
        phone = phone[-10:]
        print(f"Final phone: '{phone}' length: {len(phone)}")

        if len(phone) != 10:
            return jsonify({'status': 'error', 'message': f'Invalid number: {phone}'}), 400

        bill_items = items if items else cart
        bill_total = int(float(total)) if total else sum(i['price'] for i in bill_items)
        bill = generate_bill(bill_items, bill_total)

        print(f"Sending bill to {phone} total=Rs.{bill_total} items={len(bill_items)}")
        print(f"Bill text length: {len(bill)}")

        # Try Wati first
        if WATI_API_URL and WATI_API_TOKEN:
            if send_via_wati(phone, bill,
                             cart_items=bill_items,
                             total=bill_total,
                             trolley=trolley,
                             payment_id=payment_id):
                return jsonify({'status': 'success', 'method': 'wati'})

        # Fallback Twilio
        if TWILIO_SID and TWILIO_TOKEN:
            if send_via_twilio(phone, bill):
                return jsonify({'status': 'success', 'method': 'twilio'})

        return jsonify({'status': 'error', 'message': 'Both failed'}), 500

    except Exception as e:
        print(f"checkout ERROR: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

def send_via_wati(phone, bill, cart_items=None, total=0, trolley="", payment_id=""):
    try:
        base = WATI_API_URL.rstrip('/')
        date_str = datetime.now().strftime("%d %b %Y, %I:%M %p")

        if cart_items:
            items_text = "\n".join([f"* {i.get('name','?')} - Rs.{i.get('price',0)}" for i in cart_items])
        else:
            items_text = "Items purchased"

        # Method 1 — approved template (whatsappNumber as QUERY PARAM)
        headers = {                                          # ← was missing entirely
            'Authorization': f'Bearer {WATI_API_TOKEN}',
            'Content-Type': 'application/json'
        }
        for num_format in [f"91{phone}", phone]:             # ← indented inside try
            url = f"{base}/api/v1/sendTemplateMessage?whatsappNumber={num_format}"
            payload = {
                "template_name": "smart_trolley_bill_receipt",
                "broadcast_name": "SmartTrolley_Bill",
                "parameters": [
                    {"name": "1", "value": str(trolley or "T-0000")},
                    {"name": "2", "value": str(phone)},
                    {"name": "3", "value": str(date_str)},
                    {"name": "4", "value": str(items_text)},
                    {"name": "5", "value": str(total)},
                    {"name": "6", "value": str(payment_id or "N/A")}
                ]
            }
            r = req.post(url, json=payload, headers=headers, timeout=10)
            print(f"Wati template [{num_format}]: {r.status_code} {r.text[:300]}")
            try:
                result = r.json()
                if result.get('result') == True:
                    print(f"Wati SUCCESS with: {num_format}")
                    return True
            except:
                pass

        # Method 2 — session message
        headers2 = {
            'Authorization': f'Bearer {WATI_API_TOKEN}',
            'Content-Type': 'application/json-patch+json'
        }
        msg_text = bill if bill and len(bill) > 0 else f"SmartTrolley Bill\nTotal: Rs.{total}\nThank you!"
        url2 = f"{base}/api/v1/sendSessionMessage/91{phone}"
        r2 = req.post(url2, json={'messageText': msg_text}, headers=headers2, timeout=10)
        print(f"Wati session: {r2.status_code} {r2.text[:200]}")
        if r2.status_code == 200:
            try:
                result2 = r2.json()
                if result2.get('result') == True:
                    return True
            except:
                pass

        return False

    except Exception as e:
        print(f"Wati error: {e}")
        return False
def send_via_twilio(phone, bill):
    try:
        from twilio.rest import Client
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        msg_body = bill if bill else f"SmartTrolley Bill\nThank you for shopping!"
        msg = client.messages.create(
            from_='whatsapp:+14155238886',
            to=f'whatsapp:+91{phone}',
            body=msg_body
        )
        print(f"Twilio sent: {msg.sid}")
        return True
    except Exception as e:
        print(f"Twilio error: {e}")
        return False

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)