import json
from datetime import datetime
import os
from pathlib import Path

# Use a safe location for logs on Render (ephemeral filesystem is OK for demo)
LOG_FILE = os.getenv("LOG_FILE", "purchases.log")


def log_purchase(phone, items, total, payment_id, trolley_id):
    """
    Log a purchase transaction to file
    
    Args:
        phone: Customer phone number
        items: List of items purchased [{"name": "...", "price": ...}]
        total: Total amount
        payment_id: Payment transaction ID
        trolley_id: Trolley identifier
    """
    try:
        entry = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "phone": phone,
            "trolley_id": trolley_id,
            "payment_id": payment_id,
            "total": int(total),
            "items": items,
            "item_count": len(items)
        }
        
        # Ensure directory exists
        log_dir = os.path.dirname(LOG_FILE)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)
        
        # Append to log file
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        
        print(f"✅ Purchase logged: {phone} - ₹{total}")
        
    except Exception as e:
        print(f"❌ Error logging purchase: {e}")


def read_logs(limit=100):
    """
    Read purchase logs from file
    
    Args:
        limit: Maximum number of logs to return (most recent first)
    
    Returns:
        List of purchase log entries
    """
    logs = []
    
    try:
        if not os.path.exists(LOG_FILE):
            print(f"ℹ️  Log file not found: {LOG_FILE}")
            return logs
        
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    log_entry = json.loads(line.strip())
                    logs.append(log_entry)
                except json.JSONDecodeError as e:
                    print(f"⚠️  Skipping invalid log entry: {e}")
                    continue
        
        # Return most recent entries first
        logs = logs[-limit:]  # Get last N entries
        
        print(f"✅ Loaded {len(logs)} purchase logs")
        return logs
        
    except Exception as e:
        print(f"❌ Error reading logs: {e}")
        return []


def get_sales_summary():
    """Get summary statistics from logs"""
    logs = read_logs(limit=1000)
    
    if not logs:
        return {
            "total_transactions": 0,
            "total_revenue": 0,
            "total_items": 0,
            "average_transaction": 0
        }
    
    total_revenue = sum(log.get("total", 0) for log in logs)
    total_items = sum(log.get("item_count", 0) for log in logs)
    
    return {
        "total_transactions": len(logs),
        "total_revenue": total_revenue,
        "total_items": total_items,
        "average_transaction": round(total_revenue / len(logs), 2) if logs else 0,
        "first_transaction": logs[0].get("time") if logs else None,
        "last_transaction": logs[-1].get("time") if logs else None
    }


def clear_logs():
    """Clear all logs (use with caution)"""
    try:
        if os.path.exists(LOG_FILE):
            os.remove(LOG_FILE)
            print("✅ Logs cleared")
            return True
    except Exception as e:
        print(f"❌ Error clearing logs: {e}")
        return False