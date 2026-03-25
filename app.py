"""
SmartTrolley Server — Render (Flask + ONNX)
===========================================
ZERO LOAD ON PI: All inference runs HERE on the server.

WhatsApp Priority:
  1. WATI  (tries first — set WATI_API_URL + WATI_API_TOKEN)
  2. Twilio (fallback — set TWILIO_SID + TWILIO_TOKEN)

Pi → Server:
  POST /pi/frame/<id>        — raw JPEG frame (ONNX inference here)
  POST /pi/sensor_data       — weight + Arduino status

Server → Dashboard:
  GET  /api/status/<id>      — JSON (cart, weight, alerts, fps, detections)
  GET  /api/frame/<id>       — annotated JPEG
  POST /api/checkout/<id>    — signal checkout (freezes cart)
  POST /api/clear/<id>       — clear cart after payment
  POST /api/remove_item/<id> — remove item by id

Pi polls:
  GET  /pi/commands/<id>     — pending command (one-shot)

Payment & Billing:
  POST /create_order         — Razorpay order creation
  POST /checkout             — log purchase + send WhatsApp bill
  GET  /owner                — owner dashboard
  GET  /dashboard            — serve index.html
  GET  /health               — server health check
  GET  /ping                 — uptime check (UptimeRobot)
"""

import io
import os
import time
import logging
import threading
from queue import Queue, Empty

import numpy as np
import cv2
import requests as http_requests
from flask import (
    Flask, request, jsonify, send_file,
    redirect, send_from_directory, render_template_string
)

# ── Optional imports ─────────────────────────────────────────
try:
    import onnxruntime as ort
    ONNX_AVAILABLE = True
except ImportError:
    ONNX_AVAILABLE = False

try:
    import razorpay
    RZP_AVAILABLE = True
except ImportError:
    RZP_AVAILABLE = False

try:
    from twilio.rest import Client as TwilioClient
    TWILIO_AVAILABLE = True
except ImportError:
    TWILIO_AVAILABLE = False

from products import PRODUCTS
from logger import log_purchase, read_logs
from bill_generator import generate_bill_text_simple

# ═══════════════════════════════════════════════════════════════
#  CONFIGURATION  —  set all secrets as Render env variables
# ═══════════════════════════════════════════════════════════════

PORT           = int(os.environ.get("PORT", 5000))
STATUS_MAX_AGE = 30      # seconds before Pi marked offline

# Detection tuning
MIN_CONF        = float(os.environ.get("MIN_CONF",        "0.55"))
CONFIRM_FRAMES  = int(os.environ.get("CONFIRM_FRAMES",    "8"))
REMOVE_FRAMES   = int(os.environ.get("REMOVE_FRAMES",     "20"))
THEFT_THRESHOLD = float(os.environ.get("THEFT_THRESH",    "350"))  # grams

# Razorpay
RZP_KEY_ID     = os.environ.get("RAZORPAY_KEY_ID",     "")
RZP_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")

# ── WATI WhatsApp Business API ────────────────────────────────
#   Render env vars to set:
#     WATI_API_URL   = https://live-mt-server.wati.io/YOUR_ID   (no trailing slash)
#     WATI_API_TOKEN = your_wati_bearer_token
WATI_API_URL   = os.environ.get("WATI_API_URL",   "")
WATI_API_TOKEN = os.environ.get("WATI_API_TOKEN", "")

# ── Twilio WhatsApp Sandbox (fallback) ────────────────────────
#   Render env vars to set:
#     TWILIO_SID     = ACxxxxxxxxxxxxxxxxx
#     TWILIO_TOKEN   = your_twilio_auth_token
#     TWILIO_WA_FROM = whatsapp:+14155238886
TWILIO_SID    = os.environ.get("TWILIO_SID",     "")
TWILIO_TOKEN  = os.environ.get("TWILIO_TOKEN",   "")
WA_FROM       = os.environ.get("TWILIO_WA_FROM", "whatsapp:+14155238886")

