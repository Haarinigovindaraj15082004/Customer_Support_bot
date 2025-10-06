import re
from functools import lru_cache
from typing import Dict, Tuple, Optional

from models import DetectedIntent
from ticketing import get_or_create_customer, create_ticket, append_message, find_open_ticket_by_order
from db import get_conn, get_order_status
from policy import normalize_issue, is_allowed
import llm
from manual import get_manual_fuzzy, upsert_manual

# ----------------------------- Globals & Regex -----------------------------
SESSION_CACHE: Dict[str, Dict] = {}

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
ORDER_ID_RE    = re.compile(r"(order[ _-]?id[: ]*)(ORDL[0-9A-Z-]{1,})", re.I)
ORDER_TOKEN_RE = re.compile(r"\b(ORDL[0-9A-Z-]{1,})\b", re.I)

END_TOKENS = ("no thanks","nothing","that's all","that is all","all good","i'm good","im good","nope","nah")
THANKS_TOKENS = ("thanks","thank you")
GREET_TOKENS = ("hi","hello","hey","hola","yo","good morning","good afternoon","good evening")

STOP = {
    "the","a","an","and","or","to","for","of","in","on","is","are","i","my","me","it",
    "this","that","with","was","had","have","has","please","hi","hello","hey"
}
OPEN_TICKET_TOKENS = (
    "open a ticket","open ticket","raise a ticket","raise ticket",
    "create a ticket","create ticket","register complaint","file a complaint"
)

# ----------------------------- Helpers -----------------------------
def _tokens(text: str):
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP]

def _contains_phrase(text: str, phrase: str) -> bool:
    return re.search(rf"\b{re.escape(phrase)}\b", text, flags=re.I) is not None

def _is_bare_order_message(text: str) -> Optional[str]:
    t = text.strip().strip(" .,:;!?\n\t\r\"'`()[]{}")
    m = ORDER_TOKEN_RE.fullmatch(t)
    return m.group(1).upper() if m else None

def _extract_specs_subset(section_md: str, user_text: str, product_facts: Optional[dict] = None) -> Optional[str]:
    """
    Generic, field-agnostic selector:
      - Tokenizes the user's ask and matches against ANY labels/lines in the saved markdown.
      - If no lines match, tries to synthesize bullets from product_facts (if provided).
      - Returns only the requested lines; None means "no filtering".
    """
    # --- normalize + tokenize ---
    q = " " + user_text.lower().strip() + " "
    # very small stoplist; reuse your STOP but allow numbers/units to pass through
    q_tokens = [w for w in re.findall(r"[a-z0-9\.\-/]+", q) if w and w not in STOP]

    # Light synonym expansion (works for many categories, not just electronics)
    SYN = {
        "wifi": ["wi-fi", "wi fi", "wlan", "802.11", "80211"],
        "throughput": ["speed", "bandwidth", "rate", "mbps", "gbps"],
        "ports": ["port", "connector", "interface", "slot", "lan", "wan", "ethernet", "usb", "hdmi", "displayport", "audio", "jack"],
        "power": ["voltage", "amp", "amps", "amperage", "watt", "watts", "adapter", "input", "battery", "psu"],
        "dimensions": ["size", "measurement", "measurements", "width", "height", "depth", "length", "mm", "cm", "inch", "inches", "kg", "g", "weight"],
        "warranty": ["guarantee", "support", "rma"],
        "material": ["fabric", "steel", "stainless", "plastic", "aluminum", "wood", "leather", "cotton", "polyester"],
        "capacity": ["volume", "storage", "ml", "l", "litre", "liter", "gb", "tb"],
        "color": ["colour", "finish", "shade"],
        "care": ["wash", "washing", "maintenance", "clean", "hand-wash", "dishwasher"],
        "app": ["application", "android", "ios", "mobile"],
        "security": ["encryption", "wpa", "wpa2", "wpa3", "tls"],
    }
    expanded = set(q_tokens)
    for tok in list(q_tokens):
        for key, syns in SYN.items():
            if tok == key or tok in syns:
                expanded.add(key)
                expanded.update(syns)
    q_tokens = expanded

    # --- pass 1: pick relevant lines from markdown ---
    lines = section_md.splitlines()
    kept = []
    for ln in lines:
        low = " " + ln.lower() + " "
        # Prefer matches in bold labels like **Label:**
        score = 0
        m = re.search(r"\*\*([^*]+?)\*\*\s*:", ln)
        if m:
            label = " " + m.group(1).lower() + " "
            if any(tok in label for tok in q_tokens):
                score += 2
        # Otherwise match anywhere on the line
        if any(tok in low for tok in q_tokens):
            score += 1
        if score > 0:
            kept.append(ln)

    if kept:
        return "# Requested details\n" + "\n".join(kept)

    # --- pass 2: synthesize from structured facts (if available) ---
    if product_facts:
        synth = []
        for k, v in product_facts.items():
            label = str(k).replace("_", " ").lower()
            if any(tok in (" " + label + " ") for tok in q_tokens):
                synth.append(f"- **{label.title()}:** {v}")
        if synth:
            return "# Requested details\n" + "\n".join(synth)

    # nothing matched → let caller fall back to full section (no filter)
    return None

