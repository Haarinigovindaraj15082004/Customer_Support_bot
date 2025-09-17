ALLOWED_BY_STATUS = {
    "ADDRESS_CHANGE": {"ORDER_PLACED", "PAYMENT_PENDING", "CONFIRMED", "PACKING"},
    "CANCEL_ORDER":   {"ORDER_PLACED", "PAYMENT_PENDING", "CONFIRMED"},

    "DEFECTIVE_ITEM": {"DELIVERED"},
    "WRONG_ITEM":     {"DELIVERED"},
    "MISSING_ITEM":   {"DELIVERED"},

    "TRACKING":       {"SHIPPED", "OUT_FOR_DELIVERY", "DELIVERED"},
    "PAYMENT_ISSUE":  {"ORDER_PLACED", "PAYMENT_PENDING"},
    "REFUND":         {"DELIVERED"},
    "INVOICE":        {"SHIPPED", "DELIVERED"},
}

def normalize_issue(label_or_type: str) -> str:
    t = label_or_type.lower().strip()
    mapping = {
        "defect": "DEFECTIVE_ITEM",
        "defective_item": "DEFECTIVE_ITEM",
        "wrong_item": "WRONG_ITEM",
        "missing_item": "MISSING_ITEM",
        "address change": "ADDRESS_CHANGE",
        "cancellation": "CANCEL_ORDER",
        "order tracking": "TRACKING",
        "payment issues": "PAYMENT_ISSUE",
        "refund timelines": "REFUND",
        "invoice / gst": "INVOICE",
        "delivery time & shipping": "TRACKING",
        "other": "OTHER",
    }
    return mapping.get(t, t.upper().replace(" ", "_"))

def is_allowed(issue_code: str, status: str) -> bool:
    if status == "CANCELLED":
        return False
    allowed = ALLOWED_BY_STATUS.get(issue_code)
    return True if allowed is None else status in allowed
