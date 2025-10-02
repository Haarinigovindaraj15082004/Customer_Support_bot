from __future__ import annotations
import re
from datetime import datetime, timedelta, timezone
from typing import Tuple, Optional
from flask import Flask, request, jsonify
from zoneinfo import ZoneInfo
from config import settings
from db import (init_db, get_conn,report_summary, utc_range_for,)
from agent import (chat_turn,compose_comment_reply,refresh_faq_cache,)
from llm import classify, generate_manual_md, extract_manual_section
from manual import upsert_manual, get_manual, get_manual_fuzzy
from ticketing import get_ticket, set_status, get_or_create_customer, create_ticket, append_message, find_open_ticket_by_order
from policy import normalize_issue

app = Flask(__name__)

DEFAULT_TZ = getattr(settings, "TIMEZONE", "Asia/Kolkata")

def _to_utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()

def _week_start(dt: datetime) -> datetime:
    return (dt - timedelta(days=dt.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)

def _month_start(dt: datetime) -> datetime:
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["","January","February","March","April","May","June","July","August","September","October","November","December"]
)}

_DATE_RE = r"(\d{4})-(\d{2})-(\d{2})"

def _month_window_local(month_name: str, year: int, tz_name: str) -> Tuple[str, str]:
    tz = ZoneInfo(tz_name)
    m = _MONTHS.get(month_name.lower())
    if not m:
        raise ValueError("Unknown month name")
    start = datetime(year, m, 1, 0, 0, 0, tzinfo=tz)
    end = (datetime(year+1,1,1,0,0,0,tzinfo=tz)-timedelta(seconds=1)) if m == 12 \
        else (datetime(year,m+1,1,0,0,0,tzinfo=tz)-timedelta(seconds=1))
    return _to_utc_iso(start), _to_utc_iso(end)

def _explicit_range(text: str, tz_name: str) -> Optional[Tuple[str, str]]:
    m = re.search(rf"from\s+{_DATE_RE}\s+to\s+{_DATE_RE}", text, re.I)
    if not m:
        return None
    y1, mo1, d1, y2, mo2, d2 = map(int, m.groups())
    tz = ZoneInfo(tz_name)
    start = datetime(y1, mo1, d1, 0, 0, 0, tzinfo=tz)
    end   = datetime(y2, mo2, d2, 23, 59, 59, tzinfo=tz)
    return _to_utc_iso(start), _to_utc_iso(end)

def _range_from_query(q: str, tz_name: str) -> Tuple[str, str]:
    t = (q or "").lower()
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz).replace(microsecond=0)

    rng = _explicit_range(t, tz_name)
    if rng:
        return rng

    m = re.search(r"monthly\s+ticket\s+summary\s+for\s+([a-z]+)\s+(\d{4})", t, re.I)
    if m:
        return _month_window_local(m.group(1), int(m.group(2)), tz_name)

    if "today" in t:
        start = now.replace(hour=0, minute=0, second=0); return _to_utc_iso(start), _to_utc_iso(now)
    if "this week" in t:
        start = _week_start(now); return _to_utc_iso(start), _to_utc_iso(now)
    if "last week" in t:
        end = _week_start(now) - timedelta(seconds=1); start = _week_start(end); return _to_utc_iso(start), _to_utc_iso(end)
    if "this month" in t:
        start = _month_start(now); return _to_utc_iso(start), _to_utc_iso(now)
    if "last 30" in t or "last 30 days" in t:
        start = (now - timedelta(days=30)).replace(hour=0, minute=0, second=0); return _to_utc_iso(start), _to_utc_iso(now)
    if "last 7" in t or "last 7 days" in t:
        start = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0); return _to_utc_iso(start), _to_utc_iso(now)

    start = _week_start(now); return _to_utc_iso(start), _to_utc_iso(now)