# ----------------------------- FAQ Cache -----------------------------
@lru_cache(maxsize=1)
def _load_faqs():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, question, answer, COALESCE(keywords,'') AS keywords FROM faq"
        ).fetchall()
    faqs = []
    for r in rows:
        kws = [k.strip() for k in r["keywords"].lower().split(",") if k.strip()]
        faqs.append({"id": r["id"], "question": r["question"], "answer": r["answer"], "keywords": kws})
    return faqs

def refresh_faq_cache():
    _load_faqs.cache_clear()

def answer_faq_from_db(query: str) -> Optional[tuple[str, str]]:
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
            elif kw in toks:
                score += 1.0
        if score > best_score:
            best, best_score = f, score
    if best and best_score >= 1.0:
        return best["answer"], best["question"]
    return None

# ----------------------------- Intent Detection -----------------------------
def detect_intent(text: str) -> DetectedIntent:
    t = text.lower()
    order_id = None
    m = ORDER_ID_RE.search(text)
    if m:
        order_id = m.group(2).strip()
    else:
        fallback = ORDER_TOKEN_RE.findall(text)
        if fallback:
            order_id = fallback[0]

    if any(k in t for k in ["defect","defective","broken","damage","damaged"]):
        return DetectedIntent("defect", order_id, "Defective item")
    if ("wrong item" in t or "wrong product" in t or "not what i ordered" in t
        or "received different" in t or "received a different" in t
        or "different brand" in t or "mismatch" in t or "mismatched" in t
        or "incorrect item" in t or "wrong " in t):
        return DetectedIntent("wrong_item", order_id, "Received wrong item")
    if ("missing item" in t or "item missing" in t or "one item missing" in t
        or "not received" in t or "not delivered" in t or "partial delivery" in t
        or ("missing" in t and "item" in t)):
        return DetectedIntent("missing_item", order_id, "Missing/partial delivery")

    HUMAN_TOKENS = (
        "talk to a human","talk to human","talk to agent","human agent","human support","human assistance",
        "need human assistance","human help","need human help","connect me to a human","connect to human",
        "connect to agent","support person","representative","customer care","customer support","escalate",
        "escalation","call me","phone call","need a call","speak to someone","speak with someone","speak to a person"
    )
    if any(tok in t for tok in HUMAN_TOKENS) or (
        (("human" in t) or ("agent" in t) or ("representative" in t)) and
        ("help" in t or "assist" in t or "assistance" in t or "support" in t or
         "talk" in t or "speak" in t or "connect" in t or "call" in t)
    ):
        return DetectedIntent("human", order_id, "Human assistance request")

    BYE_TOKENS = ("bye","goodbye","bye bye","see you","cya","end chat","close chat","finish chat",
                  "stop","exit","quit","no thanks that's all","that's all","that is all")
    if any(tok in t for tok in BYE_TOKENS) or ("thanks" in t and "bye" in t):
        return DetectedIntent("bye", order_id, None)

    if any(_contains_phrase(t, g) for g in GREET_TOKENS):
        return DetectedIntent("greet", order_id, None)

    FAQ_TRIGGERS = (
        "return policy","return","exchange","refund","delivery time","shipping","track","tracking","cancel","cancellation",
        "address change","address","cod","cash on delivery","payment","payment failed","failed payment","money debited",
        "debited","charged","double charged","transaction","paid","invoice","gst","bill","billing","warranty","size",
        "fit","size chart","missing","not received","partial"
    )
    if any(k in t for k in FAQ_TRIGGERS):
        return DetectedIntent("faq", order_id, None)

    return DetectedIntent("fallback", order_id, None)

