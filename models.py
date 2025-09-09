from dataclasses import dataclass
from typing import Optional

@dataclass
class DetectedIntent:
    type: str                   # 'defect', 'wrong_item', 'faq', 'fallback'
    order_id: Optional[str]     # extracted via regex if present
    issue_summary: Optional[str]
