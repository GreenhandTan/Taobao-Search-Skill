from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class VisualStage(str, Enum):
    IDLE = "idle"
    SEARCHING = "searching"
    RESULTS_REVIEW = "results_review"
    INSPECTING = "inspecting"
    SKU_SELECTING = "sku_selecting"
    ADDING_TO_CART = "adding_to_cart"
    CART_REVIEW = "cart_review"
    DONE = "done"


@dataclass
class VisualState:
    """Persisted state for visual agent mode, keyed by task_id."""
    task_id: str
    mode: str = "visual"
    stage: VisualStage = VisualStage.IDLE
    keyword: str = ""
    search_params: dict[str, Any] = field(default_factory=dict)
    items: list[dict[str, Any]] = field(default_factory=list)
    current_item_index: int | None = None
    current_url: str | None = None
    screenshot_dir: str = ".cache/taobao-search-skill/artifacts"
    session_state_path: str = ".cache/taobao-search-skill/taobao-session.json"
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["stage"] = self.stage.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VisualState":
        stage = data.get("stage", "idle")
        if isinstance(stage, str):
            stage = VisualStage(stage)
        return cls(
            task_id=data.get("task_id", ""),
            mode=data.get("mode", "visual"),
            stage=stage,
            keyword=data.get("keyword", ""),
            search_params=data.get("search_params", {}),
            items=data.get("items", []),
            current_item_index=data.get("current_item_index"),
            current_url=data.get("current_url"),
            screenshot_dir=data.get("screenshot_dir", ".cache/taobao-search-skill/artifacts"),
            session_state_path=data.get("session_state_path", ".cache/taobao-search-skill/taobao-session.json"),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )


@dataclass
class VisualCommandResult:
    """Uniform response envelope for visual mode sub-commands."""
    status: str
    task_id: str
    stage: str
    screenshot: str | None = None
    page_text_summary: str | None = None
    hints: dict[str, Any] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    error: dict[str, Any] | None = None

    def to_output(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "task_id": self.task_id,
            "visual": True,
            "stage": self.stage,
            "screenshot": self.screenshot,
            "page_text_summary": self.page_text_summary,
            "hints": self.hints,
            "data": self.data,
            "steps": self.steps,
            "error": self.error,
        }


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