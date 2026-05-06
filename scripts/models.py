from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TaskContext:
    task_id: str
    feishu_message_id: str | None = None
    search_keyword: str = "Sony headphones"
    rating_threshold: float = 0.0
    max_candidates: int = 5
    need_screenshot: bool = True
    manual_approval_required: bool = True
    report_channel: str = "feishu"
    session_state_path: str = ".cache/taobao-search-skill/taobao-session.json"
    session_strategy: str = "storage_state"
    session_auto_save: bool = True
    price_min: float | None = None
    price_max: float | None = None
    min_sales: int | None = None
    require_free_shipping: bool = False
    require_tmall: bool | None = None
    sku_keywords: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StepRecord:
    name: str
    status: str
    message: str = ""
    artifact: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class MatchedItem:
    title: str
    item_id: str | None = None
    price: str | None = None
    price_value: float | None = None
    sales_count: int | None = None
    rating: float | None = None
    free_shipping: bool = False
    is_tmall: bool = False
    url: str | None = None
    cart_added: bool = False


@dataclass
class WorkflowResult:
    task_id: str
    status: str = "failed"
    login_status: str = "unknown"
    session_status: str = "unknown"
    search_status: str = "unknown"
    filter_status: str = "unknown"
    cart_status: str = "unknown"
    matched_items: list[MatchedItem] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    steps: list[StepRecord] = field(default_factory=list)
    error: dict[str, Any] | None = None

    def add_step(self, name: str, status: str, message: str = "", artifact: str | None = None, **details: Any) -> None:
        self.steps.append(
            StepRecord(
                name=name,
                status=status,
                message=message,
                artifact=artifact,
                details=details,
            )
        )

    # Serialization lives in taobao.py:_result_to_output() — agent-facing JSON shape.
    # Keeping models.py free of presentation concerns.