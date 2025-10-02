import re, json
from typing import Dict, Any, Optional
from groq import Groq
from config import settings

_client: Optional[Groq] = None
def _get_client() -> Optional[Groq]:
    global _client
    if _client is not None:
        return _client
    key = settings.GROQ_API_KEY
    if not key:
        return None
    _client = Groq(api_key=key)
    return _client

def _extract_json(s: str) -> Dict[str, Any]:
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}

_SYSTEM = """You classify online-shopping support messages.
Return STRICT JSON with keys:
intent: one of [defect, wrong_item, missing_item, faq, human, bye, fallback]
order_id: string like ORDL12345 or null
issue_label: short snake_case label (e.g., payment_issues, address_change) or null
confidence: number 0..1
Do not include extra text—JSON ONLY.
"""

def classify(text: str) -> Dict[str, Any]:
    client = _get_client()
    if client is None:
        return {"intent": "fallback", "order_id": None, "issue_label": None, "confidence": 0.0}
    try:
        resp = client.chat.completions.create(
            model="openai/gpt-oss-20b",
            temperature=0.0,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": text},
            ],
        )
        raw = resp.choices[0].message.content or "{}"
        data = _extract_json(raw)
        return {
            "intent": data.get("intent", "fallback"),
            "order_id": data.get("order_id"),
            "issue_label": data.get("issue_label"),
            "confidence": float(data.get("confidence", 0.0)),
        }
    except Exception:
        return {"intent": "fallback", "order_id": None, "issue_label": None, "confidence": 0.0}


_SYSTEM_REWRITE = """You are a helpful ecommerce support assistant.
You will receive:
- user_text: customer's words
- base_answer: factual answer from our KB/policies

Rewrite base_answer so it is warm, clear, and concise. Do NOT invent new facts.
Keep any uncertainty that exists. Return plain text only with a short friendly close.
"""

def rewrite_answer(user_text: str, base_answer: str) -> Optional[str]:
    client = _get_client()
    if not client:
        return None
    try:
        resp = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            temperature=0.5,
            messages=[
                {"role": "system", "content": _SYSTEM_REWRITE},
                {"role": "user", "content": f"user_text:\n{user_text}\n\nbase_answer:\n{base_answer}"},
            ],
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        return None

_SYSTEM_WELCOME = """Write a short, upbeat welcome for an online store support chat.
Tone: friendly and capable (2–4 short sentences).
Explain you can help with orders, returns/exchanges, delivery/tracking, payments/invoices.
Ask for Order ID (ORDL...) if it's order-specific. No promos. Plain text only.
"""

def welcome_message() -> str:
    brand = getattr(settings, "BRAND_NAME", "Cassie")
    hours = getattr(settings, "BRAND_HOURS", "Mon–Fri 9:00–17:00")
    client = _get_client()
    if not client:
        return (f"Hey there! I’m {brand}. I can help with orders, returns/exchanges, delivery & tracking, "
                "payments and invoices. If it’s about a specific order, please share your Order ID (e.g., ORDL12345). "
                f"We’re around {hours}. How can I help today?")
    try:
        resp = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            temperature=0.6,
            messages=[
                {"role": "system", "content": _SYSTEM_WELCOME},
                {"role": "user", "content": f"Brand: {brand}. Hours: {hours}."},
            ],
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        return (f"Hi! I’m {brand}. I can help with orders, returns, delivery, payments, and more. "
                "If it’s about a specific order, share your Order ID (ORDL…).")

# ========================================================================
#                       PRODUCT MANUALS
# ========================================================================


_SYSTEM_MANUAL = """You are a product documentation writer.
Write a USER GUIDE in Markdown for the given product using ONLY the provided facts.
If a fact is unknown, write “Not specified”.
Use exactly these H2 sections in this order:

## Overview
## What's in the Box
## Quick Start
## Usage
## Safety
## Care & Maintenance
## Troubleshooting
## Technical Specs
## Warranty & Support
## FAQ

Be concise, actionable, and non-promotional."""

