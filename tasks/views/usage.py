def format_cost_amount(amount):
    if amount is None:
        return None
    formatted = format(amount, "f")
    return formatted.rstrip("0").rstrip(".") if "." in formatted else formatted