def _process_ingest_message(
    *, channel: str, user_email: str, user_name: str | None, text: str,
    order_id: str | None = None, issue_type: str | None = None, thread: dict | None = None,
) -> dict:
    if not user_email:
        return {"error": "user_email required for ingest"}

    llm_res = classify(text) or {}
    intent_name = (llm_res.get("intent") or "fallback").lower()
    order = order_id or llm_res.get("order_id")

    if issue_type:
        issue_code = normalize_issue(issue_type)
    elif intent_name == "defect":
        issue_code = "DEFECTIVE_ITEM"
    elif intent_name == "wrong_item":
        issue_code = "WRONG_ITEM"
    elif intent_name == "missing_item":
        issue_code = "MISSING_ITEM"
    elif intent_name == "human":
        issue_code = "HUMAN_ASSISTANCE"
    else:
        label = llm_res.get("issue_label") or "other"
        issue_code = normalize_issue(label)

    customer_id = get_or_create_customer(email=user_email, name=(user_name or None))

    if order:
        existing = find_open_ticket_by_order(customer_id, order)
        if existing:
            append_message(existing, "user", text[:2000])
            return {
                "created": False,
                "appended_to_ticket": existing,
                "order_id": order,
                "issue_type": issue_code,
                "message": f"Appended to existing ticket #{existing} for order {order}."
            }

    ticket_id = create_ticket(
        customer_id=customer_id,
        order_id=order,
        issue_type=issue_code,
        first_msg=text[:1000],
        source=channel
    )
    return {
        "created": True,
        "ticket_id": ticket_id,
        "order_id": order,
        "issue_type": issue_code,
        "message": f"Created ticket #{ticket_id}."
    }

@app.get("/")
def home():
    return jsonify({
        "message": "Cassie API running",
        "endpoints": [
            "GET  /health",
            "POST /chat",
            "POST /ingest/message",
            "GET  /tickets",
            "GET  /tickets/<id>",
            "PATCH /tickets/<id>",
            "GET  /reports/summary?range=this_week|today|last7|last30|this_month or from/to",
            "POST /reports/query { q: 'plain text', tz?: 'Asia/Kolkata' }",
            "POST /faq/upsert",
            "POST /manual/generate",
            "GET  /manual/get?product=...&section=...",   
        ],
    })

@app.get("/health")
def health():
    return jsonify({"ok": True})

@app.post("/chat")
def chat():
    """
    Multi-turn assistant flow (LLM-first logic lives in agent.chat_turn).
    Optional: set {"ingest": true} to one-shot create/append a ticket.
    """
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id") or "demo-session"
    text = (data.get("text") or "").strip()
    email = (data.get("email") or "").strip()
    name = (data.get("name") or "").strip()

    if not text:
        return jsonify({"error": "text is required"}), 400

    if data.get("ingest") is True:
        if not email:
            return jsonify({"error": "email is required when ingest=true"}), 400
        result = _process_ingest_message(
            channel="chat", user_email=email, user_name=(name or None), text=text,
            order_id=(data.get("order_id") or None), issue_type=(data.get("issue_type") or None),
        )
        if "error" in result:
            return jsonify(result), 400
        return jsonify({"reply": result.get("message"), "ingest_result": result})

    reply, ticket_id = chat_turn(session_id, text, email=(email or None), name=(name or None))
    return jsonify({"reply": reply, "ticket_id": ticket_id})

@app.post("/ingest/message")
def ingest_message():
    """
    Comment-responder style intake from any channel.
    - If user.email is missing -> returns only a friendly reply (no ticket).
    - If user.email is present -> create/append ticket AND return the same reply.
    """
    data = request.get_json(silent=True) or {}
    channel = (data.get("channel") or "external").strip().lower()
    user = data.get("user") or {}
    user_email = (user.get("email") or "").strip()
    user_name = (user.get("name") or "").strip()
    text = (data.get("text") or "").strip()
    order_id = (data.get("order_id") or "").strip() or None
    issue_type = (data.get("issue_type") or "").strip() or None
    thread = data.get("thread") or {}

    if not text:
        return jsonify({"error": "text is required"}), 400

    reply = compose_comment_reply(text)

    if not user_email:
        return jsonify({"reply": reply, "ticket": None})

    result = _process_ingest_message(
        channel=channel, user_email=user_email, user_name=(user_name or None),
        text=text, order_id=order_id, issue_type=issue_type, thread=thread,
    )
    if "error" in result:
        return jsonify(result), 400
    return jsonify({"reply": reply, "ticket": result})

@app.get("/tickets")
def list_tickets():
    status = request.args.get("status")
    with get_conn() as conn:
        sql = ("SELECT id, customer_id, created_utc, updated_utc, order_id, "
               "issue_type, status, last_message, source FROM tickets ")
        args = ()
        if status:
            sql += "WHERE status = ? "
            args = (status,)
        sql += "ORDER BY id DESC"
        rows = [dict(r) for r in conn.execute(sql, args)]
    return jsonify({"tickets": rows})

@app.get("/tickets/<int:ticket_id>")
def ticket_with_messages(ticket_id: int):
    t = get_ticket(ticket_id)
    if not t:
        return jsonify({"error": "ticket not found"}), 404
    with get_conn() as conn:
        msgs = [dict(r) for r in conn.execute(
            "SELECT id, ticket_id, role, text, created_utc FROM messages WHERE ticket_id=? ORDER BY id",
            (ticket_id,)
        )]
    return jsonify({"ticket": t, "messages": msgs})

