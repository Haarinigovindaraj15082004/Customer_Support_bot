from flask import Flask, request, jsonify
from db import init_db, get_conn
from agent import chat_turn
from ticketing import get_ticket, set_status

app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return {"message": "Cassie API running", "endpoints": ["/health","POST /chat","GET /tickets","GET /tickets/<id>","PATCH /tickets/<id>"]}

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True})

@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(force=True, silent=True) or {}
    session_id = data.get("session_id") or "demo-session"
    text = (data.get("text") or "").strip()
    email = data.get("email")
    name = data.get("name")
    if not text:
        return jsonify({"error": "text is required"}), 400
    reply, ticket_id = chat_turn(session_id, text, email=email, name=name)
    return jsonify({"reply": reply, "ticket_id": ticket_id})

@app.route("/tickets/<int:ticket_id>", methods=["GET"])
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

@app.route("/tickets", methods=["GET"])
def list_tickets():
    status = request.args.get("status")
    with get_conn() as conn:
        q = ("SELECT id, customer_id, created_utc, updated_utc, order_id, issue_type, status, last_message "
             "FROM tickets ")
        args = ()
        if status:
            q += "WHERE status=? "
            args = (status,)
        q += "ORDER BY id DESC"
        rows = [dict(r) for r in conn.execute(q, args)]
    return jsonify({"tickets": rows})

@app.route("/tickets/<int:ticket_id>", methods=["PATCH"])
def update_ticket(ticket_id: int):
    data = request.get_json(force=True, silent=True) or {}
    status = data.get("status")
    if status not in {"open","in_progress","resolved","closed"}:
        return jsonify({"error":"invalid status"}), 400
    if not get_ticket(ticket_id):
        return jsonify({"error":"ticket not found"}), 404
    set_status(ticket_id, status)
    return jsonify({"ok": True, "ticket_id": ticket_id, "status": status})

if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=5000, debug=True)
