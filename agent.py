import re
from functools import lru_cache
from typing import Dict, Tuple

from models import DetectedIntent
from ticketing import (
    get_or_create_customer, create_ticket, append_message,
    find_open_ticket_by_order
)
from db import get_conn

# -----------------------------
# Session cache (very light)
# -----------------------------
SESSION_CACHE: Dict[str, Dict] = {}

# -----------------------------
# Order ID extraction (ORDL…)
# -----------------------------
ORDER_ID_RE = re.compile(r"(order[ _-]?id[: ]*)(ORDL[0-9A-Z-]{3,})", re.I)
ORDER_TOKEN_RE = re.compile(r"\b(ORDL[0-9A-Z-]{3,})\b", re.I)

# -----------------------------
# Yes/No helpers for ticket offer
# -----------------------------
YES_TOKENS = (
    "yes","y","yeah","yep","sure","ok","okay","please",
    "raise ticket","open ticket","create ticket","register complaint","register ticket"
)
NO_TOKENS = ("no","n","nope","not now","later","dont","don't","do not")

def _is_yes(text: str) -> bool:
    t = text.lower()
    return any(tok in t for tok in YES_TOKENS)

def _is_no(text: str) -> bool:
    t = text.lower()
    return any(tok in t for tok in NO_TOKENS)

# -----------------------------
# FAQ helpers (DB-backed keyword matcher)
# -----------------------------
STOP = {
    "the","a","an","and","or","to","for","of","in","on","is","are","i","my","me","it",
    "this","that","with","was","had","have","has","please","hi","hello","hey"
}

def _tokens(text: str):
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP]

