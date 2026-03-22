def generate_bill(cart, total):
    lines = []
    lines.append("Smart Trolley Bill")
    lines.append("---------------------")
    for item in cart:
        name = item.get('name', 'Item')
        price = item.get('price', 0)
        lines.append(f"{name} - Rs.{price}")
    lines.append("---------------------")
    lines.append(f"Total: Rs.{total}")
    lines.append("")
    lines.append("Thank you for shopping!")
    lines.append("SmartTrolley - Powered by AI")
    return "\n".join(lines)