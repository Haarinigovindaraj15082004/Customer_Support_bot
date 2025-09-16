import sqlite3
from contextlib import contextmanager
from typing import Iterator
from config import settings

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

        # Users who contact support (email optional for chat users)
        c.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE,
            name TEXT
        )""")

        # Tickets table
        c.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER,
            created_utc TEXT DEFAULT (datetime('now')),
            updated_utc TEXT DEFAULT (datetime('now')),
            order_id TEXT,
            issue_type TEXT,     -- 'defective_item', 'wrong_item', 'other'
            status TEXT,         -- 'open','in_progress','resolved','closed'
            last_message TEXT,
            FOREIGN KEY (customer_id) REFERENCES customers(id)
        )""")
        
        # --- add email metadata columns if missing ---
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


        # Simple chat transcripts
        c.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER,
            role TEXT,           -- 'user' or 'assistant'
            text TEXT,
            created_utc TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (ticket_id) REFERENCES tickets(id)
        )""")

        # Lightweight FAQ for rule-based answers 
        c.execute("""
        CREATE TABLE IF NOT EXISTS faq (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question TEXT,
            answer TEXT
        )""")
        
        # ensure 'keywords' column exists 
        c.execute("PRAGMA table_info(faq)")
        cols = [r["name"] for r in c.fetchall()]
        if "keywords" not in cols:
            c.execute("ALTER TABLE faq ADD COLUMN keywords TEXT")

        # Seed a couple of FAQs if empty
        c.execute("SELECT COUNT(*) as n FROM faq")
        if c.fetchone()["n"] == 0:
            c.executemany(
                "INSERT INTO faq(question, answer) VALUES(?,?)",
                [
                    ("return policy", "You can return items within 30 days if unused and in original packaging."),
                    ("delivery time", "Orders ship within 24–48 hours; delivery in 2–5 business days."),
                ],
            )

        conn.commit()
