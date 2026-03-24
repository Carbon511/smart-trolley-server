import cv2
import numpy as np
from flask import Flask, request, jsonify, render_template, Response
from ultralytics import YOLO
import threading, time, os
from logger import log_purchase, read_logs # Your existing files
from bill_generator import generate_bill # Your existing files

app = Flask(__name__)

# 1. LOAD THE LIGHTWEIGHT MODEL
try:
    model = YOLO("my_model.onnx", task="detect")
    print("AI Model loaded on Render!")
except:
    model = None

# 2. YOUR PRODUCTS & WEIGHTS
PRODUCTS = {
    "Sprite 1L": {"price": 60, "weight": 1050},
    "Maggi 140g": {"price": 25, "weight": 155},
    "krackjack 120g": {"price": 20, "weight": 135},
    "Book Record": {"price": 50, "weight": 300},
    # Add your other products here...
}

trolley = {"cart": [], "total": 0, "baseline": 0, "detections": [], "alert": None}
_frame = None
_lock = threading.Lock()

# 3. RECEIVE DATA & RUN FUSION
@app.route('/pi/frame/<tid>', methods=['POST'])
def receive_frame(tid):
    global _frame
    img_data = request.get_data()
    nparr = np.frombuffer(img_data, np.uint8)
    frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    
    if frame is not None and model:
        # RUN AI ON RENDER (Fast CPU)
        results = model.predict(frame, conf=0.45, verbose=False)
        trolley["detections"] = [model.names[int(b.cls[0])] for b in results[0].boxes]
        
        # Draw boxes for the website feed
        annotated = results[0].plot()
        _, buf = cv2.imencode('.jpg', annotated, [cv2.IMWRITE_JPEG_QUALITY, 50])
        with _lock: _frame = buf.tobytes()
    return "ok"

@app.route('/pi/sensor_data', methods=['POST'])
def receive_sensors():
    data = request.json
    weight = float(data.get('weight', 0))
    diff = weight - trolley["baseline"]

    # THE FUSION (Anti-Theft)
    if diff > 25: # If item added
        match = None
        for det in trolley["detections"]:
            if det in PRODUCTS:
                expected = PRODUCTS[det]["weight"]
                if abs(diff - expected) < (expected * 0.4): # Weight matches AI
                    match = det; break
        
        if match:
            trolley["cart"].append({"name": match, "price": PRODUCTS[match]["price"]})
            trolley["total"] += PRODUCTS[match]["price"]
            trolley["baseline"] = weight
            trolley["alert"] = None
        else:
            trolley["alert"] = "ANTI-THEFT: Item weight added but not recognized!"
            
    return "ok"

@app.route('/video_feed')
def video_feed():
    def gen():
        while True:
            with _lock:
                if _frame: yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + _frame + b'\r\n')
            time.sleep(0.05)
    return Response(gen(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/')
def index(): return render_template('index.html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)