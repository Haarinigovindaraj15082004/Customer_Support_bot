from __future__ import annotations
import re
from typing import Optional

def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()

def normalize_issue(label: Optional[str]) -> str:
    """
    Convert free-text issue labels into canonical codes used in the DB/routing.
    Always returns UPPER_SNAKE_CASE.
    """
    if not label:
        return "OTHER"

    s = _slug(label)

    pairs = [
        (("defect", "defective", "broken", "damage", "damaged"), "DEFECTIVE_ITEM"),
        (("wrong item", "wrong product", "different brand", "mismatch", "incorrect item"), "WRONG_ITEM"),
        (("missing item", "partial delivery", "not received", "not delivered", "missing"), "MISSING_ITEM"),
        (("damaged in transit", "transit damage"), "DAMAGED_IN_TRANSIT"),

        (("return policy", "returns", "exchange", "return window"), "RETURN_POLICY"),
        (("refund timelines", "refund", "refunds"), "REFUND_TIMELINES"),
        (("payment issues", "payment", "payment issue", "debited", "charged", "transaction", "double charged"), "PAYMENT_ISSUES"),
        (("delivery time & shipping", "delivery time", "shipping", "delivery"), "DELIVERY_SHIPPING"),
        (("order tracking", "tracking", "track"), "ORDER_TRACKING"),
        (("cancellation", "cancel", "order cancel"), "CANCELLATION"),
        (("address change", "change address", "address update"), "ADDRESS_CHANGE"),
        (("cash on delivery", "cod"), "CASH_ON_DELIVERY"),
        (("invoice / gst", "invoice", "gst", "bill", "billing"), "INVOICE_GST"),
        (("warranty",), "WARRANTY"),
        (("size & fit", "size", "fit", "size chart"), "SIZE_FIT"),
        (("human assistance", "human agent", "human support"), "HUMAN_ASSISTANCE"),
    ]

    for keys, code in pairs:
        for k in keys:
            if k in s:
                return code
            
    looks_like_code = re.fullmatch(r"[A-Z0-9_]+", label or "") is not None
    if looks_like_code:
        return label 

    return "OTHER"

def _norm_status(status: Optional[str]) -> str:
    return (status or "").strip().upper()

PRE_DISPATCH = {"PLACED", "PROCESSING", "PACKED"}
IN_TRANSIT   = {"SHIPPED", "OUT_FOR_DELIVERY"}
POST_DELIV   = {"DELIVERED"}

def is_allowed(issue_code: str, order_status: Optional[str]) -> bool:
    """
    Decide if opening a ticket for 'issue_code' is allowed given current 'order_status'.
    Unknown statuses default to permissive EXCEPT for actions that must be pre-dispatch.
    """
    code = normalize_issue(issue_code)
    st   = _norm_status(order_status)

    ALWAYS = {
        "PAYMENT_ISSUES",
        "REFUND_TIMELINES",
        "RETURN_POLICY",
        "DELIVERY_SHIPPING",
        "ORDER_TRACKING",
        "CASH_ON_DELIVERY",
        "INVOICE_GST",
        "HUMAN_ASSISTANCE",
        "WARRANTY",        
        "SIZE_FIT",         
        "OTHER",
    }
    if code in ALWAYS:
        return True

    if code == "ADDRESS_CHANGE":
        return st in PRE_DISPATCH
    if code == "CANCELLATION":
        return st in PRE_DISPATCH

    if code in {"DEFECTIVE_ITEM", "WRONG_ITEM"}:
        return st in POST_DELIV

    if code in {"MISSING_ITEM", "DAMAGED_IN_TRANSIT"}:
        return (st in IN_TRANSIT) or (st in POST_DELIV)

    if not st:
        return True

    return True
