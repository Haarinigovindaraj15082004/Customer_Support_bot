from db import get_conn
with get_conn() as conn:
    print("-- customers --")
    for r in conn.execute("SELECT id, email, name FROM customers ORDER BY id"):
        print(dict(r))

    print("\n-- tickets --")
    for r in conn.execute("SELECT id, customer_id, order_id, issue_type, status, last_message FROM tickets ORDER BY id"):
        print(dict(r))

    print("\n-- messages --")
    for r in conn.execute("SELECT id, ticket_id, role, text FROM messages ORDER BY id"):
        print(dict(r))
