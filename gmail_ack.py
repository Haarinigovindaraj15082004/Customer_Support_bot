import base64
import email
import os
import time
from typing import Optional
from datetime import datetime
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from config import settings
from agent import detect_intent, answer_faq_from_db, infer_issue_label_from_text
from ticketing import get_or_create_customer, create_ticket, set_ticket_email_meta

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]


def gmail_service():
    """
    Auth using JSON token at settings.GOOGLE_TOKEN_JSON (created on first run).
    """
    creds = None
    token_path = settings.GOOGLE_TOKEN_JSON

    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None
        if not creds:
            flow = InstalledAppFlow.from_client_secrets_file(
                settings.GOOGLE_CLIENT_SECRETS_FILE, SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open(token_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def _parse_email(msg) -> tuple[str, str, str]:
    """
    Returns (from_header, subject, body_text)
    """
    headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
    from_header = headers.get("from", "unknown")
    subject = headers.get("subject", "(no subject)")

    body_text = "(no body)"
    payload = msg.get("payload", {})
    if "parts" in payload:
        # try find a text
        for part in payload["parts"]:
            if part.get("mimeType") == "text/plain":
                data = part["body"].get("data")
                if data:
                    body_text = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
                    break
    else:
        data = payload.get("body", {}).get("data")
        if data:
            body_text = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")

    return from_header, subject, body_text


def send_acknowledgment(service, to_email: str, ticket_id: int, order_id: Optional[str]):
    subject = f"[Ticket #{ticket_id}] We’ve received your request"
    order_line = f" for Order {order_id}" if order_id else ""
    body = (
        f"Hello,\n\nThanks for contacting us. We’ve created ticket #{ticket_id}{order_line}.\n"
        "Our support team will follow up shortly.\n\nRegards,\nSupport"
    )

    msg = email.message.EmailMessage()
    msg["To"] = to_email
    if settings.SUPPORT_FROM_EMAIL:           
        msg["From"] = settings.SUPPORT_FROM_EMAIL
    msg["Subject"] = subject
    msg.set_content(body)

    encoded = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": encoded}).execute()


def poll_and_ack():
    """
    Poll unread emails, classify, create a ticket with specific issue_type,
    send acknowledgment, save email metadata on the ticket, mark as read.
    """
    service = gmail_service()
    print("Gmail worker running… (Ctrl+C to stop)")
    print(f"Query: {settings.GMAIL_POLL_QUERY} | Interval: {settings.GMAIL_POLL_INTERVAL_SECONDS}s")

    while True:
        try:
            results = service.users().messages().list(
                userId="me",
                q=settings.GMAIL_POLL_QUERY,
                maxResults=10
            ).execute()

            messages = results.get("messages", [])
            if not messages:
                time.sleep(settings.GMAIL_POLL_INTERVAL_SECONDS)
                continue

            for m in messages:
                full = service.users().messages().get(
                    userId="me", id=m["id"], format="full"
                ).execute()

                # basic fields
                from_header, subject, body_text = _parse_email(full)
                from_addr = from_header.split("<")[-1].rstrip(">").strip()
                was_unread = "UNREAD" in full.get("labelIds", [])

                # classify the email text
                text = f"{subject}\n{body_text}"
                intent = detect_intent(text)

                # choose issue_type 
                if intent.type == "defect":
                    issue_type = "defective_item"
                elif intent.type == "wrong_item":
                    issue_type = "wrong_item"
                elif intent.type == "missing_item":
                    issue_type = "missing_item"
                else:
                    match = answer_faq_from_db(text)  
                    if match:
                        _, issue_type = match
                    else:
                        issue_type = infer_issue_label_from_text(text)

                # create / link customer and ticket
                customer_id = get_or_create_customer(email=from_addr)
                ticket_id = create_ticket(
                    customer_id=customer_id,
                    order_id=intent.order_id,                        
                    issue_type=issue_type,                            
                    first_msg=(subject + "\n\n" + body_text)[:1000],
                    source="email"
                )

                # send ack
                send_acknowledgment(service, from_addr, ticket_id, intent.order_id)

                # save email metadata on the ticket
                now_ist = datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(timespec="seconds")
                set_ticket_email_meta(
                ticket_id,
                source="email",
                gmail_message_id=m["id"],
                email_from=from_addr,
                email_subject=subject,
                email_fetched_utc=now_ist,   
                email_ack_sent_utc=now_ist, 
                gmail_was_unread=1 if was_unread else 0
                )

                # mark as read
                service.users().messages().modify(
                    userId="me",
                    id=m["id"],
                    body={"removeLabelIds": ["UNREAD"]}
                ).execute()

            time.sleep(settings.GMAIL_POLL_INTERVAL_SECONDS)

        except HttpError as e:
            print("Gmail API error:", e)
            time.sleep(10)
        except Exception as e:
            print("Worker error:", e)
            time.sleep(10)


if __name__ == "__main__":
    poll_and_ack()