# ONNX model
MODEL_PATH  = os.environ.get("MODEL_PATH", "model.onnx")

# Class names — must match your YOLO training label order exactly
CLASS_NAMES = os.environ.get("CLASS_NAMES", (
    "Book Record,Brustro Pencils,Garnier Men,"
    "Krackjack 120g,Krackjack 180g,Sprite 1L,"
    "Sprite 400ml,Maggi 140g,Maggi 280g,"
    "Iva Liquid 500ml,Globe Cream,Soudal Paint"
)).split(",")
CLASS_NAMES = [c.strip() for c in CLASS_NAMES]

# Expected item weights (grams) — used for anti-theft
PRODUCT_WEIGHTS_G = {
    "Book Record":      200,
    "Brustro Pencils":   80,
    "Garnier Men":      150,
    "Krackjack 120g":   125,
    "Krackjack 180g":   185,
    "Sprite 1L":       1080,
    "Sprite 400ml":     430,
    "Maggi 140g":       145,
    "Maggi 280g":       285,
    "Iva Liquid 500ml": 530,
    "Globe Cream":       95,
    "Soudal Paint":     310,
}

BOX_COLOR  = (34, 197, 94)
TEXT_COLOR = (255, 255, 255)
LABEL_BG   = (26, 107, 74)

# ═══════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("SmartTrolley")


# ═══════════════════════════════════════════════════════════════
#  WHATSAPP — WATI first, Twilio fallback
# ═══════════════════════════════════════════════════════════════

def send_whatsapp_bill(phone, items, total, payment_id, trolley):
    """
    Try WATI first. If WATI is not configured or fails, try Twilio.
    Returns (provider_used: str, success: bool)
    """
    bill_text = generate_bill_text_simple(items, total)

    # ── Try WATI ─────────────────────────────────────────────
    if WATI_API_URL and WATI_API_TOKEN:
        log.info("📤 Trying WATI …")
        if _send_via_wati(phone, bill_text):
            return "wati", True
        log.warning("WATI failed — falling back to Twilio …")

    # ── Try Twilio ────────────────────────────────────────────
    if TWILIO_AVAILABLE and TWILIO_SID and TWILIO_TOKEN:
        log.info("📤 Trying Twilio …")
        if _send_via_twilio(phone, bill_text):
            return "twilio", True

    log.error("❌ Both WATI and Twilio unavailable / failed")
    return "none", False


def _clean_phone(phone):
    """Strip country code and leading zeros — return 10 digit string."""
    p = str(phone).strip().lstrip("+")
    if p.startswith("91") and len(p) == 12:
        p = p[2:]
    p = p.lstrip("0")
    return p


def _send_via_wati(phone, bill_text):
    try:
        clean = _clean_phone(phone)
        if len(clean) != 10:
            log.error(f"WATI: bad phone '{phone}' → '{clean}'")
            return False

        url = f"{WATI_API_URL.rstrip('/')}/api/v1/sendSessionMessage/91{clean}"
        headers = {
            "Authorization": f"Bearer {WATI_API_TOKEN}",
            "Content-Type":  "application/json",
        }
        r = http_requests.post(
            url,
            headers=headers,
            json={"messageText": bill_text},
            timeout=10
        )
        if r.status_code == 200:
            log.info(f"✅ WATI bill sent → +91{clean}")
            return True
        log.error(f"WATI HTTP {r.status_code}: {r.text[:300]}")
        return False
    except Exception as e:
        log.error(f"WATI exception: {e}")
        return False


