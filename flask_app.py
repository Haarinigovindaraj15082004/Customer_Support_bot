from flask import Flask, request, jsonify
from db import init_db, get_conn
from agent import chat_turn
from ticketing import get_ticket, set_status

app = Flask(__name__)

def row_to_dict(row):
    return dict(row) if row is not None else None

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True})

@app.route("/chat", methods=["POST"])
def chat():
    """
    Body (JSON):
    {
      "session_id": "user-123",           // required (any string)
      "text": "i got a wrong watch",      // required
      "email": "user@example.com",        // optional (helps link customer)
      "name": "John"                      // optional
    }

    Returns:
    {
      "reply": "...",
      "ticket_id": 2   // when a ticket was created or referenced (else null)
    }
    """
    data = request.get_json(force=True, silent=True) or {}
    session_id = data.get("session_id") or "demo-session"
    text = (data.get("text") or "").strip()
    email = data.get("email")
    name = data.get("name")

    if not text:
        return jsonify({"error": "text is required"}), 400

    try:
        reply, ticket_id = chat_turn(session_id, text, email=email, name=name)
        return jsonify({"reply": reply, "ticket_id": ticket_id})
    except Exception as e:
        # Basic error surface (keep simple)
        return jsonify({"error": str(e)}), 500

@app.route("/tickets/<int:ticket_id>", methods=["GET"])
def get_ticket_with_messages(ticket_id: int):
    """
    Returns ticket plus its messages.
    """
    t = get_ticket(ticket_id)
    if not t:
        return jsonify({"error": "ticket not found"}), 404

    with get_conn() as conn:
        msgs = [
            row_to_dict(r) for r in conn.execute(
                "SELECT id, ticket_id, role, text, created_utc "
                "FROM messages WHERE ticket_id = ? ORDER BY id",
                (ticket_id,)
            )
        ]

    return jsonify({"ticket": t, "messages": msgs})

@app.route("/tickets", methods=["GET"])
def list_tickets():
    """
    Optional query: ?status=open|in_progress|resolved|closed
    """
    status = request.args.get("status")
    with get_conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT id, customer_id, created_utc, updated_utc, "
                "order_id, issue_type, status, last_message "
                "FROM tickets WHERE status = ? ORDER BY id DESC",
                (status,)
            )
        else:
            rows = conn.execute(
                "SELECT id, customer_id, created_utc, updated_utc, "
                "order_id, issue_type, status, last_message "
                "FROM tickets ORDER BY id DESC"
            )
        tickets = [row_to_dict(r) for r in rows]
    return jsonify({"tickets": tickets})

@app.route("/tickets/<int:ticket_id>", methods=["PATCH"])
def update_ticket(ticket_id: int):
    """
    Body (JSON): { "status": "resolved" }  # or open/in_progress/closed
    """
    data = request.get_json(force=True, silent=True) or {}
    status = data.get("status")
    if status not in {"open", "in_progress", "resolved", "closed"}:
        return jsonify({"error": "invalid status"}), 400

    if not get_ticket(ticket_id):
        return jsonify({"error": "ticket not found"}), 404

    set_status(ticket_id, status)
    return jsonify({"ok": True, "ticket_id": ticket_id, "status": status})

if __name__ == "__main__":
    # Make sure tables exist, then run the API
    init_db()
    # debug=True is fine for local dev; remove in prod
    app.run(host="127.0.0.1", port=5000, debug=True)
