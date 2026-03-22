def generate_bill(cart, total):
    lines = ["🛒 *Smart Trolley Bill*", "─────────────────────"]
    for item in cart:
        lines.append(f"{item['name']} - ₹{item['price']}")
    lines.append("─────────────────────")
    lines.append(f"*Total: ₹{total}*")
    lines.append("\nThank you for shopping!")
    return "\n".join(lines)