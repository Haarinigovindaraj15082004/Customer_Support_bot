# db.py
import sqlite3
from contextlib import contextmanager
from typing import Iterator, Optional
from config import settings
from datetime import datetime, timedelta, timezone  

def _scalar(conn, sql, params=()):
    row = conn.execute(sql, params).fetchone()
    return None if row is None else list(row)[0]

def _rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]

def _day(date_str: str) -> str:
    return date_str[:10] if date_str else None

@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(settings.DATABASE_URL)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with get_conn() as conn:
        c = conn.cursor()

        c.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE,
            name  TEXT
        )""")

        c.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER,
            created_utc TEXT DEFAULT (datetime('now')),
            updated_utc TEXT DEFAULT (datetime('now')),
            order_id TEXT,
            issue_type TEXT,     -- 'DEFECTIVE_ITEM','WRONG_ITEM','OTHER', etc.
            status TEXT,         -- 'open','in_progress','resolved','closed'
            last_message TEXT,
            FOREIGN KEY (customer_id) REFERENCES customers(id)
        )""")

        c.execute("PRAGMA table_info(tickets)")
        tcols = {r["name"] for r in c.fetchall()}

        def add(col, sql_type):
            if col not in tcols:
                c.execute(f"ALTER TABLE tickets ADD COLUMN {col} {sql_type}")

        add("source", "TEXT")
        add("gmail_message_id", "TEXT")
        add("email_from", "TEXT")
        add("email_subject", "TEXT")
        add("email_fetched_utc", "TEXT")
        add("email_ack_sent_utc", "TEXT")
        add("gmail_was_unread", "INTEGER")
        add("priority", "TEXT") 

        c.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER,
            role TEXT,           -- 'user' or 'assistant'
            text TEXT,
            created_utc TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (ticket_id) REFERENCES tickets(id)
        )""")

        c.execute("""
        CREATE TABLE IF NOT EXISTS faq (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question TEXT,
            answer   TEXT
        )""")

        c.execute("PRAGMA table_info(faq)")
        cols = [r["name"] for r in c.fetchall()]
        if "keywords" not in cols:
            c.execute("ALTER TABLE faq ADD COLUMN keywords TEXT")

        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_faq_question_unique ON faq(question)")
        c.execute("SELECT COUNT(*) as n FROM faq")
        if c.fetchone()["n"] == 0:
            c.executemany(
                "INSERT INTO faq(question, answer) VALUES(?,?)",
                [
                    ("return policy", "You can return items within 30 days if unused and in original packaging."),
                    ("delivery time", "Orders ship within 24–48 hours; delivery in 2–5 business days."),
                ],
            )

        c.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            status   TEXT,                  -- NEW, PAYMENT_PENDING, PACKING, SHIPPED, DELIVERED, CANCELLED
            shipping_address TEXT,
            created_utc TEXT DEFAULT (datetime('now'))
        )""")

        conn.commit()

def get_order_status(order_id: str) -> Optional[str]:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT status FROM orders WHERE order_id = ?", (order_id,))
        row = cur.fetchone()
        return row["status"] if row else None

# ---------------- reporting: summary ----------------
def report_summary(start_utc: str, end_utc: str) -> dict:
    """
    Counts and aggregates between [start_utc, end_utc] (ISO UTC).
    """
    with get_conn() as conn:
        params = (start_utc, end_utc)

        total = _scalar(conn,
            "SELECT COUNT(*) FROM tickets WHERE created_utc >= ? AND created_utc <= ?", params)

        by_status = _rows(conn,
            "SELECT COALESCE(status,'open') AS status, COUNT(*) AS count "
            "FROM tickets WHERE created_utc >= ? AND created_utc <= ? "
            "GROUP BY COALESCE(status,'open')", params)

        by_issue = _rows(conn,
            "SELECT COALESCE(issue_type,'OTHER') AS issue_type, COUNT(*) AS count "
            "FROM tickets WHERE created_utc >= ? AND created_utc <= ? "
            "GROUP BY COALESCE(issue_type,'OTHER') ORDER BY count DESC", params)

        per_day = _rows(conn,
            "SELECT substr(created_utc,1,10) AS day, COUNT(*) AS count "
            "FROM tickets WHERE created_utc >= ? AND created_utc <= ? "
            "GROUP BY day ORDER BY day", params)

        avg_resolution_hours = _scalar(conn,
            "SELECT AVG((julianday(COALESCE(updated_utc, created_utc)) - julianday(created_utc)) * 24.0) "
            "FROM tickets WHERE status = 'closed' AND created_utc >= ? AND created_utc <= ? "
            "AND updated_utc IS NOT NULL", params)

        open_aging = _rows(conn,
            "SELECT CASE "
            "  WHEN (julianday('now') - julianday(created_utc)) * 24 < 24 THEN '<24h' "
            "  WHEN (julianday('now') - julianday(created_utc)) * 24 < 72 THEN '1-3d' "
            "  WHEN (julianday('now') - julianday(created_utc)) * 24 < 168 THEN '3-7d' "
            "  ELSE '7d+' END AS bucket, COUNT(*) AS count "
            "FROM tickets "
            "WHERE COALESCE(status,'open') != 'closed' "
            "AND created_utc >= ? AND created_utc <= ? "
            "GROUP BY bucket "
            "ORDER BY CASE bucket WHEN '<24h' THEN 1 WHEN '1-3d' THEN 2 WHEN '3-7d' THEN 3 ELSE 4 END",
            params)

    return {
        "range": {"from": start_utc, "to": end_utc},
        "total": total or 0,
        "by_status": by_status,
        "by_issue_type": by_issue,
        "created_per_day": per_day,
        "avg_resolution_hours": round(avg_resolution_hours, 2) if avg_resolution_hours else None,
        "open_aging": open_aging,
    }

