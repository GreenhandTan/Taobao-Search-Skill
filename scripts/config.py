from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
        return None
    return bool(value)


@dataclass(slots=True)
class OpenClawSkillConfig:
    task_file: Path | None = None
    task_id: str | None = None
    feishu_message_id: str | None = None
    search_keyword: str = "Sony headphones"
    rating_threshold: float = 0.0
    max_candidates: int = 5
    need_screenshot: bool = True
    manual_approval_required: bool = True
    report_channel: str = "feishu"
    browser_name: str = "chromium"
    session_state_path: str = ".cache/taobao-search-skill/taobao-session.json"
    session_strategy: str = "storage_state"
    session_auto_save: bool = True
    price_min: float | None = None
    price_max: float | None = None
    min_sales: int | None = None
    require_free_shipping: bool = False
    require_tmall: bool | None = None
    sku_keywords: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "OpenClawSkillConfig":
        return cls(
            task_id=payload.get("task_id"),
            feishu_message_id=payload.get("feishu_message_id"),
            search_keyword=str(payload.get("search_keyword", "Sony headphones")),
            rating_threshold=float(payload.get("rating_threshold", 0.0)),
            max_candidates=int(payload.get("max_candidates", 5)),
            need_screenshot=bool(payload.get("need_screenshot", True)),
            manual_approval_required=bool(payload.get("manual_approval_required", True)),
            report_channel=str(payload.get("report_channel", "feishu")),
            browser_name=str(payload.get("constraints", {}).get("browser", "chromium")),
            session_state_path=str(payload.get("session_state_path", ".cache/taobao-search-skill/taobao-session.json")),
            session_strategy=str(payload.get("session_strategy", "storage_state")),
            session_auto_save=bool(payload.get("session_auto_save", True)),
            price_min=_optional_float(payload.get("price_min")),
            price_max=_optional_float(payload.get("price_max")),
            min_sales=_optional_int(payload.get("min_sales")),
            require_free_shipping=bool(payload.get("require_free_shipping", False)),
            require_tmall=_optional_bool(payload.get("require_tmall")),
            sku_keywords=_optional_str(payload.get("sku_keywords")),
        )