@lru_cache(maxsize=1)
def _load_faqs():
    """
    Load FAQs once (id, question, answer, keywords CSV) from the DB.
    Make sure your 'faq' table has a 'keywords' TEXT column.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, question, answer, COALESCE(keywords,'') AS keywords FROM faq"
        ).fetchall()
    faqs = []
    for r in rows:
        kws = [k.strip() for k in r["keywords"].lower().split(",") if k.strip()]
        faqs.append({
            "id": r["id"],
            "question": r["question"],
            "answer": r["answer"],
            "keywords": kws
        })
    return faqs

def answer_faq_from_db(query: str) -> tuple[str, str] | None:
    q = query.lower()
    toks = set(_tokens(query))
    best = None
    best_score = 0.0

    for f in _load_faqs():
        score = 0.0
        for kw in f["keywords"]:
            if not kw:
                continue
            if " " in kw and kw in q:
                score += 2.0
            else:
                if kw in toks:
                    score += 1.0
        if score > best_score:
            best, best_score = f, score

    if best and best_score >= 1.0:
        return best["answer"], best["question"]  # question doubles as issue label
    return None

# -----------------------------
# Intent detection (rules, no RAG)
# -----------------------------
def detect_intent(text: str) -> DetectedIntent:
    t = text.lower()

    # --- Order ID extraction ---
    order_id = None
    m = ORDER_ID_RE.search(text)
    if m:
        order_id = m.group(2).strip()
    else:
        fallback = ORDER_TOKEN_RE.findall(text)
        if fallback:
            order_id = fallback[0]

    # --- Ticketable: defect / wrong item / missing item ---
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
        or "wrong " in t):
        return DetectedIntent("wrong_item", order_id, "Received wrong item")

    # Missing / partial delivery
    if (
        "missing item" in t
        or "item missing" in t
        or "one item missing" in t
        or "not received" in t
        or "not delivered" in t
        or "partial delivery" in t
        or ("missing" in t and "item" in t)
    ):
        return DetectedIntent("missing_item", order_id, "Missing/partial delivery")

    # --- FAQ triggers (broad guard so detect_intent() can route FAQs too) ---
    FAQ_TRIGGERS = (
        "return policy", "return", "exchange",
        "refund",
        "delivery time", "shipping",
        "track", "tracking",
        "cancel", "cancellation",
        "address change", "address",
        "cod", "cash on delivery",
        "payment", "payment failed", "failed payment", "money debited", "debited", "charged", "double charged", "transaction", "paid",
        "invoice", "gst", "bill", "billing",
        "warranty",
        "size", "fit", "size chart",
        "missing", "not received", "partial"
    )
    if any(k in t for k in FAQ_TRIGGERS):
        return DetectedIntent("faq", order_id, None)

    # --- Fallback ---
    return DetectedIntent("fallback", order_id, None)

# -----------------------------
# Simple inline FAQ fallback (backup)
# -----------------------------
def answer_faq(question: str) -> str:
    t = question.lower()

    # Returns / exchanges / refunds
    if "return" in t or "exchange" in t:
        return ("Returns: 30 days if unused and in original packaging. "
                "Exchanges are subject to stock availability. Start from Orders → Return/Exchange.")
    if "refund" in t:
        return ("Refunds: issued to your original payment method within 5–7 business days "
                "after we receive and inspect the item.")

    # Shipping / delivery / tracking
    if "delivery" in t or "shipping" in t:
        return ("Shipping: we dispatch in 24–48 hours; delivery is usually 2–5 business days "
                "depending on your location. You’ll get a tracking link by email/SMS.")
    if "track" in t or "tracking" in t:
        return ("Tracking: use the tracking link in your email/SMS. If you don’t have it, "
                "share your Order ID (starts with ORDL) and we’ll fetch it for you.")

    # Order changes / cancellation / address
    if "cancel" in t or "cancellation" in t:
        return ("Cancellation: allowed until the order is packed/shipped. If it’s already shipped, "
                "please refuse delivery or create a return after it arrives.")
    if "address" in t or "change address" in t:
        return ("Address change: possible before dispatch. Share your Order ID (ORDL…) and the new address; "
                "we’ll try to update if the shipment hasn’t left.")

    # Payment / COD / invoice
    if "cod" in t or "cash on delivery" in t:
        return ("Cash on Delivery: available on eligible pin codes and order totals under the COD limit.")
    if "payment" in t or "paid" in t or "failed" in t or "debited" in t or "charged" in t:
        return ("Payment issues: if your payment was debited but the order isn’t visible, "
                "it’ll auto-refund in 5–7 business days. Share your Order ID or transaction reference for checks.")
    if "invoice" in t or "gst" in t or "bill" in t:
        return ("Invoice: you can download it from the Orders page after the item ships. "
                "For GST invoice, ensure GST details are added before placing the order.")

    # Product issues / warranty / size
    if "warranty" in t:
        return ("Warranty: covered as per brand policy. Keep your invoice; brand service centers may ask for it.")
    if "size" in t or "fit" in t or "size chart" in t:
        return ("Sizing: refer to the Size Chart on the product page. If it doesn’t fit, "
                "you can request an exchange or return within 30 days.")

    # Missing/partial/damage in transit
    if "missing" in t or "not received" in t or "partial" in t:
        return ("Missing items: sometimes multi-item orders arrive in separate boxes. "
                "If something is still missing after the expected date, raise a ticket with your ORDL order ID.")
    if "damaged" in t or "broken" in t:
        return ("Damaged item: sorry about that! Please share photos and your ORDL order ID; "
                "we’ll create a replacement/return right away.")

    # Default
    return ("Thanks! I’ve noted this. For order-specific help, please share your Order ID "
            "(starts with ORDL), e.g., ORDL12345.")

# -----------------------------
# Main chat turn
# -----------------------------
def chat_turn(session_id: str, user_text: str, email: str | None = None, name: str | None = None) -> Tuple[str, int | None]:
    """
    Returns (assistant_reply, ticket_id_or_None)
    """
    session = SESSION_CACHE.setdefault(session_id, {"facts": {}})
    facts = session["facts"]
    t = user_text.lower()

    intent = detect_intent(user_text)

    # Merge newly detected order id if present
    if intent.order_id:
        facts["order_id"] = intent.order_id

    # ---------------------------------------------------
    # Pending: user was asked "Do you want a ticket?"
    # ---------------------------------------------------
    pending = facts.get("pending_ticket_offer")  # dict with keys: issue_type, first_msg
    if pending:
        # User declines
        if _is_no(t):
            facts.pop("pending_ticket_offer", None)
            return "Okay, I won’t raise a ticket. Anything else I can help with?", None

        # Treat "yes" OR supplying an ORDL as acceptance
        if _is_yes(t) or facts.get("order_id"):
            # Need customer id to create a ticket
            customer_id = facts.get("customer_id")
            if not customer_id:
                customer_id = get_or_create_customer(email=email, name=name)
                facts["customer_id"] = customer_id

            # Ensure we have order id
            order_id = facts.get("order_id")
            if not order_id:
                return "Sure—please share your Order ID (starts with ORDL) to raise the ticket.", None

            # Reuse open ticket for same order
            existing = find_open_ticket_by_order(customer_id, order_id)
            if existing:
                append_message(existing, "user", pending.get("first_msg","(no message)"))
                facts.pop("pending_ticket_offer", None)
                return f"Got it. I’ve added this to your existing ticket #{existing} for Order {order_id}.", existing

            # Create the ticket for ANY issue
            issue_type = pending.get("issue_type") or "other"
            first_msg = pending.get("first_msg") or user_text
            ticket_id = create_ticket(
                customer_id=customer_id,
                order_id=order_id,
                issue_type=issue_type,
                first_msg=first_msg
            )
            facts.pop("pending_ticket_offer", None)
            reply = (
                f"Thanks! I’ve created ticket #{ticket_id} for Order {order_id}. "
                f"Our team will reach out with next steps."
            )
            return reply, ticket_id

        # Neither yes/no nor order id → gently remind
        return "If you’d like me to raise a ticket, say **yes** or share your ORDL Order ID.", None

    # ---------------------------------------------------
    # Try DB-backed FAQ auto-answer first (unless clearly ticketable)
    # ---------------------------------------------------
    faq_res = answer_faq_from_db(user_text)
    if faq_res and intent.type not in ("defect", "wrong_item", "missing_item"):
        ans, label = faq_res  # label like "payment issues", "order tracking", etc.
        facts["pending_ticket_offer"] = {"issue_type": label, "first_msg": user_text}
        return ans + "\n\nWould you like me to raise a support ticket for this? (yes/no)", None

    # ---------------------------------------------------
    # Bridge: user sent only an Order ID — ask open-ended (don’t force only defect/wrong)
    # ---------------------------------------------------
    PAYMENT_WORDS = ("payment","paid","debited","charged","refund","transaction","failed")
    MISSING_WORDS = ("missing","not received","not delivered","partial")
    ISSUE_HINT_WORDS = ("defect","wrong","broken","damage","damaged") + PAYMENT_WORDS + MISSING_WORDS

    if intent.type == "fallback" and facts.get("order_id") and not any(w in t for w in ISSUE_HINT_WORDS):
        return (
            f"Got your Order ID {facts['order_id']}. "
            "Tell me the issue (e.g., payment issue, return/refund, delivery/tracking, cancellation, "
            "address change, warranty, sizing, or defective/wrong/missing item)."
        ), None

    # ---------------------------------------------------
    # Ensure we have a customer id (only when we might create/append tickets)
    # ---------------------------------------------------
    customer_id = facts.get("customer_id")
    if not customer_id:
        customer_id = get_or_create_customer(email=email, name=name)
        facts["customer_id"] = customer_id

    # ---------------------------------------------------
    # FAQ branch (rule-based backup if DB didn’t match)
    # ---------------------------------------------------
    if intent.type == "faq":
        reply = answer_faq(user_text)
        label = infer_issue_label_from_text(user_text)   # <- get a meaningful issue label
        facts["pending_ticket_offer"] = {"issue_type": label, "first_msg": user_text}
        return reply + "\n\nWould you like me to raise a support ticket for this? (yes/no)", None

    # ---------------------------------------------------
    # Ticketable issues (create for ANY of these without asking)
    # ---------------------------------------------------
    if intent.type in ("defect", "wrong_item", "missing_item"):
        issue_type = (
            "defective_item" if intent.type == "defect"
            else "wrong_item" if intent.type == "wrong_item"
            else "missing_item"
        )

        order_id = facts.get("order_id")
        if not order_id:
            return "Please share your Order ID (starts with ORDL), e.g., ORDL12345.", None

        # Reuse open ticket if same order
        existing = find_open_ticket_by_order(customer_id, order_id)
        if existing:
            append_message(existing, "user", user_text)
            ack = f"Got it. I’ve added this to your existing ticket #{existing} for Order {order_id}."
            return ack, existing

        # Create a new ticket
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

    # ---------------------------------------------------
    # Fallback nudges
    # ---------------------------------------------------
    if "order" in t and "id" in t and not facts.get("order_id"):
        return "Share the Order ID in the format: Order ID: ORDL12345", None

    return ("I can answer questions (payment, returns, delivery, tracking, etc.) and raise tickets for any issue. "
            "Tell me your issue, and if it’s about a specific order, share the Order ID (e.g., ORDL12345)."), None
    
def infer_issue_label_from_text(t: str) -> str:
    t = t.lower()
    if "payment" in t or "debited" in t or "charged" in t or "transaction" in t:
        return "payment issues"
    if "refund" in t:
        return "refund timelines"
    if "return" in t or "exchange" in t:
        return "return policy"
    if "delivery" in t or "shipping" in t:
        return "delivery time & shipping"
    if "track" in t or "tracking" in t:
        return "order tracking"
    if "cancel" in t:
        return "cancellation"
    if "address" in t:
        return "address change"
    if "cod" in t or "cash on delivery" in t:
        return "cash on delivery"
    if "invoice" in t or "gst" in t or "bill" in t:
        return "invoice / gst"
    if "warranty" in t:
        return "warranty"
    if "size" in t or "fit" in t or "size chart" in t:
        return "size & fit"
    if "missing" in t or "not received" in t or "partial" in t:
        return "missing / partial delivery"
    if "damaged" in t or "broken" in t:
        return "damaged in transit"
    return "other"

