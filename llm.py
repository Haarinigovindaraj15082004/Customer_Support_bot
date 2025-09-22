import re, json
from typing import Dict, Any, Optional
from groq import Groq
from config import settings  

_SYSTEM = """You classify online-shopping support messages.
Return STRICT JSON with keys:
intent: one of [defect, wrong_item, missing_item, faq, human, bye, fallback]
order_id: string like ORDL12345 or null
issue_label: short snake_case label (e.g., payment_issues, address_change) or null
confidence: number 0..1
Do not include extra text—JSON ONLY.
"""

_client: Optional[Groq] = None

def _get_client() -> Optional[Groq]:
    """Create the Groq client lazily; return None if no key so we can fail gracefully."""
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

def classify(text: str) -> Dict[str, Any]:
    """
    Return a dict: {intent, order_id, issue_label, confidence}.
    If no API key or any error, return a safe low-confidence fallback.
    """
    client = _get_client()
    if client is None:
        return {"intent": "fallback", "order_id": None, "issue_label": None, "confidence": 0.0}

    try:
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",  
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
