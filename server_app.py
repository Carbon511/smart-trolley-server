import cv2
import numpy as np
from flask import Flask, request, jsonify, render_template, Response
from bill_generator import generate_bill
from logger import log_purchase, read_logs
import os
import threading
import time as _time
from collections import defaultdict
from ultralytics import YOLO

app = Flask(__name__)

# 1. LOAD AI (Render has 512MB RAM - YOLOv8 Nano is recommended)
try:
    model = YOLO("my_model.pt") 
    print("AI Model loaded on Render!")
except Exception as e:
    print(f"Model Load Error: {e}")
    model = None

# 2. PRODUCT DATABASE
PRODUCTS = {
    "Book Record":      {"price": 50,  "weight": 300},
    "Brustro Pencils":  {"price": 120, "weight": 150},
    "Garnier Men":      {"price": 199, "weight": 120},
    "krackjack 120g":   {"price": 20,  "weight": 135},
    "krackjack 180g":   {"price": 30,  "weight": 195},
    "Sprite 1L":        {"price": 60,  "weight": 1050},
    "Sprite 400ml":     {"price": 30,  "weight": 420},
    "Maggi 140g":       {"price": 25,  "weight": 155},
    "Maggi 280g":       {"price": 50,  "weight": 300},
    "iva Liquid 500ml": {"price": 99,  "weight": 520},
    "Globe Cream":      {"price": 85,  "weight": 80},
    "Soudal paint":     {"price": 250, "weight": 400},
}

# 3. GLOBAL STORAGE
trolley_state = defaultdict(lambda: {
    'weight': 0.0, 'baseline': 0.0, 'detections': [],
    'cart': [], 'total': 0.0, 'last_seen': 0
})
_frame_store = {}
_frame_lock = threading.Lock()

@app.route('/owner')
def owner_dashboard():
    return render_template('owner.html', logs=list(reversed(read_logs())), products=PRODUCTS)

# 4. THE AI ENGINE (Runs on Render)
@app.route('/pi/frame/<trolley_id>', methods=['POST'])
def receive_frame(trolley_id):
    jpg_data = request.get_data()
    if not jpg_data or model is None: return jsonify({'status': 'err'}), 200
    
    # Decode image
    nparr = np.frombuffer(jpg_data, np.uint8)
    frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    
    if frame is not None:
        # Run YOLO Inference on Render CPU
        results = model.predict(frame, conf=0.4, verbose=False)
        
        # Extract detections for Fusion
        current_dets = []
        for box in results[0].boxes:
            name = model.names[int(box.cls[0])]
            current_dets.append({'name': name})
        
        trolley_state[trolley_id]['detections'] = current_dets

        # Draw AI Boxes for the Website Feed
        annotated = results[0].plot()
        _, buf = cv2.imencode('.jpg', annotated, [cv2.IMWRITE_JPEG_QUALITY, 60])
        with _frame_lock:
            _frame_store[trolley_id] = buf.tobytes()
            
    return jsonify({'status': 'ok'}), 200

# 5. THE FUSION LOGIC (Weight + Vision)
@app.route('/pi/sensor_data', methods=['POST'])
def sensor_data():
    data = request.get_json()
    tid = data.get('trolley_id', 'trolley_01')
    s = trolley_state[tid]
    s['last_seen'] = _time.time()
    
    new_weight = float(data.get('weight', 0))
    delta = new_weight - s['baseline']
    s['weight'] = new_weight

    # If weight increases by more than 20g
    if delta > 20: 
        best_match = None
        # Compare weight change to things AI currently sees
        for det in s['detections']:
            name = det['name']
            if name in PRODUCTS:
                expected = PRODUCTS[name]['weight']
                # If weight matches within 30% tolerance
                if abs(delta - expected) < (expected * 0.3):
                    best_match = name
                    break
        
        if best_match:
            price = PRODUCTS[best_match]['price']
            s['cart'].append({'name': best_match, 'price': price})
            s['total'] += price
            s['baseline'] = new_weight # Update baseline
            print(f"Fusion Success: Added {best_match}")

    return jsonify({'status': 'ok', 'total': s['total']})

@app.route('/pi/video_feed/<trolley_id>')
def video_feed(trolley_id):
    def gen():
        while True:
            with _frame_lock:
                jpg = _frame_store.get(trolley_id)
            if jpg:
                yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + jpg + b'\r\n')
            _time.sleep(0.04) # ~25 FPS
    return Response(gen(), mimetype='multipart/x-mixed-replace; boundary=frame')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)