def utc_range_for(preset: str) -> tuple[str, str]:
    """
    Helper to get [start,end] UTC ISO for 'today' | 'this_week' | 'this_month'.
    Week = Mon 00:00:00 to now. Default: last 7 days.
    """
    now = datetime.utcnow().replace(microsecond=0)
    if preset == "today":
        start = now.replace(hour=0, minute=0, second=0)
    elif preset == "this_week":
        dow = now.weekday() 
        start = (now - timedelta(days=dow)).replace(hour=0, minute=0, second=0)
    elif preset == "this_month":
        start = now.replace(day=1, hour=0, minute=0, second=0)
    else:
        start = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0)
    return start.isoformat(), now.isoformat()

def _where_from_filters(start_utc, end_utc, status=None, priority=None, channel=None, customer_email=None):
    clauses = ["created_utc >= ? AND created_utc <= ?"]
    params = [start_utc, end_utc]
    if status:
        clauses.append("status = ?")
        params.append(status)
    if priority:
        clauses.append("COALESCE(priority,'P2') = ?")
        params.append(priority)
    if channel:
        clauses.append("COALESCE(source,'chat') = ?")
        params.append(channel)
    if customer_email:
        clauses.append("customer_id IN (SELECT id FROM customers WHERE email = ?)")
        params.append(customer_email)
    return " WHERE " + " AND ".join(clauses), tuple(params)

def report_summary_filtered(start_utc, end_utc, **filters):
    where, params = _where_from_filters(start_utc, end_utc, **filters)
    with get_conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM tickets {where}", params).fetchone()[0]
        by_status = [dict(r) for r in conn.execute(f"SELECT status, COUNT(*) AS count FROM tickets {where} GROUP BY status", params)]
        by_issue  = [dict(r) for r in conn.execute(f"SELECT issue_type, COUNT(*) AS count FROM tickets {where} GROUP BY issue_type", params)]
    return {"total": total, "by_status": by_status, "by_issue_type": by_issue}

def report_status_breakdown(start_utc, end_utc, **filters):
    where, params = _where_from_filters(start_utc, end_utc, **filters)
    with get_conn() as conn:
        rows = conn.execute(f"SELECT status, COUNT(*) AS count FROM tickets {where} GROUP BY status", params).fetchall()
    return [dict(r) for r in rows]

def report_priority_breakdown(start_utc, end_utc, **filters):
    where, params = _where_from_filters(start_utc, end_utc, **filters)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT COALESCE(priority,'P2') AS priority, COUNT(*) AS count FROM tickets {where} GROUP BY priority",
            params
        ).fetchall()
    return [dict(r) for r in rows]

def report_channel_breakdown(start_utc, end_utc, **filters):
    where, params = _where_from_filters(start_utc, end_utc, **filters)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT COALESCE(source,'chat') AS channel, COUNT(*) AS count FROM tickets {where} GROUP BY channel",
            params
        ).fetchall()
    return [dict(r) for r in rows]

def report_daily_counts(start_utc, end_utc, **filters):
    where, params = _where_from_filters(start_utc, end_utc, **filters)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT substr(created_utc,1,10) AS day, COUNT(*) AS count FROM tickets {where} GROUP BY day ORDER BY day",
            params
        ).fetchall()
    return [dict(r) for r in rows]

def report_aging_buckets(start_utc, end_utc, **filters):
    where, params = _where_from_filters(start_utc, end_utc, **filters)
    q = (
        f"SELECT CASE "
        f" WHEN (julianday('now') - julianday(created_utc)) * 24 < 24 THEN '0-24h' "
        f" WHEN (julianday('now') - julianday(created_utc)) * 24 < 48 THEN '24-48h' "
        f" WHEN (julianday('now') - julianday(created_utc)) * 24 < 72 THEN '48-72h' "
        f" ELSE '>72h' END AS bucket, COUNT(*) AS count "
        f"FROM tickets {where} AND COALESCE(status,'open') != 'closed' "
        f"GROUP BY bucket "
        f"ORDER BY CASE bucket WHEN '0-24h' THEN 1 WHEN '24-48h' THEN 2 WHEN '48-72h' THEN 3 ELSE 4 END"
    )
    with get_conn() as conn:
        rows = conn.execute(q, params).fetchall()
    return [dict(r) for r in rows]

def report_oldest_open(start_utc, end_utc, limit=10, **filters):
    where, params = _where_from_filters(start_utc, end_utc, **filters)
    q = f"SELECT id, order_id, created_utc FROM tickets {where} AND COALESCE(status,'open') != 'closed' ORDER BY created_utc ASC LIMIT ?"
    with get_conn() as conn:
        rows = conn.execute(q, params + (limit,)).fetchall()
    return [dict(r) for r in rows]
