"""
SmartTrolley Server — Flask
Deployed on Render: https://smart-trolley-owlx.onrender.com

Endpoints (Pi → Server):
  POST /pi/sensor_data                  — Pi pushes cart/weight/detection status
  POST /pi/frame/<trolley_id>           — Pi pushes annotated JPEG frame

Endpoints (Dashboard → Server):
  GET  /                                — Redirect to /dashboard
  GET  /dashboard                       — Serve the live dashboard HTML
  GET  /api/status/<trolley_id>         — JSON status snapshot
  GET  /api/frame/<trolley_id>          — Latest annotated JPEG
  POST /api/checkout/<trolley_id>       — Queue checkout command for Pi
  POST /api/clear/<trolley_id>          — Queue clear_cart command for Pi
  POST /api/remove_item/<trolley_id>    — Remove a specific cart item by id

Endpoints (Pi polls):
  GET  /pi/commands/<trolley_id>        — Pi fetches pending command (consumed once)
"""

from flask import Flask, request, jsonify, send_file, redirect, send_from_directory
import threading
import time
import io
import os
import logging

# ============================================================
# CONFIGURATION
# ============================================================
PORT            = int(os.environ.get("PORT", 5000))
FRAME_MAX_AGE   = 10      # seconds — discard stale frames
STATUS_MAX_AGE  = 30      # seconds — mark trolley offline after this

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("SmartTrolley-Server")

# ============================================================
# IN-MEMORY STORE
# Per-trolley state:
#   status  — latest sensor_data payload from Pi
#   frame   — latest JPEG bytes
#   command — pending command for Pi to consume (one-shot)
# ============================================================
store       = {}   # { trolley_id: { "status": {}, "frame": bytes, "command": str|None } }
store_lock  = threading.Lock()


def get_trolley(trolley_id: str) -> dict:
    """Return (create if absent) the store entry for a trolley."""
    with store_lock:
        if trolley_id not in store:
            store[trolley_id] = {
                "status" : {},
                "frame"  : None,
                "frame_ts": 0,
                "command": None,
            }
        return store[trolley_id]


# ============================================================
# FLASK APP
# ============================================================
app = Flask(__name__, static_folder="static", template_folder="templates")


# ── Pi → Server ─────────────────────────────────────────────

@app.route("/pi/sensor_data", methods=["POST"])
def pi_sensor_data():
    """Receive cart + weight + detection status from Pi."""
    data       = request.get_json(force=True, silent=True) or {}
    trolley_id = data.get("trolley_id", "unknown")
    data["server_received_at"] = time.time()

    entry = get_trolley(trolley_id)
    with store_lock:
        entry["status"] = data

    return jsonify({"ok": True}), 200


@app.route("/pi/frame/<trolley_id>", methods=["POST"])
def pi_frame(trolley_id):
    """Receive annotated JPEG frame from Pi."""
    frame_bytes = request.data
    if not frame_bytes:
        return jsonify({"error": "empty body"}), 400

    entry = get_trolley(trolley_id)
    with store_lock:
        entry["frame"]    = frame_bytes
        entry["frame_ts"] = time.time()

    return jsonify({"ok": True}), 200


# ── Pi polls for commands ────────────────────────────────────

@app.route("/pi/commands/<trolley_id>", methods=["GET"])
def pi_commands(trolley_id):
    """
    Pi polls this endpoint every 0.5 s.
    Returns the pending command and clears it (one-shot delivery).
    """
    entry = get_trolley(trolley_id)
    with store_lock:
        cmd              = entry.get("command")
        entry["command"] = None          # consume
    return jsonify({"command": cmd}), 200


# ── Dashboard API ────────────────────────────────────────────

@app.route("/api/status/<trolley_id>", methods=["GET"])
def api_status(trolley_id):
    """Return latest trolley status as JSON."""
    entry  = get_trolley(trolley_id)
    status = entry.get("status", {})

    # Augment with server-computed fields
    last_rx = status.get("server_received_at", 0)
    status["online"] = (time.time() - last_rx) < STATUS_MAX_AGE if last_rx else False
    status["frame_age_s"] = round(time.time() - entry.get("frame_ts", 0), 1)

    return jsonify(status), 200


