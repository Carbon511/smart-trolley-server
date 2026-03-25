from datetime import datetime
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.units import inch
from reportlab.lib import colors


def generate_bill(cart, total, phone="N/A", payment_id="N/A", trolley_id="T-4470"):
    """
    Generate a formatted bill text
    
    Args:
        cart: List of items [{"name": "...", "price": ...}]
        total: Total amount
        phone: Customer phone number
        payment_id: Payment transaction ID
        trolley_id: Trolley identifier
    
    Returns:
        Formatted bill string
    """
    bill_lines = []
    
    # Header
    bill_lines.append("=" * 50)
    bill_lines.append(" " * 10 + "SMART TROLLEY RECEIPT")
    bill_lines.append("=" * 50)
    
    # Info
    bill_lines.append(f"Trolley ID: {trolley_id}")
    bill_lines.append(f"Date & Time: {datetime.now().strftime('%d-%m-%Y %H:%M:%S')}")
    bill_lines.append(f"Phone: {phone}")
    bill_lines.append(f"Payment ID: {payment_id}")
    bill_lines.append("")
    
    # Items
    bill_lines.append("-" * 50)
    bill_lines.append(f"{'Item':<30} {'Price':>15}")
    bill_lines.append("-" * 50)
    
    if cart:
        for item in cart:
            item_name = str(item.get('name', 'Unknown'))[:28]
            price = int(item.get('price', 0))
            bill_lines.append(f"{item_name:<30} ₹{price:>14}")
    
    # Total
    bill_lines.append("-" * 50)
    bill_lines.append(f"{'TOTAL':<30} ₹{int(total):>14}")
    bill_lines.append("=" * 50)
    
    # Footer
    bill_lines.append("")
    bill_lines.append("Thank you for shopping!")
    bill_lines.append("Smart Trolley - AI Powered Checkout")
    bill_lines.append("=" * 50)
    
    return "\n".join(bill_lines)


def generate_bill_text_simple(cart, total):
    """Simple text bill without extra details"""
    lines = [
        "Smart Trolley Bill",
        "---------------------"
    ]
    
    if cart:
        for item in cart:
            lines.append(f"• {item['name']} - ₹{item['price']}")
    
    lines.extend([
        "---------------------",
        f"Total: ₹{total}",
        "",
        "Thank you for shopping!",
        "SmartTrolley - Powered by AI"
    ])
    
    return "\n".join(lines)