# ----------------------------- FAQ Answers -----------------------------
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

def _polite(text: str) -> str:
    return text.strip() + "\n\nAnything else I can help with?"

def compose_comment_reply(text: str) -> str:
    db = answer_faq_from_db(text)
    base = db[0] if db else answer_faq(text)
    polished = getattr(llm, "rewrite_answer", lambda u, b: None)(text, base)
    return polished or _polite(base)

# ----------------------------- Ticketing -----------------------------
def _create_or_append_ticket(
    customer_id: int,
    order_id: Optional[str],
    issue_code: str,
    first_msg: str,
    source: str = "chat",
) -> tuple[int, bool]:
    if order_id:
        existing = find_open_ticket_by_order(customer_id, order_id)
        if existing:
            append_message(existing, "user", first_msg[:2000])
            return existing, False
    tid = create_ticket(
        customer_id=customer_id,
        order_id=order_id,
        issue_type=issue_code,
        first_msg=first_msg[:1000],
        source=source,
    )
    return tid, True

# ----------------------------- Manual Routing -----------------------------
def _manual_route(text: str):
    r = getattr(llm, "manual_route", None)
    if callable(r):
        try:
            return r(text)
        except Exception:
            pass
    r2 = getattr(llm, "detect_manual_request", None)
    if callable(r2):
        try:
            sec, prod = r2(text)
            if sec:
                return {"section": sec, "product": prod, "confidence": 0.75}
        except Exception:
            pass
    return None

