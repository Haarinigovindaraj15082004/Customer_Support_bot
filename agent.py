import re
from functools import lru_cache
from typing import Dict, Tuple, Optional

from models import DetectedIntent
from ticketing import (
    get_or_create_customer, create_ticket, append_message,
    find_open_ticket_by_order
)
from db import get_conn, get_order_status
from policy import normalize_issue, is_allowed
from llm import classify  

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
# -----------------------------
# Session cache
# -----------------------------
SESSION_CACHE: Dict[str, Dict] = {}

# -----------------------------
# Order ID extraction (strict ORDL…)
# -----------------------------
ORDER_ID_RE    = re.compile(r"(order[ _-]?id[: ]*)(ORDL[0-9A-Z-]{1,})", re.I)
ORDER_TOKEN_RE = re.compile(r"\b(ORDL[0-9A-Z-]{1,})\b", re.I)

# -----------------------------
# Yes/No + end/thanks helpers
# -----------------------------
YES_TOKENS = (
    "yes","y","yeah","yep","sure","ok","okay","please",
    "raise ticket","open ticket","create ticket","register complaint","register ticket"
)
NO_TOKENS = ("no","n","nope","not now","later","dont","don't","do not")

END_TOKENS = (
    "no thanks", "nothing", "that's all", "that is all",
    "all good", "i'm good", "im good", "nope", "nah"
)
THANKS_TOKENS = ("thanks", "thank you", "thx", "ty")

def _is_yes(text: str) -> bool:
    t = text.lower()
    return any(tok in t for tok in YES_TOKENS)

def _is_no(text: str) -> bool:
    t = text.lower()
    return any(tok in t for tok in NO_TOKENS)

# -----------------------------
# FAQ helpers 
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
    Ensure your 'faq' table has a 'keywords' TEXT column.
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

def answer_faq_from_db(query: str) -> Optional[tuple[str, str]]:
    """
    Returns (answer, label_from_question) or None
    """
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
        return best["answer"], best["question"]
    return None

# -----------------------------
# Intent detection
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

    if ("missing item" in t
        or "item missing" in t
        or "one item missing" in t
        or "not received" in t
        or "not delivered" in t
        or "partial delivery" in t
        or ("missing" in t and "item" in t)):
        return DetectedIntent("missing_item", order_id, "Missing/partial delivery")

    # Human escalation 
    HUMAN_TOKENS = (
        "talk to a human", "talk to human", "talk to agent", "human agent",
        "human support", "human assistance", "need human assistance", "human help",
        "need human help", "connect me to a human", "connect to human",
        "connect to agent", "support person", "representative", "customer care",
        "customer support", "escalate", "escalation", "call me", "phone call",
        "need a call", "speak to someone", "speak with someone", "speak to a person"
    )
    if any(tok in t for tok in HUMAN_TOKENS) or (
        (("human" in t) or ("agent" in t) or ("representative" in t))
        and ( "help" in t or "assist" in t or "assistance" in t
              or "support" in t or "talk" in t or "speak" in t
              or "connect" in t or "call" in t )
    ):
        return DetectedIntent("human", order_id, "Human assistance request")

    BYE_TOKENS = (
        "bye", "goodbye", "bye bye", "see you", "cya",
        "end chat", "close chat", "finish chat", "stop", "exit", "quit",
        "no thanks that's all", "that's all", "that is all"
    )
    if any(tok in t for tok in BYE_TOKENS) or ("thanks" in t and "bye" in t):
        return DetectedIntent("bye", order_id, None)

    # --- FAQ triggers ---
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

    return DetectedIntent("fallback", order_id, None)

# -----------------------------
# Simple inline FAQ fallback 
# -----------------------------
def answer_faq(question: str) -> str:
    t = question.lower()

    if "return" in t or "exchange" in t:
        return ("Returns: 30 days if unused and in original packaging. "
                "Exchanges are subject to stock availability. Start from Orders → Return/Exchange.")
    if "refund" in t:
        return ("Refunds: issued to your original payment method within 5–7 business days "
                "after we receive and inspect the item.")

    if "delivery" in t or "shipping" in t:
        return ("Shipping: we dispatch in 24–48 hours; delivery is usually 2–5 business days "
                "depending on your location. You’ll get a tracking link by email/SMS.")
    if "track" in t or "tracking" in t:
        return ("Tracking: use the tracking link in your email/SMS. If you don’t have it, "
                "share your Order ID (starts with ORDL) and we’ll fetch it for you.")

    if "cancel" in t or "cancellation" in t:
        return ("Cancellation: allowed until the order is packed/shipped. If it’s already shipped, "
                "please refuse delivery or create a return after it arrives.")
    if "address" in t or "change address" in t:
        return ("Address change: possible before dispatch.")

    if "cod" in t or "cash on delivery" in t:
        return ("Cash on Delivery: available on eligible pin codes and order totals under the COD limit.")
    if "payment" in t or "paid" in t or "failed" in t or "debited" in t or "charged" in t:
        return ("Payment issues: if your payment was debited but the order isn’t visible, "
                "it’ll auto-refund in 5–7 business days.")
    if "invoice" in t or "gst" in t or "bill" in t:
        return ("Invoice: you can download it from the Orders page after the item ships. "
                "For GST invoice, ensure GST details are added before placing the order.")

    if "warranty" in t:
        return ("Warranty: covered as per brand policy. Keep your invoice; brand service centers may ask for it.")
    if "size" in t or "fit" in t or "size chart" in t:
        return ("Sizing: refer to the Size Chart on the product page. If it doesn’t fit, "
                "you can request an exchange or return within 30 days.")

    if "missing" in t or "not received" in t or "partial" in t:
        return ("Missing items: sometimes multi-item orders arrive in separate boxes. "
                "If something is still missing after the expected date, raise a ticket with your ORDL order ID.")
    if "damaged" in t or "broken" in t:
        return ("Damaged item: sorry about that! Please share photos and your ORDL order ID; "
                "we’ll create a replacement/return right away.")

    return ("Thanks! I’ve noted this. For order-specific help, please share your Order ID "
            "(starts with ORDL), e.g., ORDL12345.")