def _send_via_twilio(phone, bill_text):
    try:
        clean  = _clean_phone(phone)
        client = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
        msg    = client.messages.create(
            from_=WA_FROM,
            to=f"whatsapp:+91{clean}",
            body=bill_text,
        )
        log.info(f"✅ Twilio bill sent → +91{clean}  sid={msg.sid}")
        return True
    except Exception as e:
        log.error(f"Twilio exception: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
#  ONNX MODEL
# ═══════════════════════════════════════════════════════════════
_session    = None
_input_name = None
_input_size = 640


def _load_model():
    global _session, _input_name, _input_size
    if not ONNX_AVAILABLE:
        log.warning("⚠️  onnxruntime not installed — detection disabled")
        return
    if not os.path.exists(MODEL_PATH):
        log.warning(f"⚠️  '{MODEL_PATH}' not found — detection disabled")
        log.warning("    Upload model.onnx to your Render repo root")
        return
    try:
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 2
        opts.intra_op_num_threads = 2
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        _session    = ort.InferenceSession(
            MODEL_PATH, sess_options=opts,
            providers=["CPUExecutionProvider"]
        )
        _input_name = _session.get_inputs()[0].name
        _input_size = _session.get_inputs()[0].shape[2]
        log.info(
            f"✅ ONNX loaded  input={_input_size}px  "
            f"classes={len(CLASS_NAMES)}: {CLASS_NAMES[:4]} …"
        )
    except Exception as e:
        log.error(f"❌ Model load failed: {e}")


def _preprocess(frame):
    img = cv2.resize(frame, (_input_size, _input_size))
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    return np.expand_dims(np.transpose(img, (2, 0, 1)), axis=0)


def _postprocess(outputs, orig_h, orig_w):
    raw       = np.squeeze(outputs[0], axis=0).T     # [8400, 4+nc]
    scores    = raw[:, 4:]
    class_ids = np.argmax(scores, axis=1)
    confs     = np.max(scores, axis=1)
    mask      = confs >= MIN_CONF
    if not np.any(mask):
        return []
    boxes_xy  = raw[mask, :4]
    class_ids = class_ids[mask]
    confs     = confs[mask]
    sx = orig_w / _input_size
    sy = orig_h / _input_size
    x1 = (boxes_xy[:, 0] - boxes_xy[:, 2] / 2) * sx
    y1 = (boxes_xy[:, 1] - boxes_xy[:, 3] / 2) * sy
    x2 = (boxes_xy[:, 0] + boxes_xy[:, 2] / 2) * sx
    y2 = (boxes_xy[:, 1] + boxes_xy[:, 3] / 2) * sy
    nms_in = np.stack([x1, y1, x2 - x1, y2 - y1], axis=1).tolist()
    idxs   = cv2.dnn.NMSBoxes(nms_in, confs.tolist(), MIN_CONF, 0.45)
    result = []
    if len(idxs) > 0:
        for i in np.array(idxs).flatten():
            cid  = int(class_ids[i])
            name = CLASS_NAMES[cid] if cid < len(CLASS_NAMES) else f"cls{cid}"
            result.append({
                "name": name,
                "conf": float(confs[i]),
                "box":  [float(x1[i]), float(y1[i]),
                         float(x2[i]), float(y2[i])],
            })
    return result


def _annotate(frame, detections):
    for det in detections:
        x1, y1, x2, y2 = [int(v) for v in det["box"]]
        label = f"{det['name']} {det['conf']*100:.0f}%"
        cv2.rectangle(frame, (x1, y1), (x2, y2), BOX_COLOR, 2)
        (tw, th), _ = cv2.getTextSize(
            label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1
        )
        cv2.rectangle(
            frame, (x1, y1 - th - 6), (x1 + tw + 4, y1), LABEL_BG, -1
        )
        cv2.putText(
            frame, label, (x1 + 2, y1 - 4),
            cv2.FONT_HERSHEY_SIMPLEX, 0.5, TEXT_COLOR, 1, cv2.LINE_AA
        )
    return frame


def run_inference(frame_bytes):
    arr   = np.frombuffer(frame_bytes, np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if frame is None:
        return frame_bytes, []
    detections = []
    if _session is not None:
        try:
            tensor     = _preprocess(frame)
            outputs    = _session.run(None, {_input_name: tensor})
            detections = _postprocess(outputs, frame.shape[0], frame.shape[1])
        except Exception as e:
            log.error(f"Inference error: {e}")
    annotated = _annotate(frame.copy(), detections)
    _, buf    = cv2.imencode(
        ".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 80]
    )
    return buf.tobytes(), detections


# ═══════════════════════════════════════════════════════════════
#  TROLLEY STORE
# ═══════════════════════════════════════════════════════════════
store      = {}
store_lock = threading.Lock()


def get_trolley(tid: str) -> dict:
    with store_lock:
        if tid not in store:
            store[tid] = _fresh_trolley()
        return store[tid]


def _fresh_trolley() -> dict:
    return {
        "status": {
            "cart":        [],
            "total":       0,
            "weight_g":    0,
            "arduino_ok":  False,
            "fps":         0,
            "pi_fps":      0,
            "detections":  [],
            "fraud_alert": None,
            "theft_alert": None,
        },
        "frame":          None,
        "frame_ts":       0,
        "command":        None,
        "_rx_ts":         0,
        "_payment_mode":  False,
        "_det_buffer":    {},
        "_absent_buffer": {},
        "_fps_history":   [],
        "_next_item_id":  1,
    }


# ═══════════════════════════════════════════════════════════════
#  INFERENCE WORKER (background thread)
# ═══════════════════════════════════════════════════════════════
_infer_queue: Queue = Queue(maxsize=4)


def _inference_worker():
    while True:
        try:
            tid, raw_bytes = _infer_queue.get(timeout=1)
        except Empty:
            continue
        try:
            annotated, detections = run_inference(raw_bytes)
            _update_from_detections(tid, annotated, detections)
        except Exception as e:
            log.error(f"Worker [{tid}]: {e}")


def _update_from_detections(tid, annotated_frame, detections):
    entry  = get_trolley(tid)
    status = entry["status"]

    with store_lock:
        entry["frame"]    = annotated_frame
        entry["frame_ts"] = time.time()

        # FPS
        now = time.time()
        h   = entry["_fps_history"]
        h.append(now)
        if len(h) > 20:
            h.pop(0)
        if len(h) >= 2:
            status["fps"] = round(len(h) / (h[-1] - h[0]), 1)

        status["detections"] = [
            {"name": d["name"], "conf": d["conf"]} for d in detections
        ]

        if entry["_payment_mode"]:
            _check_payment_fraud(entry, detections, status)
            return

        seen_now = {d["name"] for d in detections if d["conf"] >= MIN_CONF}
        db = entry["_det_buffer"]
        ab = entry["_absent_buffer"]

        for name in seen_now:
            db[name] = db.get(name, 0) + 1
            ab[name] = 0

        all_tracked = set(db.keys()) | set(ab.keys())
        for name in all_tracked - seen_now:
            ab[name] = ab.get(name, 0) + 1
            db[name] = max(0, db.get(name, 0) - 1)

        cart       = status["cart"]
        cart_names = [i["name"] for i in cart]

        # ADD items
        for name in seen_now:
            if db[name] >= CONFIRM_FRAMES and name not in cart_names:
                product = _find_product(name)
                if product:
                    item_id = f"{name[:4].lower()}-{entry['_next_item_id']}"
                    entry["_next_item_id"] += 1
                    cart.append({
                        "id":    item_id,
                        "name":  product["name"],
                        "price": product["price"],
                    })
                    status["total"] = sum(i["price"] for i in cart)
                    log.info(f"[{tid}] ➕ {product['name']} ₹{product['price']}")

        # REMOVE items (vision + weight confirmation)
        weight_g = status.get("weight_g", 0)
        for name in list(all_tracked):
            if ab.get(name, 0) >= REMOVE_FRAMES and name in cart_names:
                expected_w = sum(
                    PRODUCT_WEIGHTS_G.get(i["name"], 200)
                    for i in cart if i["name"] != name
                )
                if weight_g <= expected_w + THEFT_THRESHOLD:
                    cart = [i for i in cart if i["name"] != name]
                    status["total"] = sum(i["price"] for i in cart)
                    log.info(f"[{tid}] ➖ {name}")
                    db.pop(name, None)
                    ab.pop(name, None)

        status["cart"] = cart
        _check_theft(tid, entry, status)


def _check_theft(tid, entry, status):
    weight_g = status.get("weight_g", 0)
    if weight_g < 50:
        status["theft_alert"] = None
        return
    expected = sum(
        PRODUCT_WEIGHTS_G.get(i["name"], 200) for i in status["cart"]
    )
    excess = weight_g - expected
    if excess > THEFT_THRESHOLD and len(status["cart"]) > 0:
        msg = (
            f"⚠️ Weight mismatch! Scale={weight_g:.0f}g "
            f"Cart≈{expected:.0f}g ({excess:.0f}g unaccounted)"
        )
        status["theft_alert"] = {"message": msg, "timestamp": time.time()}
        log.warning(f"[{tid}] THEFT: {msg}")
    else:
        status["theft_alert"] = None


def _check_payment_fraud(entry, detections, status):
    seen_names = {d["name"] for d in detections if d["conf"] >= MIN_CONF}
    cart_names = {i["name"] for i in status["cart"]}
    new_items  = seen_names - cart_names
    if new_items:
        status["fraud_alert"] = {
            "message":   f"🚨 Item added during payment: {', '.join(new_items)}!",
            "timestamp": time.time(),
        }


def _find_product(detection_name):
    dn = detection_name.lower()
    for key, val in PRODUCTS.items():
        if key.lower() == dn or val["name"].lower() == dn:
            return val
    for key, val in PRODUCTS.items():
        if dn in key.lower() or dn in val["name"].lower():
            return val
    return None


# ═══════════════════════════════════════════════════════════════
#  FLASK APP
# ═══════════════════════════════════════════════════════════════
app = Flask(__name__, template_folder="templates")


# ── Ping / Uptime check ───────────────────────────────────────

@app.route("/ping", methods=["GET", "HEAD"])
def ping():
    return "pong", 200


# ── Pi → Server ───────────────────────────────────────────────

@app.route("/pi/sensor_data", methods=["POST"])
def pi_sensor_data():
    data   = request.get_json(force=True, silent=True) or {}
    tid    = data.get("trolley_id", "unknown")
    entry  = get_trolley(tid)
    status = entry["status"]
    with store_lock:
        if "weight_g"   in data: status["weight_g"]   = data["weight_g"]
        if "arduino_ok" in data: status["arduino_ok"] = data["arduino_ok"]
        if "pi_fps"     in data: status["pi_fps"]     = data["pi_fps"]
        entry["_rx_ts"] = time.time()
    return jsonify({"ok": True}), 200


@app.route("/pi/frame/<trolley_id>", methods=["POST"])
def pi_frame(trolley_id):
    raw = request.data
    if not raw:
        return jsonify({"error": "empty"}), 400
    entry = get_trolley(trolley_id)
    with store_lock:
        entry["_rx_ts"] = time.time()
        if entry["frame"] is None:
            entry["frame"]    = raw
            entry["frame_ts"] = time.time()
    try:
        _infer_queue.put_nowait((trolley_id, raw))
    except Exception:
        pass   # queue full — drop frame
    return jsonify({"ok": True}), 200


@app.route("/pi/commands/<trolley_id>", methods=["GET"])
def pi_commands(trolley_id):
    entry = get_trolley(trolley_id)
    with store_lock:
        cmd              = entry.get("command")
        entry["command"] = None
    return jsonify({"command": cmd}), 200


# ── Dashboard API ─────────────────────────────────────────────

@app.route("/api/status/<trolley_id>", methods=["GET"])
def api_status(trolley_id):
    entry  = get_trolley(trolley_id)
    status = dict(entry["status"])
    last   = entry.get("_rx_ts", 0)
    status["online"]      = bool(last and (time.time() - last) < STATUS_MAX_AGE)
    status["frame_age_s"] = round(time.time() - entry.get("frame_ts", 0), 1)
    return jsonify(status), 200


@app.route("/api/frame/<trolley_id>", methods=["GET"])
def api_frame(trolley_id):
    entry    = get_trolley(trolley_id)
    frame    = entry.get("frame")
    frame_ts = entry.get("frame_ts", 0)
    if not frame or (time.time() - frame_ts) > 12:
        return send_file(
            io.BytesIO(_grey_placeholder()),
            mimetype="image/jpeg", max_age=0
        )
    return send_file(io.BytesIO(frame), mimetype="image/jpeg", max_age=0)


@app.route("/api/checkout/<trolley_id>", methods=["POST"])
def api_checkout(trolley_id):
    entry = get_trolley(trolley_id)
    with store_lock:
        entry["_payment_mode"] = True
        entry["command"]       = "checkout"
    log.info(f"[{trolley_id}] Checkout — cart frozen")
    return jsonify({"ok": True}), 200


@app.route("/api/clear/<trolley_id>", methods=["POST"])
def api_clear(trolley_id):
    entry = get_trolley(trolley_id)
    with store_lock:
        s = entry["status"]
        s["cart"]        = []
        s["total"]       = 0
        s["fraud_alert"] = None
        s["theft_alert"] = None
        entry["_payment_mode"]  = False
        entry["_det_buffer"]    = {}
        entry["_absent_buffer"] = {}
        entry["_next_item_id"]  = 1
        entry["command"]        = "clear_cart"
    log.info(f"[{trolley_id}] Cart cleared")
    return jsonify({"ok": True}), 200


@app.route("/api/remove_item/<trolley_id>", methods=["POST"])
def api_remove_item(trolley_id):
    data    = request.get_json(force=True, silent=True) or {}
    item_id = data.get("item_id")
    entry   = get_trolley(trolley_id)
    with store_lock:
        status = entry["status"]
        before = len(status["cart"])
        removed = next(
            (i for i in status["cart"] if i.get("id") == item_id), None
        )
        if removed:
            entry["_det_buffer"].pop(removed["name"], None)
            entry["_absent_buffer"].pop(removed["name"], None)
        status["cart"]  = [i for i in status["cart"] if i.get("id") != item_id]
        status["total"] = sum(i["price"] for i in status["cart"])
    return jsonify({"ok": True, "removed": before - len(entry["status"]["cart"])}), 200


@app.route("/api/trolleys", methods=["GET"])
def api_trolleys():
    with store_lock:
        ids = list(store.keys())
    return jsonify({"trolleys": ids}), 200


# ── Razorpay ──────────────────────────────────────────────────

@app.route("/create_order", methods=["POST"])
def create_order():
    if not RZP_AVAILABLE:
        return jsonify({"status": "error", "message": "razorpay not installed"}), 500
    data   = request.get_json(force=True, silent=True) or {}
    amount = int(data.get("amount", 0))
    if amount <= 0:
        return jsonify({"status": "error", "message": "Invalid amount"}), 400
    if not RZP_KEY_ID or not RZP_KEY_SECRET:
        log.warning("Razorpay keys not set — returning demo order")
        return jsonify({
            "status":   "success",
            "order_id": f"order_DEMO_{int(time.time())}",
        }), 200
    try:
        client = razorpay.Client(auth=(RZP_KEY_ID, RZP_KEY_SECRET))
        order  = client.order.create({
            "amount":          amount * 100,
            "currency":        "INR",
            "payment_capture": 1,
        })
        log.info(f"Razorpay order: {order['id']}  ₹{amount}")
        return jsonify({"status": "success", "order_id": order["id"]}), 200
    except Exception as e:
        log.error(f"Razorpay error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# ── Checkout + WhatsApp ───────────────────────────────────────

@app.route("/checkout", methods=["POST"])
def checkout():
    data       = request.get_json(force=True, silent=True) or {}
    phone      = data.get("phone",      "N/A")
    items      = data.get("items",      [])
    total      = data.get("total",      0)
    payment_id = data.get("payment_id", "N/A")
    trolley    = data.get("trolley",    "T-0000")

    # Log purchase
    try:
        log_purchase(phone, items, total, payment_id, trolley)
    except Exception as e:
        log.error(f"Log error: {e}")

    # Send WhatsApp bill (WATI → Twilio)
    provider, wa_ok = send_whatsapp_bill(
        phone, items, total, payment_id, trolley
    )
    log.info(f"WhatsApp: provider={provider} success={wa_ok}")

    return jsonify({
        "status":   "success" if wa_ok else "partial",
        "whatsapp": wa_ok,
        "provider": provider,
    }), 200


# ── Owner Dashboard ───────────────────────────────────────────

@app.route("/owner", methods=["GET"])
def owner_dashboard():
    here = os.path.dirname(os.path.abspath(__file__))
    # Check templates/ folder first, then root as fallback
    tpl_path = None
    for folder in [os.path.join(here, "templates"), here]:
        candidate = os.path.join(folder, "owner.html")
        if os.path.exists(candidate):
            tpl_path = candidate
            break
    if not tpl_path:
        return "<h2>owner.html not found — place it inside the templates/ folder</h2>", 404
    with open(tpl_path, encoding="utf-8") as f:
        template = f.read()
    logs = read_logs(200)
    return render_template_string(template, products=PRODUCTS, logs=logs), 200


# ── Static / Dashboard ────────────────────────────────────────

@app.route("/", methods=["GET"])
def root():
    return redirect("/dashboard")


@app.route("/dashboard", methods=["GET"])
def dashboard():
    here = os.path.dirname(os.path.abspath(__file__))
    # Check templates/ folder first, then root as fallback
    for folder in [os.path.join(here, "templates"), here]:
        if os.path.exists(os.path.join(folder, "index.html")):
            return send_from_directory(folder, "index.html")
    return "<h2>index.html not found — place it inside the templates/ folder</h2>", 404


# ── Health ────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    with store_lock:
        n = len(store)
    wa_via = (
        "wati"    if (WATI_API_URL and WATI_API_TOKEN) else
        "twilio"  if (TWILIO_AVAILABLE and TWILIO_SID) else
        "not_configured"
    )
    return jsonify({
        "status":       "ok",
        "trolleys":     n,
        "model":        "loaded" if _session else "not_loaded",
        "onnx":         ONNX_AVAILABLE,
        "razorpay":     RZP_AVAILABLE and bool(RZP_KEY_ID),
        "wati":         bool(WATI_API_URL and WATI_API_TOKEN),
        "twilio":       TWILIO_AVAILABLE and bool(TWILIO_SID),
        "whatsapp_via": wa_via,
        "time":         time.time(),
    }), 200


# ═══════════════════════════════════════════════════════════════
#  UTILITY
# ═══════════════════════════════════════════════════════════════

def _grey_placeholder(w=640, h=480):
    try:
        img = np.full((h, w, 3), 40, dtype=np.uint8)
        cv2.putText(
            img, "Waiting for Pi camera...",
            (w // 2 - 160, h // 2),
            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (120, 120, 120), 2
        )
        _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 60])
        return buf.tobytes()
    except Exception:
        return (
            b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xfb\xd4P\x00\x00\x00\x1f\xff\xd9"
        )


# ═══════════════════════════════════════════════════════════════
#  STARTUP
# ═══════════════════════════════════════════════════════════════
_load_model()

_worker = threading.Thread(
    target=_inference_worker, daemon=True, name="InferenceWorker"
)
_worker.start()
log.info("✅ Inference worker started")

if WATI_API_URL and WATI_API_TOKEN:
    log.info(f"✅ WATI ready: {WATI_API_URL[:50]}")
elif TWILIO_AVAILABLE and TWILIO_SID:
    log.info("✅ Twilio ready (WATI not configured)")
else:
    log.warning("⚠️  No WhatsApp provider configured")

# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    log.info(f"SmartTrolley Server on port {PORT}")
    app.run(host="0.0.0.0", port=PORT, threaded=True)