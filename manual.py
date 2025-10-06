import re
import json
from typing import Optional, Tuple
from db import get_conn

# ------------ small helpers ------------

def _slug(s: str) -> str:
    """Normalize product/section names for consistent storage & fuzzy search."""
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")

def _has_column(table: str, col: str) -> bool:
    with get_conn() as conn:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any((r["name"] == col) for r in rows)

# Placeholder detector used when LLM gives empty/boilerplate text
_PLACEHOLDER_PAT = re.compile(r"(?i)\bnot\s*specified\b")

def _is_placeholder(md: Optional[str]) -> bool:
    if not md:
        return True
    body = md.strip()
    return (not body) or bool(_PLACEHOLDER_PAT.search(body))

def _facts_to_markdown(section: str, facts: dict) -> str:
    """
    Render arbitrary facts into a clean, section-specific markdown block.
    Works for electronics, bottles, apparel—anything.
    """
    title = (section or "details").replace("_", " ").title()
    lines = [f"## {title}"]
    for k, v in (facts or {}).items():
        if v is None:
            continue
        key = str(k).replace("_", " ").strip().title()
        val = str(v).strip()
        if not val:
            continue
        lines.append(f"- **{key}:** {val}")
    # If nothing materialized, keep a minimal non-placeholder header
    return "\n".join(lines) if len(lines) > 1 else f"## {title}\n(No details provided)"

# ------------ persistence API ------------

def upsert_manual(product: str, section: str, markdown: str, facts: dict | None = None) -> int:
    """
    UPSERT (product, section) -> markdown. Returns row id.
    Works with schemas both WITH and WITHOUT the facts_json/updated_utc columns.

    IMPORTANT: If 'markdown' is empty/placeholder and 'facts' exist, we render markdown from facts
    (without altering any of your existing logic paths otherwise).
    """
    p = _slug(product)
    s = _slug(section)

    # Prefer facts -> markdown when LLM gave us a placeholder
    if _is_placeholder(markdown) and facts:
        markdown = _facts_to_markdown(section, facts)

    facts_json = json.dumps(facts or {}, ensure_ascii=False)
    has_facts_col = _has_column("manual", "facts_json")
    has_updated_col = _has_column("manual", "updated_utc")

    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM manual WHERE product=? AND section=?",
            (p, s)
        ).fetchone()

        if row:
            if has_facts_col and has_updated_col:
                conn.execute(
                    "UPDATE manual SET markdown=?, facts_json=?, updated_utc=datetime('now') WHERE id=?",
                    (markdown, facts_json, row["id"])
                )
            elif has_updated_col:
                conn.execute(
                    "UPDATE manual SET markdown=?, updated_utc=datetime('now') WHERE id=?",
                    (markdown, row["id"])
                )
            else:
                conn.execute(
                    "UPDATE manual SET markdown=? WHERE id=?",
                    (markdown, row["id"])
                )
            return row["id"]

        if has_facts_col:
            cur = conn.execute(
                "INSERT INTO manual (product, section, markdown, facts_json) VALUES (?,?,?,?)",
                (p, s, markdown, facts_json)
            )
        else:
            cur = conn.execute(
                "INSERT INTO manual (product, section, markdown) VALUES (?,?,?)",
                (p, s, markdown)
            )
        return cur.lastrowid

def get_manual(product: str, section: str) -> Optional[str]:
    """Exact fetch by (product, section)."""
    p = _slug(product)
    s = _slug(section)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT markdown FROM manual WHERE product=? AND section=?",
            (p, s)
        ).fetchone()
        return None if not row else row["markdown"]

def get_manual_fuzzy(product_text: str, section: str) -> Optional[str]:
    """
    Fuzzy fetch: try exact (slug), then LIKE with tokens (in order), then first/last tokens.
    Meant for queries like "tech specs for Wireless Router AX1800".
    """
    p = _slug(product_text)
    s = _slug(section)
    toks = [t for t in p.split("_") if t]
    if not toks:
        return None

    with get_conn() as conn:
        # 1) exact slug match
        row = conn.execute(
            "SELECT markdown FROM manual WHERE product=? AND section=?",
            (p, s)
        ).fetchone()
        if row:
            return row["markdown"]

        # 2) LIKE with all tokens in order
        like = "%" + "%".join(toks) + "%"
        row = conn.execute(
            "SELECT markdown FROM manual WHERE section=? AND product LIKE ? COLLATE NOCASE",
            (s, like)
        ).fetchone()
        if row:
            return row["markdown"]

        # 3) LIKE with first ... last (handles long names)
        if len(toks) >= 2:
            like2 = f"%{toks[0]}%{toks[-1]}%"
            row = conn.execute(
                "SELECT markdown FROM manual WHERE section=? AND product LIKE ? COLLATE NOCASE",
                (s, like2)
            ).fetchone()
            if row:
                return row["markdown"]

    return None
