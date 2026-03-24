import cv2
import numpy as np
from flask import Flask, request, jsonify, render_template, Response
import threading, time, os
from ultralytics import YOLO

# Import your custom logic files
from bill_generator import generate_bill
from logger import log_purchase, read_logs

app = Flask(__name__)

# 1. LOAD AI (ONNX task='detect' is most memory efficient)
try:
    model = YOLO("my_model.onnx", task="detect")
    print("AI Brain Ready on Render")
except Exception as e:
    print(f"AI Error: {e}")
    model = None

# 2. PRODUCT MASTER DATABASE (Ensure names match YOLO labels exactly)
PRODUCTS = {
    "Book Record":      {"price": 50,  "weight": 300, "stock": 100},
    "Sprite 1L":        {"price": 60,  "weight": 1050, "stock": 100},
    "Maggi 140g":       {"price": 25,  "weight": 155, "stock": 100},
    "krackjack 120g":   {"price": 20,  "weight": 135, "stock": 100},
    "Garnier Men":      {"price": 199, "weight": 120, "stock": 100},
}

# 3. LIVE SYSTEM STATE
trolley = {
    "cart": [], "total": 0, "weight": 0, "baseline": 0,
    "detections": [], "alert": None, "last_seen": 0
}
_frame = None
_lock = threading.Lock()

# 4. VISION ROUTE (From Pi)
@app.route('/pi/frame/trolley_01', methods=['POST'])
def receive_frame():
    global _frame
    img_data = request.get_data()
    nparr = np.frombuffer(img_data, np.uint8)
    frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    
    if frame is not None and model:
        # Inference on Render
        results = model.predict(frame, conf=0.45, verbose=False)
        trolley["detections"] = [model.names[int(b.cls[0])] for b in results[0].boxes]
        
        # Annotate for dashboard
        annotated = results[0].plot()
        _, buf = cv2.imencode('.jpg', annotated, [cv2.IMWRITE_JPEG_QUALITY, 50])
        with _lock: _frame = buf.tobytes()
    return "ok"

# 5. SENSOR FUSION & ANTI-THEFT
@app.route('/pi/sensor_data', methods=['POST'])
def receive_sensors():
    data = request.json
    weight = float(data.get('weight', 0))
    diff = weight - trolley["baseline"]
    trolley["weight"] = weight
    trolley["last_seen"] = time.time()

    if diff > 25: # Potential item added
        match = None
        for det in trolley["detections"]:
            if det in PRODUCTS:
                expected = PRODUCTS[det]["weight"]
                if abs(diff - expected) < (expected * 0.4): # Fusion Match
                    match = det; break
        
        if match:
            price = PRODUCTS[match]["price"]
            trolley["cart"].append({"name": match, "price": price})
            trolley["total"] += price
            trolley["baseline"] = weight # Update scale
            trolley["alert"] = None
            PRODUCTS[match]["stock"] -= 1 # Update Stock
        else:
            trolley["alert"] = "⚠️ THEFT ALERT: Weight added but item not recognized!"
            
    return "ok"

# 6. PAYMENT & WHATSAPP CHECKOUT
@app.route('/checkout', methods=['POST'])
def checkout():
    data = request.json
    phone = data.get('phone', '0000000000')
    if not trolley["cart"]: return jsonify({"status": "error"}), 400

    bill_msg = generate_bill(trolley["cart"], trolley["total"])
    log_purchase(phone, trolley["cart"], trolley["total"], data.get('payment_id', 'CASH'), "T-01")

    # WhatsApp Placeholder (Uncomment when Twilio is set)
    # send_whatsapp(phone, bill_msg)

    # Reset
    trolley["cart"] = []; trolley["total"] = 0; trolley["baseline"] = trolley["weight"]
    return jsonify({"status": "success", "bill": bill_msg})

# 7. TARE SCALE (Calibration)
@app.route('/api/tare', methods=['POST'])
def tare():
    trolley["baseline"] = trolley["weight"]
    trolley["alert"] = None
    return jsonify({"status": "ok"})

# 8. DASHBOARD ROUTES
@app.route('/owner')
def owner_dash():
    logs = list(reversed(read_logs()))
    return render_template('owner.html', logs=logs, products=PRODUCTS)

@app.route('/video_feed')
def video_feed():
    def gen():
        while True:
            with _lock:
                if _frame: yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + _frame + b'\r\n')
            time.sleep(0.05)
    return Response(gen(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/api/status')
def get_status():
    return jsonify(trolley)

@app.route('/')
def index(): return render_template('index.html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)