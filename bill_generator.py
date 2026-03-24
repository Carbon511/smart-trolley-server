def generate_bill(cart, total):
    lines = ["Smart Trolley Bill", "---------------------"]
    for item in cart:
        lines.append(f"{item['name']} - Rs.{item['price']}")
    lines.append("---------------------")
    lines.append(f"Total: Rs.{total}")
    lines.append("\nThank you for shopping!\nSmartTrolley - Powered by AI")
    return "\n".join(lines)