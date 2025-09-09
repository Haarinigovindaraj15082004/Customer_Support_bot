import base64
import email
import time
from typing import Optional
import os

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from config import settings
from agent import detect_intent
from ticketing import get_or_create_customer, create_ticket

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]

def gmail_service():
    """
    Uses a JSON token file (settings.GOOGLE_TOKEN_JSON) instead of pickle.
    Creates/refreshes it on first run after OAuth.
    """
    creds = None
    token_path = settings.GOOGLE_TOKEN_JSON

    # Load existing token if present
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    # Refresh or run OAuth
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

        # Save the token as JSON
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)

def _parse_email(msg) -> tuple[str, str, str]:
    """
    Returns (from_email_header, subject, body_text)
    """
    headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
    from_email = headers.get("from", "unknown")
    subject = headers.get("subject", "(no subject)")

    body_text = "(no body)"
    if "parts" in msg["payload"]:
        for part in msg["payload"]["parts"]:
            if part.get("mimeType") == "text/plain":
                data = part["body"].get("data")
                if data:
                    body_text = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
                    break
    else:
        data = msg["payload"]["body"].get("data")
        if data:
            body_text = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")

    return from_email, subject, body_text

def send_acknowledgment(service, to_email: str, ticket_id: int, order_id: Optional[str]):
    subject = f"[Ticket #{ticket_id}] We’ve received your request"
    order_line = f" for Order {order_id}" if order_id else ""
    body = (
        f"Hello,\n\nThanks for contacting us. We’ve created ticket #{ticket_id}{order_line}.\n"
        "Our support team will follow up shortly.\n\nRegards,\nSupport"
    )

    message = email.message.EmailMessage()
    message["To"] = to_email
    # SAFER: only set From if you’ve configured SUPPORT_FROM_EMAIL; otherwise let Gmail use the authenticated account.
    if settings.SUPPORT_FROM_EMAIL:
        message["From"] = settings.SUPPORT_FROM_EMAIL
    message["Subject"] = subject
    message.set_content(body)

    encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": encoded_message}).execute()

def poll_and_ack():
    """
    Poll unread emails, create tickets automatically, and send acknowledgments.
    Marks processed messages as read.
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
                full = service.users().messages().get(userId="me", id=m["id"], format="full").execute()

                from_header, subject, body_text = _parse_email(full)
                # Extract plain email from "Name <addr>"
                from_addr = from_header.split("<")[-1].rstrip(">").strip()

                intent = detect_intent(subject + "\n" + body_text)

                customer_id = get_or_create_customer(email=from_addr)
                issue_type = (
                    "defective_item" if intent.type == "defect"
                    else "wrong_item" if intent.type == "wrong_item"
                    else "other"
                )

                ticket_id = create_ticket(
                    customer_id=customer_id,
                    order_id=intent.order_id,
                    issue_type=issue_type,
                    first_msg=(subject + "\n\n" + body_text)[:1000]
                )

                # Send acknowledgment
                send_acknowledgment(service, from_addr, ticket_id, intent.order_id)

                # Mark as read
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