def _fallback_manual(product: str, facts: Optional[dict] = None) -> str:
    return f"""# {product} — User Guide

## Overview
Not specified

## What's in the Box
Not specified

## Quick Start
1. Charge or power the device (if applicable).
2. Follow on-screen or printed setup steps.
3. Test basic operation.

## Usage
Not specified

## Safety
Not specified

## Care & Maintenance
Not specified

## Troubleshooting
- Issue: Not specified  
  Fix: Not specified

## Technical Specs
Not specified

## Warranty & Support
Not specified

## FAQ
Not specified
"""

def generate_manual_md(product: str, facts: Optional[Dict] = None) -> str:
    client = _get_client()
    payload = {"product": product, "facts": facts or {}}
    if client is None:
        return _fallback_manual(product, facts)
    try:
        resp = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            temperature=0.4,
            messages=[
                {"role": "system", "content": _SYSTEM_MANUAL},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        )
        return (resp.choices[0].message.content or "").strip() or _fallback_manual(product, facts)
    except Exception:
        return _fallback_manual(product, facts)

_SECTION_MAP = {
    "full": "full",
    "overview": "Overview",
    "box": "What's in the Box",
    "whats_in_the_box": "What's in the Box",
    "quick_start": "Quick Start",
    "setup": "Quick Start",
    "usage": "Usage",
    "how_to_use": "Usage",
    "safety": "Safety",
    "care": "Care & Maintenance",
    "maintenance": "Care & Maintenance",
    "troubleshooting": "Troubleshooting",
    "specs": "Technical Specs",
    "technical_specs": "Technical Specs",
    "tech_specs": "Technical Specs",
    "warranty": "Warranty & Support",
    "support": "Warranty & Support",
    "faq": "FAQ",
    "overview": "Overview",
}

def extract_manual_section(md: str, section_key: str) -> str:
    key = (section_key or "quick_start").lower()
    if key == "full":
        return md
    heading = _SECTION_MAP.get(key, "Quick Start")
    parts = re.split(r"\n(?=## )", md)
    for p in parts:
        if p.strip().startswith(f"## {heading}"):
            return p.strip()
    return f"## {heading}\nNot specified"

_SYSTEM_MANUAL = """
You route a user's product-help query to EXACTLY ONE manual section.

Return STRICT JSON ONLY:
{
  "section": "overview|box|quick_start|usage|troubleshooting|safety|care|tech_specs|warranty|faq" or null,
  "product": "<product name or null>",
  "confidence": 0..1
}

Mapping rules (category-agnostic):
- tech_specs → any factual attributes/specs/details:
  materials, fabric, composition, size, dimensions, weight, color options,
  capacity/volume, ingredients, nutrition facts, allergens, certifications/compliance,
  compatibility, features list, battery/power rating, speed/band/throughput (if applicable),
  SKU/model number, included accessories.
- quick_start → setup, first-use, assembly, installation, pairing, initial charge.
- usage → how to use, directions, application, dosage, cooking instructions, styling tips.
- troubleshooting → problems, errors, not working, fixes.
- safety → warnings, hazards, choking, flammable, side effects, age restrictions.
- care → washing/cleaning, maintenance, storage, shelf life.
- box → what's in the box / package contents.
- warranty → warranty, support, contact.
- faq → common questions when none of the above clearly fit.

PRODUCT extraction:
- If the text contains “for <PRODUCT>…”, set product to that substring (no quotes, trim punctuation).
- Otherwise set product to null.

If uncertain, return {"section": null, "product": null, "confidence": 0.0}.
JSON ONLY.
"""

def manual_route(text: str) -> Optional[Dict[str, Any]]:
    client = _get_client()
    if not client:
        return None
    try:
        resp = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            temperature=0.2,
            messages=[
                {"role": "system", "content": _SYSTEM_MANUAL},
                {"role": "user", "content": text},
            ],
        )
        raw = resp.choices[0].message.content or "{}"
        data = _extract_json(raw)
        if not isinstance(data, dict):
            return None
        sec = (data.get("section") or "").strip().lower() or None
        if sec not in {"tech_specs","quick_start","user_guide","troubleshooting","warranty","overview"}:
            sec = None
        return {
            "product": (data.get("product") or "").strip() or None,
            "section": sec,
            "confidence": float(data.get("confidence", 0.0)),
        }
    except Exception:
        return None