# -----------------------------
# Main chat turn
# -----------------------------
def chat_turn(session_id: str, user_text: str, email: Optional[str] = None, name: Optional[str] = None) -> Tuple[str, Optional[int]]:
    """
    Returns (assistant_reply, ticket_id_or_None)

    Flow:
      1) Answer FAQ / offer ticket
      2) Offer ticket (yes/no) WITHOUT asking Order ID first
      3) If YES, ask for Order ID (if missing)
      4) When Order ID present, check policy and create/append ticket
    """
    session = SESSION_CACHE.setdefault(session_id, {"facts": {}})
    facts = session["facts"]
    t = user_text.lower()

    intent = detect_intent(user_text)

    if intent.order_id:
        facts["order_id"] = intent.order_id

    if facts.get("awaiting_closure"):
        if intent.type == "bye" or _is_no(t) or any(k in t for k in END_TOKENS) or any(k in t for k in THANKS_TOKENS) or t.strip() == "":
            SESSION_CACHE.pop(session_id, None)
            return "Alright — I’ll close this chat now. If you need anything later, just start a new one. 👋", None
        else:
            facts.pop("awaiting_closure", None)

    if intent.type == "bye":
        SESSION_CACHE.pop(session_id, None)
        return "Bye! 👋 I’ll close this chat now. If you need help later, just start a new session.", None
    
    if SESSION_CACHE[session_id]["facts"].get("awaiting_human_email"):
        m = EMAIL_RE.search(user_text)
        if not m:
            return "To connect you to a human, please share your email ID (e.g., name@example.com).", None

        contact_email = m.group(0)

        customer_id = SESSION_CACHE[session_id]["facts"].get("customer_id")
        customer_id = get_or_create_customer(email=contact_email, name=name or "Chat User")
        SESSION_CACHE[session_id]["facts"]["customer_id"] = customer_id
        SESSION_CACHE[session_id]["facts"]["contact_email"] = contact_email

        order_id = SESSION_CACHE[session_id]["facts"].get("order_id")
        ticket_id = create_ticket(
            customer_id=customer_id,
            order_id=order_id,
            issue_type="human assistance",
            first_msg=f"[Human request] Contact email: {contact_email}",
            source="chat"
        )

        SESSION_CACHE[session_id]["facts"].pop("awaiting_human_email", None)

        return (
            f"Okay, I’ve requested a human agent. Ticket #{ticket_id} is created"
            + (f" for Order {order_id}." if order_id else ".")
            + f" We’ll reach out to {contact_email} shortly."
        ), ticket_id


    # ---------------------------------------------------
    # Pending YES/NO flow
    # ---------------------------------------------------
    pending = facts.get("pending_ticket_offer")
    if pending:
        if _is_no(t):
            facts.pop("pending_ticket_offer", None)
            facts["awaiting_closure"] = True
            return "Okay, I won’t raise a ticket. Anything else I can help with?", None

        order_id = facts.get("order_id")

        if _is_yes(t) and not order_id:
            return "Sure—please share your Order ID (starts with ORDL) to raise the ticket.", None

        if order_id:
            customer_id = facts.get("customer_id")
            if not customer_id:
                customer_id = get_or_create_customer(email=email, name=name)
                facts["customer_id"] = customer_id

            status = get_order_status(order_id)
            if not status:
                return f"I couldn’t find {order_id}. Please double-check the Order ID.", None

            facts["order_status"] = status
            issue_code = (pending.get("issue_type") or "OTHER").upper()

            if not is_allowed(issue_code, status):
                facts.pop("pending_ticket_offer", None)
                pretty = issue_code.replace("_", " ").lower()
                return (f"Order {order_id} is **{status}**. "
                        f"Sorry, *{pretty}* isn’t available at this stage. "
                        f"Would you like alternatives or to talk to a human?"), None

            existing = find_open_ticket_by_order(customer_id, order_id)
            if existing:
                append_message(existing, "user", pending.get("first_msg", "(no message)"))
                facts.pop("pending_ticket_offer", None)
                return f"Got it. I’ve added this to your existing ticket #{existing} for Order {order_id}.", existing

            first_msg = pending.get("first_msg") or user_text
            ticket_id = create_ticket(
                customer_id=customer_id,
                order_id=order_id,
                issue_type=issue_code,
                first_msg=first_msg,
                source="chat"
            )
            facts.pop("pending_ticket_offer", None)
            return (f"Thanks! I’ve created ticket #{ticket_id} for Order {order_id}. "
                    f"Our team will reach out with next steps."), ticket_id

        return "Would you like me to raise a support ticket for this? (yes/no)", None
    # --------------------
    # Human escalation
    # --------------------
    if intent.type == "human":
        SESSION_CACHE[session_id]["facts"]["awaiting_human_email"] = True
        return "Sure — I’ll connect you to a human. Please share your email ID (e.g., name@example.com) so we can reach you.", None
    
    # ---------------------------------------------------
    # DB-backed FAQ → answer + offer
    # ---------------------------------------------------
    faq_res = answer_faq_from_db(user_text)
    if faq_res and intent.type not in ("defect", "wrong_item", "missing_item"):
        ans, label = faq_res
        facts["pending_ticket_offer"] = {
            "issue_type": normalize_issue(label),
            "first_msg": user_text
        }
        return ans + "\n\nWould you like me to raise a support ticket for this? (yes/no)", None

    # ---------------------------------------------------
    # Bridge: user sent only an Order ID — ask issue
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
    # Ensure we have a customer id 
    # ---------------------------------------------------
    customer_id = facts.get("customer_id")
    if not customer_id:
        customer_id = get_or_create_customer(email=email, name=name)
        facts["customer_id"] = customer_id

    # ---------------------------------------------------
    # Inline FAQ → answer + offer
    # ---------------------------------------------------
    if intent.type == "faq":
        reply = answer_faq(user_text)
        label = infer_issue_label_from_text(user_text)
        facts["pending_ticket_offer"] = {
            "issue_type": normalize_issue(label),
            "first_msg": user_text
        }
        return reply + "\n\nWould you like me to raise a support ticket for this? (yes/no)", None

    # ---------------------------------------------------
    # Ticketable issues 
    # ---------------------------------------------------
    if intent.type in ("defect", "wrong_item", "missing_item"):
        issue_code = (
            "DEFECTIVE_ITEM" if intent.type == "defect"
            else "WRONG_ITEM" if intent.type == "wrong_item"
            else "MISSING_ITEM"
        )
        facts["pending_ticket_offer"] = {
            "issue_type": issue_code,
            "first_msg": user_text
        }
        return "I can help with that. Would you like me to raise a support ticket for this? (yes/no)", None

    # ---------------------------------------------------
    # Fallback nudge 
    # ---------------------------------------------------
    if "order" in t and "id" in t and not facts.get("order_id"):
        return "Share the Order ID in the format: Order ID: ORDL12345", None

    # ---------------------------------------------------
    # ----- LLM BACKUP -----
    # ---------------------------------------------------
    llm = classify(user_text)
    if llm:
        if llm.get("order_id") and not facts.get("order_id"):
            facts["order_id"] = llm["order_id"]

        llm_intent = llm.get("intent", "fallback")
        conf = float(llm.get("confidence", 0))

        if llm_intent == "bye" and conf >= 0.7:
            SESSION_CACHE.pop(session_id, None)
            return "Bye! 👋 I’ll close this chat now.", None

        if llm_intent == "human" and conf >= 0.7:
            SESSION_CACHE[session_id]["facts"]["awaiting_human_email"] = True
            return "Sure — I’ll connect you to a human. Please share your email ID (e.g., name@example.com) so we can reach you.", None

        if llm_intent in ("defect", "wrong_item", "missing_item") and conf >= 0.7:
            code = {"defect":"DEFECTIVE_ITEM","wrong_item":"WRONG_ITEM","missing_item":"MISSING_ITEM"}[llm_intent]
            facts["pending_ticket_offer"] = {"issue_type": code, "first_msg": user_text}
            return "I can help with that. Would you like me to raise a support ticket for this? (yes/no)", None

        if llm_intent == "faq" and conf >= 0.6:
            db = answer_faq_from_db(user_text)
            if db:
                ans, label = db
            else:
                ans = answer_faq(user_text)
                label = llm.get("issue_label") or "other"
            facts["pending_ticket_offer"] = {"issue_type": normalize_issue(label), "first_msg": user_text}
            return ans + "\n\nWould you like me to raise a support ticket for this? (yes/no)", None
    facts["pending_ticket_offer"] = {"issue_type": "human assistance", "first_msg": user_text}
    return "I can connect you to a human agent for this. Should I raise a ticket for a callback? (yes/no)", None

# -----------------------------
# Heuristic issue label 
# -----------------------------
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
