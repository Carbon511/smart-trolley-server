import json
from datetime import datetime
LOG_FILE = "purchases.log"

def log_purchase(phone, items, total, payment_id, trolley):
    entry = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "phone": phone, "trolley": trolley, "payment_id": payment_id, "total": total, "items": items}
    with open(LOG_FILE, "a") as f: f.write(json.dumps(entry) + "\n")

def read_logs():
    logs = []
    try:
        with open(LOG_FILE, "r") as f:
            for line in f: logs.append(json.loads(line.strip()))
    except: pass
    return logs