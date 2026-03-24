import cv2
import numpy as np
from flask import Flask, request, jsonify, render_template, Response
import threading, time, os, requests
from datetime import datetime
from ultralytics import YOLO

# Import your custom logic
from bill_generator import generate_bill
from logger import log_purchase, read_logs

app = Flask(__name__)

# --- 1. CONFIGURATION ---
WATI_API_URL = "https://live-server.wati.io" # Replace with your WATI API URL
WATI_TOKEN = "your_token"                     # Replace with your WATI Token
TROLLEY_ID_CODE = "T-4470"

# --- 2. AI LOAD (MEMORY OPTIMIZED) ---
try:
    # task='detect' is critical to stay under 512MB RAM on Render
    model = YOLO("my_model.onnx", task="detect")
    print("✅ AI Brain Ready (Lighter Engine)")
except Exception as e:
    print(f"❌ AI Load Error: {e}")
    model = None

# --- 3. COMPLETE PRODUCT MASTER LIST ---
# Ensure these names match your YOLO labels exactly!
PRODUCTS = {
    "Book Record":      {"price": 50,  "weight": 300, "stock": 100},
    "Brustro Pencils":  {"price": 120, "weight": 150, "stock": 100},
    "Garnier Men":      {"price": 199, "weight": 120, "stock": 100},
    "krackjack 120g":   {"price": 20,  "weight": 135, "stock": 100},
    "krackjack 180g":   {"price": 30,  "weight": 195, "stock": 100},
    "Sprite 1L":        {"price": 60,  "weight": 1050, "stock": 100},
    "Sprite 400ml":     {"price": 30,  "weight": 420, "stock": 100},
    "Maggi 140g":       {"price": 25,  "weight": 155, "stock": 100},
    "Maggi 280g":       {"price": 50,  "weight": 300, "stock": 100},
    "iva Liquid 500ml": {"price": 99,  "weight": 520, "stock": 100},
    "Globe Cream":      {"price": 85,  "weight": 80,  "stock": 100},
    "Soudal paint":     {"price": 250, "weight": 400, "stock": 100},
    "Dr wash 190g":     {"price": 45,  "weight": 210, "stock": 100},
    "Fresh& Fruity":    {"price": 30,  "weight": 100, "stock": 100},
}

# --- 4. LIVE SYSTEM STATE ---
trolley = {
    "cart": [], "total": 0, "weight": 0, "baseline": 0, 
    "detections": [], "alert": None, "last_seen": 0
}
_frame = None
_lock = threading.Lock()

# --- 5. ROUTES ---

@app.route('/pi/frame/trolley_01', methods=['POST'])
def receive_frame():
    global _frame
    img_data = request.get_data()
    nparr = np.frombuffer(img_data, np.uint8)
    frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if frame is not None and model:
        results = model.predict(frame, conf=0.45, verbose=False)
        trolley["detections"] = [model.names[int(b.cls[0])] for b in results[0].boxes]
        annotated = results[0].plot()
        _, buf = cv2.imencode('.jpg', annotated, [cv2.IMWRITE_JPEG_QUALITY, 50])
        with _lock: _frame = buf.tobytes()
    return "ok"

@app.route('/pi/sensor_data', methods=['POST'])
def receive_sensors():
    data = request.json
    weight = float(data.get('weight', 0))
    diff = weight - trolley["baseline"]
    trolley["weight"] = weight

    # FUSION LOGIC (Anti-Theft)
    if diff > 25: 
        match = None
        for det in trolley["detections"]:
            if det in PRODUCTS:
                expected_w = PRODUCTS[det]["weight"]
                if abs(diff - expected_w) < (expected_w * 0.4): # 40% Tolerance
                    match = det; break
        if match:
            price = PRODUCTS[match]["price"]
            trolley["cart"].append({"name": match, "price": price})
            trolley["total"] += price
            trolley["baseline"] = weight
            trolley["alert"] = None
            PRODUCTS[match]["stock"] -= 1 # Update Stock
        else:
            trolley["alert"] = "⚠️ THEFT ALERT: Item added but not recognized!"
    return "ok"

@app.route('/checkout', methods=['POST'])
def checkout():
    data = request.json
    phone = data.get('phone', '0000000000')
    payment_id = data.get('payment_id', 'Razorpay_Demo')
    if not trolley["cart"]: return jsonify({"status": "error"}), 400

    items_text = "\n".join([f"• {i['name']} - ₹{i['price']}" for i in trolley["cart"]])
    total_val = str(trolley["total"])
    now_time = datetime.now().strftime("%d %b %Y, %I:%M %p")

    # WATI Template Parameters {{1}} to {{6}}
    headers = {"Authorization": f"Bearer {WATI_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "template_name": "smart_trolley_bill_receipt",
        "broadcast_name": "SmartTrolley_Receipt",
        "parameters": [
            {"name": "1", "value": TROLLEY_ID_CODE}, 
            {"name": "2", "value": phone},           
            {"name": "3", "value": now_time},        
            {"name": "4", "value": items_text},      
            {"name": "5", "value": total_val},       
            {"name": "6", "value": payment_id}       
        ]
    }
    wati_url = f"{WATI_API_URL}/api/v1/sendTemplateMessage?whatsappNumber=91{phone}"
    try: requests.post(wati_url, json=payload, headers=headers)
    except: pass

    log_purchase(phone, trolley["cart"], trolley["total"], payment_id, TROLLEY_ID_CODE)
    trolley["cart"] = []; trolley["total"] = 0; trolley["baseline"] = trolley["weight"]; trolley["alert"] = None
    return jsonify({"status": "success"})

@app.route('/video_feed')
def video_feed():
    def gen():
        while True:
            with _lock:
                if _frame: yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + _frame + b'\r\n')
            time.sleep(0.05)
    return Response(gen(), mimetype='multipart/x-mixed-replace; boundary=frame', headers={'X-Accel-Buffering': 'no'})

@app.route('/api/status')
def get_status(): return jsonify(trolley)

@app.route('/owner')
def owner():
    return render_template('owner.html', logs=list(reversed(read_logs())), products=PRODUCTS)

@app.route('/ping')
def ping(): return jsonify({"status": "alive"}), 200

@app.route('/')
def index(): return render_template('index.html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)