@app.patch("/tickets/<int:ticket_id>")
def update_ticket(ticket_id: int):
    data = request.get_json(silent=True) or {}
    status = data.get("status")
    if status not in {"open", "in_progress", "resolved", "closed"}:
        return jsonify({"error": "invalid status"}), 400
    if not get_ticket(ticket_id):
        return jsonify({"error": "ticket not found"}), 404
    set_status(ticket_id, status)
    return jsonify({"ok": True, "ticket_id": ticket_id, "status": status})

@app.get("/reports/summary")
def reports_summary_get():
    preset = (request.args.get("range") or "").lower()
    if preset:
        start, end = utc_range_for(preset)
    else:
        start = request.args.get("from"); end = request.args.get("to")
        if not start or not end:
            start, end = utc_range_for("last7")
    data = report_summary(start, end)
    return jsonify({"range": {"from_utc": start, "to_utc": end}, "summary": data})

@app.post("/reports/query")
def reports_query_post():
    body = request.get_json(silent=True) or {}
    q = (body.get("q") or "").strip()
    if not q:
        return jsonify({"error": "Missing 'q' in JSON body"}), 400
    tz_name = body.get("tz") or DEFAULT_TZ
    try:
        ZoneInfo(tz_name)
    except Exception:
        return jsonify({"error": f"Unknown timezone '{tz_name}'"}), 400
    start_utc, end_utc = _range_from_query(q, tz_name)
    data = report_summary(start_utc, end_utc)
    return jsonify({"query": q, "tz": tz_name, "range": {"from_utc": start_utc, "to_utc": end_utc}, "summary": data})

@app.post("/faq/upsert")
def faq_upsert():
    """
    Upsert FAQ rows (useful during setup). Accepts either a single object or a list:
    {
      "faqs": [
        {"question": "return policy", "answer": "...", "keywords": ["return","exchange"]},
        ...
      ]
    }
    """
    data = request.get_json(silent=True) or {}
    items = data.get("faqs")

    if isinstance(items, dict):
        items = [items]

    if not isinstance(items, list) or not items:
        return jsonify({"error": "Body must include 'faqs': [ {question, answer, keywords?} ]"}), 400

    inserted = updated = skipped = 0
    ids = []

    with get_conn() as conn:
        for f in items:
            q = (f.get("question") or "").strip()
            a = (f.get("answer") or "").strip()
            kws = f.get("keywords")

            if not q or not a:
                skipped += 1
                continue

            if isinstance(kws, str):
                kws = [kws]
            if isinstance(kws, list):
                kws = ",".join(sorted({(k or "").strip().lower() for k in kws if isinstance(k, str) and k.strip()}))
            else:
                kws = ""

            existing = conn.execute("SELECT id FROM faq WHERE question = ?", (q,)).fetchone()
            if existing:
                conn.execute(
                    "UPDATE faq SET answer = ?, keywords = ? WHERE id = ?",
                    (a, kws, existing["id"])
                )
                updated += 1
                ids.append(existing["id"])
            else:
                cur = conn.execute(
                    "INSERT INTO faq (question, answer, keywords) VALUES (?, ?, ?)",
                    (q, a, kws)
                )
                inserted += 1
                ids.append(cur.lastrowid)

    refresh_faq_cache()

    return jsonify({
        "ok": True,
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "ids": ids
    })

@app.post("/manual/generate")
def manual_generate():
    data = request.get_json(silent=True) or {}
    product = (data.get("product") or "").strip()
    if not product:
        return jsonify({"error": "product is required"}), 400

    facts   = data.get("facts") or {}
    section = (data.get("section") or "full").lower()

    md  = generate_manual_md(product, facts)
    out = extract_manual_section(md, section)

    manual_id = upsert_manual(product, section, out, facts=facts)

    return jsonify({
        "product": product,
        "section": section,
        "markdown": out,
        "id": manual_id
    })


@app.get("/manual/get")
def manual_get():
    """Fetch a stored manual section from DB."""
    product = (request.args.get("product") or "").strip()
    section = (request.args.get("section") or "full").strip().lower()
    if not product:
        return jsonify({"error": "product query param is required"}), 400
    md = get_manual(product, section)
    if not md:
        return jsonify({"error": "not found"}), 404
    return jsonify({"product": product, "section": section, "markdown": md})

if __name__ == "__main__":
    init_db()  
    app.run(host="127.0.0.1", port=5000, debug=True)
