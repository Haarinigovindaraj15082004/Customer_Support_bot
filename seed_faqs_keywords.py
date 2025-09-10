from db import get_conn

FAQS = [
    # (question, answer, keywords CSV)
    ("return policy",
     "Returns: 30 days if unused and in original packaging. Exchanges subject to stock.",
     "return, returns, exchange, replace, replacement, exchange policy"),
    ("refund timelines",
     "Refunds go back to your original payment method within 5–7 business days after inspection.",
     "refund, refund status, when refund, money back, reversed, reversal"),
    ("delivery time & shipping",
     "We dispatch in 24–48 hours; delivery is usually 2–5 business days. You’ll get a tracking link.",
     "delivery, shipping, when arrive, eta, courier, timeline"),
    ("order tracking",
     "Use the tracking link in your email/SMS. If missing, share your ORDL order ID and we’ll fetch it.",
     "track, tracking, shipment status, where is my order"),
    ("cancellation",
     "You can cancel until the order is packed/shipped. If shipped, refuse delivery or create a return.",
     "cancel, cancellation, stop order"),
    ("address change",
     "We can update the address before dispatch. Share your ORDL order ID and new address.",
     "change address, wrong address, update address"),
    ("cash on delivery",
     "COD is available on eligible pin codes and order totals under the limit.",
     "cod, cash on delivery, pay on delivery"),
    ("payment issues",
     "If payment was debited but the order isn’t visible, it auto-refunds in 5–7 business days. Share your ORDL order ID or transaction reference.",
     "payment failed, money debited, charged, double charged, transaction failed, paid but no order, billing"),
    ("invoice / GST",
     "Download the invoice from Orders after shipment. Add GST details before placing the order for a GST invoice.",
     "invoice, gst, bill, billing"),
    ("warranty",
     "Warranty is as per brand policy. Keep your invoice; service centers may ask for it.",
     "warranty, guarantee, manufacturer warranty"),
    ("size & fit",
     "Check the Size Chart on the product page. If it doesn’t fit, request an exchange/return within 30 days.",
     "size, fit, size chart, too big, too small"),
    ("missing / partial delivery",
      "Multi-item orders may arrive separately. If still missing after the expected date, raise a ticket with your ORDL order ID.",
      "missing, not received, partial, short, one item missing"),
    ("damaged in transit",
     "Sorry! Please share photos and your ORDL order ID; we’ll arrange a replacement/return immediately.",
     "damaged, broken, dented, cracked, bad condition"),
]

with get_conn() as conn:
    for q, a, kws in FAQS:
        row = conn.execute("SELECT id FROM faq WHERE question = ?", (q,)).fetchone()
        if row:
            conn.execute("UPDATE faq SET answer=?, keywords=? WHERE id=?", (a, kws, row["id"]))
        else:
            conn.execute("INSERT INTO faq(question, answer, keywords) VALUES (?,?,?)", (q, a, kws))

print("Seeded/updated FAQ keywords.")