# ----------------------------- Chat Turn -----------------------------
def chat_turn(session_id: str, user_text: str, email: Optional[str] = None, name: Optional[str] = None) -> Tuple[str, Optional[int]]:
    session = SESSION_CACHE.setdefault(session_id, {"facts": {}})
    facts = session["facts"]
    t = user_text.strip()
    tl = t.lower()

    rep = facts.setdefault("_repeat", {"key": None, "count": 0})
    def _mark(reply_key: str) -> int:
        if rep["key"] == reply_key: rep["count"] += 1
        else: rep["key"], rep["count"] = reply_key, 1
        return rep["count"]

    intent = detect_intent(user_text)
    if intent.order_id:
        facts["order_id"] = intent.order_id
    order_id = facts.get("order_id")

    # Greets / Bye
    if intent.type == "greet":
        wm = None
        try:
            wm = getattr(llm, "welcome_message", None)() if callable(getattr(llm, "welcome_message", None)) else None
        except Exception:
            wm = None
        return (wm or "Hello! I can help with orders (defective/wrong/missing), payments, refunds/returns, "
                      "delivery/tracking, cancellations, address changes, invoices, warranty and sizing."), None

    if intent.type == "bye" or any(k in tl for k in END_TOKENS) or any(k in tl for k in THANKS_TOKENS):
        SESSION_CACHE.pop(session_id, None)
        return "Alright — I’ll close this chat now. If you need anything later, just start a new one. 👋", None

    # Human email capture
    if facts.get("awaiting_human_email"):
        m = EMAIL_RE.search(user_text)
        if not m:
            return "To connect you to a human, please share your email ID (e.g., name@example.com).", None
        contact_email = m.group(0)
        customer_id = get_or_create_customer(email=contact_email, name=name or "Chat User")
        facts["customer_id"] = customer_id
        facts["contact_email"] = contact_email
        tid, _ = _create_or_append_ticket(customer_id, order_id, "HUMAN_ASSISTANCE", f"[Human request] {contact_email}", "chat")
        facts.pop("awaiting_human_email", None)
        return (f"Okay, I’ve requested a human agent. Ticket #{tid}"
                + (f" for Order {order_id}." if order_id else ".")
                + f" We’ll reach out to {contact_email} shortly."), tid

    # Bare ORDL short-circuit
    bare_order = _is_bare_order_message(user_text)
    if bare_order:
        facts["order_id"] = bare_order
        order_id = bare_order
        last_code = facts.get("last_issue_code")
        pending_msg = facts.get("pending_issue_text") or "Auto-created from prior complaint."
        if last_code:
            status = get_order_status(order_id)
            if not status:
                return f"I captured **{order_id}**, but I couldn’t find it in our system. Please double-check the ID.", None
            if not is_allowed(last_code, status):
                pretty = last_code.replace("_"," ").lower()
                return (f"Order {order_id} is **{status}**. *{pretty}* isn’t available at this stage. "
                        "I can connect you to a human or suggest alternatives (e.g., return/refund)."), None
            customer_id = facts.get("customer_id") or get_or_create_customer(email=email, name=name)
            facts["customer_id"] = customer_id
            tid, created = _create_or_append_ticket(customer_id, order_id, last_code, pending_msg, "chat")
            facts.pop("pending_issue_text", None)
            return (f"{'Created' if created else 'Updated'} ticket #{tid} for Order {order_id}. We’ll follow up shortly."), tid
        if _mark("ask_issue_after_bare_ordl") >= 2:
            return ("We have your Order ID. Tell me what happened (defective/wrong/missing item, "
                    "payment, refund/return, delivery/tracking, etc.), or say “connect me to a human”."
                    ), None
        return (f"Thanks — I’ve saved Order ID **{order_id}**. What went wrong with it—wrong item, missing item, "
                "damaged, late delivery, or something else?"), None

    # Guard manuals when touching orders
    touching_order = bool(ORDER_TOKEN_RE.search(user_text)) or bool(order_id)
    route = None if touching_order else _manual_route(user_text)

    if route and route.get("section") and float(route.get("confidence", 0.0)) >= 0.6:
        sec  = (route["section"] or "").lower().strip()
        prod = (route.get("product") or facts.get("last_manual_product"))
        if sec in {"specs","technical_specs","technical specs"}: sec = "tech_specs"
        if not prod:
            return (f"Sure — {sec.replace('_',' ')}. Which product is this for? Please tell me the product name."), None

        section_md = get_manual_fuzzy(prod, sec)
        if not section_md:
            full_md = getattr(llm, "generate_manual_md")(prod, facts.get("manual_facts", {}))
            section_md = getattr(llm, "extract_manual_section")(full_md, sec)
            upsert_manual(prod, sec, section_md, facts=facts.get("manual_facts", {}))
            session["facts"]["last_manual_md"] = full_md

        if sec == "tech_specs":
            subset = _extract_specs_subset(section_md, user_text, session["facts"].get("manual_facts"))
            if subset: section_md = subset

        session["facts"]["last_manual_product"] = prod
        if len(section_md) > 1500:
            section_md = section_md[:1500].rstrip() + "\n\n…(truncated) Say “send full guide” for the complete manual."
        return section_md, None

    if user_text.strip().lower() in {"send full guide","full manual","full user guide"}:
        md = session["facts"].get("last_manual_md")
        if not md:
            return "I don’t have a generated guide yet. Ask me like “user guide for <product>”.", None
        product = session["facts"].get("last_manual_product") or "your product"
        out = md if len(md) <= 3500 else (md[:3500].rstrip() + "\n\n…(truncated)")
        return f"# {product} — User Guide\n\n{out}", None

    # Ensure customer id
    customer_id = facts.get("customer_id")
    if not customer_id:
        customer_id = get_or_create_customer(email=email, name=name)
        facts["customer_id"] = customer_id

    # Explicit ticket-open phrasing
    if any(tok in tl for tok in OPEN_TICKET_TOKENS):
        issue_code = facts.get("last_issue_code") or "GENERAL_QUERY"
        tid, _ = _create_or_append_ticket(customer_id, facts.get("order_id"), issue_code, user_text, "chat")
        return (f"Done — I’ve opened ticket #{tid}"
                + (f" for Order {facts.get('order_id')}." if facts.get("order_id") else ".")
                + " We’ll follow up shortly."), tid

    # Human escalation
    if intent.type == "human":
        if not (email and "@" in (email or "")):
            facts["awaiting_human_email"] = True
            return "Sure — I’ll connect you to a human. Please share your email ID (e.g., name@example.com).", None
        tid, created = _create_or_append_ticket(customer_id, order_id, "HUMAN_ASSISTANCE", user_text, "chat")
        msg = "I’ve requested a human agent" if created else "I’ve added your request to your existing ticket"
        return (f"Okay, {msg} #{tid}" + (f" for Order {order_id}." if order_id else ".") + " We’ll reach out shortly."), None

    # Core issue intents
    if intent.type in ("defect","wrong_item","missing_item"):
        issue_code = "DEFECTIVE_ITEM" if intent.type == "defect" else "WRONG_ITEM" if intent.type == "wrong_item" else "MISSING_ITEM"
        facts["last_issue_code"] = issue_code
        facts["pending_issue_text"] = user_text
        if not order_id:
            if _mark("ask_ordl_for_ticketable") >= 2:
                return ("I still don’t have an Order ID. I can connect you to a human right away. Would you like me to do that?"), None
            return "Got it. Please share your Order ID (starts with ORDL…) and I’ll file it right away.", None
        status = get_order_status(order_id)
        if not status:
            return f"I couldn’t find {order_id}. Please double-check the Order ID.", None
        if not is_allowed(issue_code, status):
            pretty = issue_code.replace("_"," ").lower()
            return (f"Order {order_id} is **{status}**. Sorry, *{pretty}* isn’t available at this stage. "
                    "I can connect you to a human or suggest alternatives (e.g., return/refund where possible)."), None
        tid, created = _create_or_append_ticket(customer_id, order_id, issue_code, user_text, "chat")
        facts.pop("pending_issue_text", None)
        return f"Done! I’ve created ticket #{tid} for Order {order_id}. We’ll update you shortly.", tid

    # FAQ
    if intent.type == "faq":
        hit = answer_faq_from_db(user_text)
        raw_ans = hit[0] if hit else answer_faq(user_text)
        if hit:
            _, label = hit
            facts["last_issue_code"] = normalize_issue(label)
        else:
            label = infer_issue_label_from_text(user_text)
            facts["last_issue_code"] = normalize_issue(label)
        polished = getattr(llm, "rewrite_answer", lambda u, b: None)(user_text, raw_ans) or raw_ans
        return polished + "\n\nIf you’d like me to open a ticket for this, just say: “open a ticket for this issue”.", None

    # If there is an order id but no issue yet
    if intent.type == "fallback" and order_id and not any(k in tl for k in (
        "defect","broken","damaged","wrong","missing","not received","partial","refund","return","exchange",
        "payment","charged","debited","invoice","tracking","cancel","address","size","warranty"
    )):
        if _mark("ask_issue_after_ordl") >= 2:
            return ("We can take this forward with a human or you can quickly tell me the issue "
                    "(defective/wrong/missing, payment, refund/return, delivery/tracking, etc.)."), None
        return (f"Noted Order ID {order_id}. Tell me the issue (e.g., defective/wrong/missing item, "
                "payment, refund/return, delivery/tracking, cancellation, address change, invoice, warranty, sizing)."), None

    # Defect fast-path
    if any(w in tl for w in ("defect","defective","broken","damage","damaged")):
        if not order_id:
            if _mark("ask_ordl_for_defect") >= 2:
                return ("I still don’t have an Order ID. I can connect you to a human right away. Would you like me to do that?"), None
        status = get_order_status(order_id)
        if not status:
            return f"I couldn’t find {order_id}. Please double-check the Order ID.", None
        if not is_allowed("DEFECTIVE_ITEM", status):
            return (f"Order {order_id} is **{status}**. A defective-item replacement isn’t available at this stage. "
                    "I can connect you to a human or suggest alternatives (e.g., return/refund)."), None
        tid, created = _create_or_append_ticket(customer_id, order_id, "DEFECTIVE_ITEM", user_text, "chat")
        return (f"{'Created' if created else 'Updated'} ticket #{tid}" + (f" for Order {order_id}." if order_id else ".")), tid

    # LLM fallback
    llm_res = getattr(llm, "classify", lambda _: None)(user_text) or {}
    if llm_res:
        llm_intent = llm_res.get("intent", "fallback")
        conf = float(llm_res.get("confidence", 0.0))
        if llm_res.get("order_id") and not order_id:
            order_id = llm_res["order_id"]; facts["order_id"] = order_id

        if llm_intent == "human" and conf >= 0.7:
            if not (email and "@" in (email or "")):
                facts["awaiting_human_email"] = True
                return "Sure — I’ll connect you to a human. Please share your email ID (e.g., name@example.com).", None
            tid, created = _create_or_append_ticket(customer_id, order_id, "HUMAN_ASSISTANCE", user_text, "chat")
            msg = "I’ve requested a human agent" if created else "I’ve added your request to your existing ticket"
            return (f"Okay, {msg} #{tid}" + (f" for Order {order_id}." if order_id else ".") + " We’ll reach out shortly."), None

        if llm_intent in ("defect","wrong_item","missing_item") and conf >= 0.7:
            code = {"defect":"DEFECTIVE_ITEM","wrong_item":"WRONG_ITEM","missing_item":"MISSING_ITEM"}[llm_intent]
            if not order_id:
                if _mark("ask_ordl_llm") >= 2:
                    return ("Still no Order ID. I can connect you to a human right away. Do you want that?"), None
                return "Got it. Share your Order ID (ORDL…) and I’ll file it for you.", None
            status = get_order_status(order_id)
            if not status:
                return f"I couldn’t find {order_id}. Please double-check the Order ID.", None
            if not is_allowed(code, status):
                pretty = code.replace("_"," ").lower()
                return (f"Order {order_id} is **{status}** so *{pretty}* isn’t available now. "
                        "I can connect you to a human or suggest alternatives."), None
            tid, created = _create_or_append_ticket(customer_id, order_id, code, user_text, "chat")
            return (f"{'Created' if created else 'Updated'} ticket #{tid}" + (f" for Order {order_id}." if order_id else ".")), tid

        if llm_intent == "faq" and conf >= 0.6:
            db = answer_faq_from_db(user_text)
            ans, label = (db[0], db[1]) if db else (answer_faq(user_text), llm_res.get("issue_label") or "other")
            facts["last_issue_code"] = normalize_issue(label)
            rewriter = getattr(llm, "rewrite_answer", None)
            polished = rewriter(user_text, ans) if callable(rewriter) else None
            return (polished or ans) + "\n\nIf you’d like me to open a ticket for this, just say so.", None

    # Generic help
    generic = ("I can help with order issues (defective/wrong/missing), payments, refunds/returns, "
               "delivery/tracking, cancellations, address changes, invoices, warranty or sizing. "
               "Tell me what happened, and share your Order ID (ORDL…) if it’s about a specific order.")
    if _mark("generic_help") >= 2:
        return ("It looks like we’re going in circles. I can connect you to a human agent now, "
                "or you can share your Order ID (ORDL…). What would you prefer?"), None
    return generic, None

# ----------------------------- Label Helper -----------------------------
def infer_issue_label_from_text(t: str) -> str:
    t = t.lower()
    if "payment" in t or "debited" in t or "charged" in t or "transaction" in t: return "payment issues"
    if "refund" in t: return "refund timelines"
    if "return" in t or "exchange" in t: return "return policy"
    if "delivery" in t or "shipping" in t: return "delivery time & shipping"
    if "track" in t or "tracking" in t: return "order tracking"
    if "cancel" in t: return "cancellation"
    if "address" in t: return "address change"
    if "cod" in t or "cash on delivery" in t: return "cash on delivery"
    if "invoice" in t or "gst" in t or "bill" in t: return "invoice / gst"
    if "warranty" in t: return "warranty"
    if "size" in t or "fit" in t or "size chart" in t: return "size & fit"
    if "missing" in t or "not received" in t or "partial" in t: return "missing / partial delivery"
    if "damaged" in t or "broken" in t: return "damaged in transit"
    return "other"
