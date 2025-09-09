import re
from typing import Dict, Tuple
from models import DetectedIntent
from ticketing import (
    get_or_create_customer, create_ticket, append_message,
    find_open_ticket_by_order
)

# Very lightweight in-memory cache (per session_id)
SESSION_CACHE: Dict[str, Dict] = {}

# Order id will always start as 'ORDL'
ORDER_ID_RE = re.compile(r"(order[ _-]?id[: ]*)(ORDL[0-9A-Z-]{3,})", re.I)
ORDER_TOKEN_RE = re.compile(r"\b(ORDL[0-9A-Z-]{3,})\b", re.I)

def detect_intent(text: str) -> DetectedIntent:
    t = text.lower()

    # Extract order id (explicit pattern first)
    order_id = None
    m = ORDER_ID_RE.search(text)
    if m:
        order_id = m.group(2).strip()
    else:
        # fallback when user types just the id (e.g., ORDL12345)
        fallback = ORDER_TOKEN_RE.findall(text)
        if fallback:
            order_id = fallback[0]

    if any(k in t for k in ["defect", "defective", "broken", "damage", "damaged"]):
        return DetectedIntent("defect", order_id, "Defective item")
    if ("wrong item" in t
    or "wrong product" in t
    or "not what i ordered" in t
    or "received different" in t
    or "received a different" in t
    or "different brand" in t
    or "mismatch" in t
    or "mismatched" in t
    or "incorrect item" in t
    or "wrong " in t ):
        return DetectedIntent("wrong_item", order_id, "Received wrong item")

    if any(k in t for k in ["return policy", "refund", "delivery time", "shipping", "warranty"]):
        return DetectedIntent("faq", order_id, None)

    return DetectedIntent("fallback", order_id, None)

def answer_faq(question: str) -> str:
    t = question.lower()
    if "return" in t:
        return "You can return items within 30 days if unused and in original packaging."
    if "delivery" in t or "shipping" in t:
        return "Orders ship in 24–48 hrs; delivery usually in 2–5 business days."
    if "refund" in t:
        return "Refunds are processed within 5–7 business days to your original payment method."
    return "Thanks! I’ll note this. For specific orders, please share your Order ID (e.g., ORDL12345)."

def chat_turn(session_id: str, user_text: str, email: str | None = None, name: str | None = None) -> Tuple[str, int | None]:
    """
    Returns (assistant_reply, ticket_id_or_None)
    """
    session = SESSION_CACHE.setdefault(session_id, {"facts": {}})
    facts = session["facts"]

    intent = detect_intent(user_text)

    # Merge newly detected order id if present
    if intent.order_id:
        facts["order_id"] = intent.order_id

    # Bridge: if only Order ID was given (no issue words yet)
    t = user_text.lower()
    if intent.type == "fallback" and facts.get("order_id") and not (
        "defect" in t or "wrong" in t or "broken" in t or "damage" in t
    ):
        return f"Got your Order ID {facts['order_id']}. Is the issue a *defective item* or a *wrong item*?", None

    # Ensure we have a customer id
    customer_id = facts.get("customer_id")
    if not customer_id:
        customer_id = get_or_create_customer(email=email, name=name)
        facts["customer_id"] = customer_id

    # FAQ branch
    if intent.type == "faq":
        reply = answer_faq(user_text)
        return reply, None

    # Ticketable issues
    if intent.type in ("defect", "wrong_item"):
        issue_type = "defective_item" if intent.type == "defect" else "wrong_item"

        order_id = facts.get("order_id")
        if not order_id:
            return "Please share your Order ID (starts with ORDL), e.g., ORDL12345.", None

        existing = find_open_ticket_by_order(customer_id, order_id)
        if existing:
            append_message(existing, "user", user_text)
            ack = f"Got it. I’ve added this to your existing ticket #{existing} for Order {order_id}."
            return ack, existing

        ticket_id = create_ticket(
            customer_id=customer_id,
            order_id=order_id,
            issue_type=issue_type,
            first_msg=user_text
        )
        reply = (
            f"Thanks! I’ve created ticket #{ticket_id} for Order {order_id}. "
            f"Our team will reach out with next steps."
        )
        return reply, ticket_id

    # Fallback nudges
    if "order" in t and "id" in t and not facts.get("order_id"):
        return "Share the Order ID in the format: Order ID: ORDL12345", None

    return "I can help with defective or wrong items. Please tell me your issue and your Order ID (e.g., ORDL12345).", None