@app.route("/api/frame/<trolley_id>", methods=["GET"])
def api_frame(trolley_id):
    """Return the latest annotated JPEG frame."""
    entry    = get_trolley(trolley_id)
    frame    = entry.get("frame")
    frame_ts = entry.get("frame_ts", 0)

    if not frame or (time.time() - frame_ts) > FRAME_MAX_AGE:
        # Return a tiny grey placeholder JPEG when no frame is available
        placeholder = _grey_placeholder()
        return send_file(
            io.BytesIO(placeholder),
            mimetype="image/jpeg",
            max_age=0
        )

    return send_file(
        io.BytesIO(frame),
        mimetype="image/jpeg",
        max_age=0
    )


@app.route("/api/checkout/<trolley_id>", methods=["POST"])
def api_checkout(trolley_id):
    """
    Dashboard signals 'checkout'.
    Server queues command; Pi picks it up on next poll and sets payment_mode=True.
    """
    entry = get_trolley(trolley_id)
    with store_lock:
        entry["command"] = "checkout"
    log.info(f"[{trolley_id}] Checkout command queued")
    return jsonify({"ok": True, "command": "checkout"}), 200


@app.route("/api/clear/<trolley_id>", methods=["POST"])
def api_clear(trolley_id):
    """Queue a clear_cart command for Pi (called after payment completed)."""
    entry = get_trolley(trolley_id)
    with store_lock:
        entry["command"] = "clear_cart"
    log.info(f"[{trolley_id}] Clear-cart command queued")
    return jsonify({"ok": True, "command": "clear_cart"}), 200


@app.route("/api/remove_item/<trolley_id>", methods=["POST"])
def api_remove_item(trolley_id):
    """
    Manually remove an item from the server-side status snapshot.
    Note: this only patches the last-received status snapshot on the server.
    The Pi will overwrite it on its next push, but it prevents stale display.
    """
    data    = request.get_json(force=True, silent=True) or {}
    item_id = data.get("item_id")
    entry   = get_trolley(trolley_id)

    with store_lock:
        status = entry.get("status", {})
        cart   = status.get("cart", [])
        before = len(cart)
        cart   = [item for item in cart if item.get("id") != item_id]
        status["cart"]  = cart
        status["total"] = round(sum(i.get("price", 0) for i in cart), 2)
        entry["status"] = status

    removed = before - len(entry["status"]["cart"])
    return jsonify({"ok": True, "removed": removed}), 200


@app.route("/api/trolleys", methods=["GET"])
def api_trolleys():
    """List all known trolley IDs."""
    with store_lock:
        ids = list(store.keys())
    return jsonify({"trolleys": ids}), 200


# ── Dashboard ────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def root():
    return redirect("/dashboard")


@app.route("/dashboard", methods=["GET"])
def dashboard():
    """Serve the main dashboard HTML."""
    # Look for index.html next to this file
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "index.html")
    if os.path.exists(path):
        return send_from_directory(here, "index.html")
    return "<h2>Dashboard not found. Place index.html next to server_app.py</h2>", 404


# ── Utilities ────────────────────────────────────────────────

def _grey_placeholder(width=640, height=480):
    """Generate a tiny grey JPEG used as a placeholder when no frame is available."""
    try:
        import cv2
        import numpy as np
        img     = np.full((height, width, 3), 40, dtype=np.uint8)
        cv2.putText(img, "No frame available", (width // 2 - 130, height // 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (120, 120, 120), 2)
        _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 60])
        return buf.tobytes()
    except Exception:
        # Absolute fallback: 1×1 grey JPEG (valid minimal JPEG)
        return (
            b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t"
            b"\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a"
            b"\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\x1e"
            b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
            b"\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08"
            b"\t\n\x0b\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03"
            b"\x05\x05\x04\x04\x00\x00\x01}\x01\x02\x03\x00\x04\x11\x05\x12"
            b"!1A\x06\x13Qa\x07\"q\x142\x81\x91\xa1\x08#B\xb1\xc1\x15R\xd1"
            b"\xf0$3br\x82\t\n\x16\x17\x18\x19\x1a%&'()*456789:CDEFGHIJSTUVWXYZ"
            b"cdefghijstuvwxyz\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94"
            b"\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa"
            b"\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7"
            b"\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe1\xe2\xe3"
            b"\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf1\xf2\xf3\xf4\xf5\xf6\xf7\xf8"
            b"\xf9\xfa\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xfb\xd4P\x00\x00"
            b"\x00\x1f\xff\xd9"
        )


# ── Health check ─────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    with store_lock:
        n = len(store)
    return jsonify({"status": "ok", "trolleys": n, "time": time.time()}), 200


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    log.info(f"SmartTrolley Server starting on port {PORT}")
    app.run(host="0.0.0.0", port=PORT, threaded=True)