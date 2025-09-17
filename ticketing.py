from typing import Optional
from db import get_conn
from datetime import datetime
from zoneinfo import ZoneInfo

def set_ticket_email_meta(ticket_id: int, **meta) -> None:
    """
    Save email-related metadata on a ticket and bump updated_utc.
    Accepts keys: source, gmail_message_id, email_from, email_subject,
                  email_fetched_utc, email_ack_sent_utc, gmail_was_unread
    """
    cols, vals = [], []
    for k in ("source","gmail_message_id","email_from","email_subject",
              "email_fetched_utc","email_ack_sent_utc","gmail_was_unread"):
        if k in meta and meta[k] is not None:
            cols.append(f"{k}=?")
            vals.append(meta[k])
    if not cols:
        return
    vals.append(datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(timespec="seconds"))
    vals.append(ticket_id)
    with get_conn() as conn:
        conn.execute(f"UPDATE tickets SET {', '.join(cols)}, updated_utc=? WHERE id=?", vals)

def get_or_create_customer(email: Optional[str], name: Optional[str] = None) -> int:
    with get_conn() as conn:
        c = conn.cursor()
        if email:
            c.execute("SELECT id FROM customers WHERE email = ?", (email,))
            row = c.fetchone()
            if row:
                return row["id"]
            c.execute("INSERT INTO customers(email, name) VALUES(?,?)", (email, name))
            return c.lastrowid
        else:
            # Anonymous chat customer
            c.execute("INSERT INTO customers(email, name) VALUES(?,?)", (None, name))
            return c.lastrowid

def create_ticket(
    customer_id: int,
    order_id: Optional[str],
    issue_type: str,
    first_msg: str,
    *,
    source: str = "chat",
) -> int:

    now_ist = datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(timespec="seconds")

    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
          INSERT INTO tickets (customer_id, order_id, issue_type, status, last_message, created_utc, updated_utc, source)
          VALUES (?, ?, ?, 'open', ?, ?, ?, ?)
        """, (customer_id, order_id, issue_type, first_msg, now_ist, now_ist, source))
        ticket_id = c.lastrowid

        c.execute(
            "INSERT INTO messages (ticket_id, role, text) VALUES (?, ?, ?)",
            (ticket_id, "user", first_msg)
        )

    return ticket_id

def append_message(ticket_id: int, role: str, text: str) -> None:
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO messages(ticket_id, role, text) VALUES (?,?,?)",
                  (ticket_id, role, text))
        c.execute("UPDATE tickets SET last_message = ?, updated_utc = datetime('now') WHERE id = ?",
                  (text, ticket_id))

def get_ticket(ticket_id: int) -> Optional[dict]:
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
        row = c.fetchone()
        return dict(row) if row else None

def find_open_ticket_by_order(customer_id: int, order_id: str) -> Optional[int]:
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""SELECT id FROM tickets
                     WHERE customer_id = ? AND order_id = ? AND status != 'closed'
                  """, (customer_id, order_id))
        row = c.fetchone()
        return row["id"] if row else None

def set_status(ticket_id: int, status: str) -> None:
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE tickets SET status = ?, updated_utc = datetime('now') WHERE id = ?",
                  (status, ticket_id))
