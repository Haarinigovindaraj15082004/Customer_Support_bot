from dataclasses import dataclass
from typing import Optional

@dataclass
class DetectedIntent:
    type: str                  
    order_id: Optional[str]    
    issue_summary: Optional[str]
