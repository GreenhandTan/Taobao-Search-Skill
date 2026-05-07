#!/usr/bin/env python3
"""Taobao browser automation executor — pure hands, no brain.

The Agent (guided by SKILL.md) is the decision-maker. This script executes
browser operations and returns structured JSON. It does not decide policy.

Usage:
    python scripts/taobao.py search --keyword "苹果手机" --rating-threshold 0.95
    python scripts/taobao.py resume
    python scripts/taobao.py check-session
    python scripts/taobao.py clear-session
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).parent.resolve()
_WORKFLOW_STATE_FILE = ".cache/taobao-search-skill/workflow-state.json"
_DEFAULT_SESSION_PATH = ".cache/taobao-search-skill/taobao-session.json"
_DEFAULT_ARTIFACT_DIR = ".cache/taobao-search-skill/artifacts"
_VISUAL_STATE_DIR = ".cache/taobao-search-skill/visual-states"

# Real stdout reference — set in main() before redirecting sys.stdout → stderr.
# _output_json() writes to this so JSON always lands on the true stdout.
_JSON_OUT = sys.stdout


def _resolve_path(path_str: str) -> Path:
    path = Path(path_str)
    if not path.is_absolute():
        path = _SCRIPTS_DIR / path
    return path


# ──────────────────────────────────────────────
# Output helpers
# ──────────────────────────────────────────────


def _output_json(data: dict[str, Any]) -> None:
    """Write JSON to the real stdout (preserved before redirect), suppressing broken pipe errors."""
    with suppress(BrokenPipeError):
        json.dump(data, _JSON_OUT, ensure_ascii=False, indent=2)
        _JSON_OUT.write("\n")
        _JSON_OUT.flush()


def _log(msg: str) -> None:
    """Write diagnostic messages to stderr so they never pollute JSON stdout."""
    print(f"[taobao] {msg}", file=sys.stderr)


# ──────────────────────────────────────────────
# Workflow state persistence (for resume)
# ──────────────────────────────────────────────


def _save_workflow_state(stage: str, session_state_path: str, args: dict[str, Any]) -> None:
    state_path = _resolve_path(_WORKFLOW_STATE_FILE)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "stage": stage,
        "session_state_path": session_state_path,
        "args": args,
    }
    with state_path.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    _log(f"workflow state saved: stage={stage}")


def _load_workflow_state() -> dict[str, Any] | None:
    state_path = _resolve_path(_WORKFLOW_STATE_FILE)
    if not state_path.exists():
        return None
    with state_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _clear_workflow_state() -> None:
    state_path = _resolve_path(_WORKFLOW_STATE_FILE)
    if state_path.exists():
        state_path.unlink()
        _log("workflow state cleared")


# ──────────────────────────────────────────────
# Visual State Manager (for --visual mode)
# ──────────────────────────────────────────────


class VisualStateManager:
    """Persist visual agent state between sub-commands, keyed by task_id."""

    def __init__(self, task_id: str) -> None:
        from models import VisualState

        self.task_id = task_id
        state_dir = _resolve_path(_VISUAL_STATE_DIR)
        state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = state_dir / f"{task_id}.json"
        self._VisualState = VisualState

    def save(self, state: Any) -> None:
        state.updated_at = datetime.now(timezone.utc).isoformat()
        with self.state_path.open("w", encoding="utf-8") as f:
            json.dump(state.to_dict(), f, ensure_ascii=False, indent=2)
        _log(f"visual state saved: stage={state.stage.value}")

    def load(self) -> Any | None:
        try:
            from models import VisualState
        except ImportError:
            return None
        if not self.state_path.exists():
            return None
        with self.state_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("mode") != "visual":
            return None
        return VisualState.from_dict(data)

    def delete(self) -> None:
        if self.state_path.exists():
            self.state_path.unlink()
            _log("visual state deleted")


def _visual_result_to_output(
    status: str,
    task_id: str,
    stage: str,
    screenshot: str | None = None,
    page_text: str | None = None,
    hints: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    steps: list[dict[str, Any]] | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "task_id": task_id,
        "visual": True,
        "stage": stage,
        "screenshot": screenshot,
        "page_text_summary": page_text[:500] if page_text else None,
        "hints": hints or {},
        "data": data or {},
        "steps": steps or [],
        "error": error,
    }


def _add_visual_common_args(parser: argparse.ArgumentParser) -> None:
    """Add arguments shared by all visual sub-commands."""
    parser.add_argument("--session-state-path", default=_DEFAULT_SESSION_PATH)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--no-manual-approval", action="store_true",
                        help="Return immediately on login/captcha instead of waiting")


# ──────────────────────────────────────────────
# Subcommand: search
# ──────────────────────────────────────────────


def _cmd_search(args: argparse.Namespace) -> None:
    from browser_adapter import BrowserAdapter
    from config import OpenClawSkillConfig
    from models import TaskContext, WorkflowResult
    from session_flow import SessionFlow
    from session_manager import SessionManager

    # Build payload from CLI args (mirrors run_workflow.py payload structure)
    payload: dict[str, Any] = {
        "task_id": args.task_id,
        "search_keyword": args.keyword,
        "rating_threshold": args.rating_threshold,
        "max_candidates": args.max_candidates,
        "need_screenshot": not args.no_screenshot,
        "manual_approval_required": not args.no_manual_approval,
        "report_channel": args.report_channel,
        "session_state_path": args.session_state_path,
        "session_strategy": args.session_strategy,
        "session_auto_save": not args.no_session_auto_save,
        "price_min": args.price_min,
        "price_max": args.price_max,
        "min_sales": args.min_sales,
        "require_free_shipping": args.require_free_shipping,
        "require_tmall": {"yes": True, "no": False}.get(args.require_tmall),
        "sku_keywords": args.sku_keywords,
        "constraints": {"browser": "chromium", "headless": args.headless},
    }

    config = OpenClawSkillConfig.from_payload(payload)
    context = TaskContext(
        task_id=str(config.task_id or "taobao-search"),
        search_keyword=config.search_keyword,
        rating_threshold=config.rating_threshold,
        max_candidates=config.max_candidates,
        need_screenshot=config.need_screenshot,
        manual_approval_required=config.manual_approval_required,
        report_channel=config.report_channel,
        session_state_path=config.session_state_path,
        session_strategy=config.session_strategy,
        session_auto_save=config.session_auto_save,
        price_min=config.price_min,
        price_max=config.price_max,
        min_sales=config.min_sales,
        require_free_shipping=config.require_free_shipping,
        require_tmall=config.require_tmall,
        sku_keywords=config.sku_keywords,
        raw_payload=payload,
    )

    result = WorkflowResult(task_id=context.task_id)
    browser = BrowserAdapter(
        browser_name=config.browser_name,
        headless=bool(payload.get("constraints", {}).get("headless", False)),
    )
    session_manager = SessionManager(context.session_state_path)
    session_flow = SessionFlow(browser, session_manager)

    try:
        result.add_step("task_received", "success", keyword=context.search_keyword)
        browser.open()
        result.add_step("browser_opened", "success")

        # ── Session restore ──
        restored = False
        if context.session_strategy in {"storage_state", "cookie_localstorage"}:
            restored = session_flow.try_restore()
            result.session_status = "restored" if restored else "missing"
            msg = "restored" if restored else "no persisted session"
            result.add_step("session_restore", "success" if restored else "skipped", message=msg)

        browser.navigate_to_taobao()
        result.add_step("taobao_opened", "success")

        # ── Login check ──
        logged_in = browser.is_logged_in()
        if not logged_in:
            result.login_status = "waiting_manual"
            result.add_step("login_check", "blocked", message="淘宝未登录")

            if not context.manual_approval_required:
                # Return immediately — Agent will tell user
                browser.close()
                _save_workflow_state("awaiting_login", context.session_state_path, payload)
                _output_json({
                    "status": "need_login",
                    "task_id": context.task_id,
                    "session": {"status": result.session_status, "logged_in": False},
                    "message": "淘宝未登录，需要手动登录后重试",
                    "action": "请在弹出的浏览器窗口中完成淘宝登录，完成后告知我继续",
                    "resume_stage": "awaiting_login",
                    "steps": [s.__dict__ for s in result.steps],
                })
                return

            # Wait for user to complete login manually
            browser.ensure_login(manual_approval_required=True, force_manual=True)
            logged_in = browser.is_logged_in()
            if not logged_in:
                browser.close()
                result.status = "partial_success"
                result.error = {"code": "LOGIN_REQUIRED", "message": "登录超时或失败，请重试"}
                result.add_step("workflow_stopped", "blocked", message="login timeout")
                _output_json(_result_to_output(result, context))
                return

            result.login_status = "success"
            result.add_step("login_flow", "success", message="manual login completed")
            if context.session_auto_save:
                session_flow.capture_after_login()
                result.session_status = "captured"
                result.add_step("session_capture", "success")
        else:
            result.login_status = "success"
            result.add_step("login_check", "success", message="登录状态已确认")
            if context.session_auto_save and context.session_strategy in {"storage_state", "cookie_localstorage"}:
                session_flow.capture_after_login()
                result.session_status = "captured"
                result.add_step("session_capture", "success")

        # ── Search ──
        result.search_status = browser.search(context.search_keyword)
        result.add_step("search_submitted", result.search_status, keyword=context.search_keyword)
        result.search_status = browser.wait_for_results()
        result.add_step("search_results_ready", result.search_status)

        if not browser.ensure_search_access(context.manual_approval_required):
            browser.close()
            _save_workflow_state("awaiting_captcha", context.session_state_path, payload)
            _output_json({
                "status": "need_captcha",
                "task_id": context.task_id,
                "session": {"status": result.session_status, "logged_in": True},
                "message": "淘宝风控拦截，需要手动完成验证",
                "action": "请在浏览器中完成滑块验证，完成后告知我继续",
                "resume_stage": "awaiting_captcha",
                "steps": [s.__dict__ for s in result.steps],
            })
            return

        if context.need_screenshot:
            result.evidence.append(browser.capture_evidence("search_results"))

        # ── Collect candidates with basic filters ──
        candidates = browser.collect_candidates(
            context.search_keyword, context.max_candidates,
            price_min=context.price_min, price_max=context.price_max,
            min_sales=context.min_sales, require_free_shipping=context.require_free_shipping,
            require_tmall=context.require_tmall,
        )
        result.filter_status = "success"
        result.add_step("candidates_collected", "success", candidate_count=len(candidates))

        # ── Enrich & add to cart ──
        # Agent is the brain: we enrich ALL items and report ALL data.
        # Rating filtering is Agent's decision, not ours — we just extract and report.
        skipped_items: list[dict[str, Any]] = []
        for item in candidates:
            browser.enrich_item_rating(item)

            cart_ok = browser.add_to_cart(
                item, sku_keywords=context.sku_keywords,
                price_min=context.price_min, price_max=context.price_max,
            )
            if cart_ok is True:
                result.matched_items.append(item)
                result.add_step("item_added", "success", message=item.title, item_id=item.item_id or "")
            elif cart_ok is False:
                skipped_items.append({
                    "title": item.title,
                    "url": item.url,
                    "reason": "sku_or_price_mismatch",
                    "detail": f"SKU/价格不匹配: {context.sku_keywords or '默认规格'}",
                })
                result.add_step("candidate_skipped", "skipped",
                                message=f"{item.title} (SKU/价格不匹配)")

        # ── Confirm cart ──
        result.cart_status = browser.confirm_cart_state()
        result.add_step("cart_confirmed", result.cart_status, item_count=len(result.matched_items))
        if context.need_screenshot:
            result.evidence.append(browser.capture_evidence("cart_result"))
        result.status = "success" if result.matched_items else "partial_success"
        result.add_step("workflow_completed", result.status, matched_count=len(result.matched_items))

        _clear_workflow_state()
        _output_json(_result_to_output(result, context, skipped_items))

    except Exception as exc:
        browser.close()
        result.status = "failed"
        result.error = {"code": "WORKFLOW_ERROR", "message": str(exc), "step": "workflow"}
        result.add_step("workflow_failed", "failed", message=str(exc))
        _output_json(_result_to_output(result, context))
    finally:
        browser.close()


def _build_payload_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "task_id": args.task_id,
        "search_keyword": args.keyword,
        "rating_threshold": args.rating_threshold,
        "max_candidates": args.max_candidates,
        "need_screenshot": not args.no_screenshot,
        "manual_approval_required": not args.no_manual_approval,
        "report_channel": args.report_channel,
        "session_state_path": args.session_state_path,
        "session_strategy": args.session_strategy,
        "session_auto_save": not args.no_session_auto_save,
        "price_min": args.price_min,
        "price_max": args.price_max,
        "min_sales": args.min_sales,
        "require_free_shipping": args.require_free_shipping,
        "require_tmall": {"yes": True, "no": False}.get(args.require_tmall),
        "sku_keywords": args.sku_keywords,
        "constraints": {"browser": "chromium", "headless": args.headless},
    }


def _open_browser_and_restore_session(
    session_state_path: str, headless: bool, strategy: str
) -> tuple[Any, Any, Any]:
    """Shared browser init for visual sub-commands. Returns (browser, session_manager, session_flow)."""
    from browser_adapter import BrowserAdapter
    from session_flow import SessionFlow
    from session_manager import SessionManager

    browser = BrowserAdapter(headless=headless)
    session_manager = SessionManager(session_state_path)
    session_flow = SessionFlow(browser, session_manager)
    browser.open()
    session_flow.try_restore()
    browser.navigate_to_taobao()
    return browser, session_manager, session_flow


def _handle_login_and_captcha(
    browser: Any, session_flow: Any, manual_approval_required: bool,
    session_auto_save: bool, result: Any, task_id: str,
) -> dict[str, Any] | None:
    """Check login/captcha state. Returns error JSON if blocked, None if OK."""
    page = browser._ensure_page()

    # Check login
    if not browser.is_logged_in():
        if not manual_approval_required:
            browser.close()
            return _visual_result_to_output(
                "need_login", task_id, "searching",
                error={"code": "LOGIN_REQUIRED",
                       "message": "淘宝未登录，请在弹出的浏览器窗口中手动登录后告知我继续"},
                hints={"resume_stage": "awaiting_login"},
            )
        browser.ensure_login(manual_approval_required=True, force_manual=True)
        if not browser.is_logged_in():
            browser.close()
            return _visual_result_to_output(
                "need_login", task_id, "idle",
                error={"code": "LOGIN_REQUIRED", "message": "登录超时或失败"},
            )
        if session_auto_save:
            session_flow.capture_after_login()

    # Check CAPTCHA
    if browser._looks_access_blocked(page):
        browser._handle_captcha_if_present(page)
        if browser._looks_access_blocked(page):
            if not manual_approval_required:
                browser.close()
                return _visual_result_to_output(
                    "need_captcha", task_id, "searching",
                    error={"code": "SEARCH_BLOCKED", "message": "需要手动完成验证"},
                    hints={"resume_stage": "awaiting_captcha"},
                )
            browser._wait_for_access_recovery(page)

    return None  # All clear


# ──────────────────────────────────────────────
# Visual sub-command: search --visual
# ──────────────────────────────────────────────


def _cmd_search_visual(args: argparse.Namespace) -> None:
    from config import OpenClawSkillConfig
    from models import TaskContext, VisualStage, WorkflowResult

    payload = _build_payload_from_args(args)
    config = OpenClawSkillConfig.from_payload(payload)
    context = TaskContext(
        task_id=str(config.task_id or f"taobao-visual-{uuid.uuid4().hex[:8]}"),
        search_keyword=config.search_keyword,
        rating_threshold=config.rating_threshold,
        max_candidates=config.max_candidates,
        need_screenshot=True,
        manual_approval_required=config.manual_approval_required,
        session_state_path=config.session_state_path,
        session_strategy=config.session_strategy,
        session_auto_save=config.session_auto_save,
        price_min=config.price_min,
        price_max=config.price_max,
        min_sales=config.min_sales,
        require_free_shipping=config.require_free_shipping,
        require_tmall=config.require_tmall,
    )

    result = WorkflowResult(task_id=context.task_id)
    state_mgr = VisualStateManager(context.task_id)

    from models import VisualState as VS
    state = VS(
        task_id=context.task_id,
        keyword=context.search_keyword,
        search_params=payload,
        session_state_path=context.session_state_path,
    )

    browser, session_manager, session_flow = _open_browser_and_restore_session(
        context.session_state_path, args.headless, context.session_strategy,
    )

    try:
        result.add_step("task_received", "success", keyword=context.search_keyword)
        result.add_step("browser_opened", "success")

        # Login check
        blocked = _handle_login_and_captcha(
            browser, session_flow, context.manual_approval_required,
            context.session_auto_save, result, context.task_id,
        )
        if blocked:
            _output_json(blocked)
            return

        result.add_step("login_check", "success", message="登录状态已确认")

        # Search
        browser.search(context.search_keyword)
        browser.wait_for_results()
        result.add_step("search_results_ready", "success", keyword=context.search_keyword)

        # Screenshot search results
        screenshot = browser.capture_viewport_screenshot(f"search_{context.task_id}")

        # Collect basic candidate info (no enrichment, no cart)
        candidates = browser.collect_candidates(
            context.search_keyword, context.max_candidates,
            price_min=context.price_min, price_max=context.price_max,
            min_sales=context.min_sales,
            require_free_shipping=context.require_free_shipping,
            require_tmall=context.require_tmall,
        )

        items = []
        for idx, item in enumerate(candidates):
            items.append({
                "index": idx,
                "title": item.title,
                "url": item.url,
                "price_snippet": item.price,
                "price_value": item.price_value,
                "sales_count": item.sales_count,
                "rating": item.rating,
                "is_tmall": item.is_tmall,
                "free_shipping": item.free_shipping,
                "inspected": False,
                "cart_added": False,
            })

        state.items = items
        state.stage = VS.RESULTS_REVIEW
        state_mgr.save(state)

        # Also save workflow state for resume compatibility
        _save_workflow_state("visual_search_completed", context.session_state_path, payload)

        page_text = browser.get_page_text(2000)

        _output_json(_visual_result_to_output(
            "success", context.task_id, "results_review",
            screenshot=screenshot,
            page_text=page_text,
            hints={
                "step": "search_results",
                "keyword": context.search_keyword,
                "items_collected": len(items),
                "next": "Use Read tool to view the screenshot, then call 'open --task-id {} --index N' to inspect an item".format(context.task_id),
            },
            data={"items": items},
            steps=[s.__dict__ for s in result.steps],
        ))

    except Exception as exc:
        state_mgr.delete()
        _output_json(_visual_result_to_output(
            "failed", context.task_id, "idle",
            error={"code": "WORKFLOW_ERROR", "message": str(exc)},
        ))
    finally:
        browser.close()


# ──────────────────────────────────────────────
# Visual sub-command: open
# ──────────────────────────────────────────────


def _cmd_open(args: argparse.Namespace) -> None:
    from models import VisualStage

    state_mgr = VisualStateManager(args.task_id)
    state = state_mgr.load()
    if state is None:
        _output_json(_visual_result_to_output(
            "failed", args.task_id, "idle",
            error={"code": "NO_VISUAL_STATE", "message": "没有视觉任务状态，请先运行 search --visual"},
        ))
        return

    index = args.index
    if index < 0 or index >= len(state.items):
        _output_json(_visual_result_to_output(
            "failed", args.task_id, state.stage.value,
            error={"code": "INVALID_INDEX", "message": f"Index {index} 超出范围 (0-{len(state.items)-1})"},
        ))
        return

    item = state.items[index]
    browser, session_manager, session_flow = _open_browser_and_restore_session(
        state.session_state_path, args.headless, "storage_state",
    )

    try:
        blocked = _handle_login_and_captcha(
            browser, session_flow, not args.no_manual_approval,
            True, None, state.task_id,
        )
        if blocked:
            _output_json(blocked)
            return

        page = browser._ensure_page()
        url = item.get("url", "")
        if not url:
            _output_json(_visual_result_to_output(
                "failed", state.task_id, state.stage.value,
                error={"code": "NO_URL", "message": "Item has no URL"},
            ))
            return

        _log(f"navigating to item[{index}]: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        with suppress(Exception):
            page.wait_for_load_state("networkidle", timeout=10000)
        browser._handle_captcha_if_present(page)
        browser._simulate_browsing(page, max_scroll=800)
        browser._human_wait(0.5, 1.5)

        screenshot = browser.capture_viewport_screenshot(f"item_{index}_{state.task_id}")
        page_text = browser.get_page_text(3000)
        sku_groups = browser.get_sku_structure()

        # Enrich price from detail page
        detail_price = browser._extract_detail_price(page)
        if detail_price is not None:
            item["price_snippet"] = f"¥{detail_price:.2f}"
            item["price_value"] = detail_price

        # Update state
        state.current_item_index = index
        state.current_url = url
        state.stage = VisualStage.INSPECTING
        state.items[index]["inspected"] = True
        state_mgr.save(state)

        _output_json(_visual_result_to_output(
            "success", state.task_id, "inspecting",
            screenshot=screenshot,
            page_text=page_text,
            hints={
                "step": "product_detail",
                "item_index": index,
                "item_title": item.get("title", ""),
                "sku_groups_found": len(sku_groups),
                "detail_price": detail_price,
                "next": "Read screenshot. If SKU selection needed: 'sku-select --task-id {} --label <group> --value <option>'. Otherwise: 'cart-add --task-id {}'".format(state.task_id, state.task_id),
            },
            data={
                "item": item,
                "sku_groups": sku_groups,
                "detail_price": detail_price,
            },
        ))

    except Exception as exc:
        _output_json(_visual_result_to_output(
            "failed", state.task_id, state.stage.value if state else "idle",
            error={"code": "WORKFLOW_ERROR", "message": str(exc)},
        ))
    finally:
        browser.close()


# ──────────────────────────────────────────────
# Visual sub-command: sku-select
# ──────────────────────────────────────────────


def _cmd_sku_select(args: argparse.Namespace) -> None:
    from models import VisualStage

    state_mgr = VisualStateManager(args.task_id)
    state = state_mgr.load()
    if state is None:
        _output_json(_visual_result_to_output(
            "failed", args.task_id, "idle",
            error={"code": "NO_VISUAL_STATE", "message": "没有视觉任务状态"},
        ))
        return

    browser, session_manager, session_flow = _open_browser_and_restore_session(
        state.session_state_path, args.headless, "storage_state",
    )

    try:
        blocked = _handle_login_and_captcha(
            browser, session_flow, not args.no_manual_approval,
            True, None, state.task_id,
        )
        if blocked:
            _output_json(blocked)
            return

        page = browser._ensure_page()
        if state.current_url:
            page.goto(state.current_url, wait_until="domcontentloaded", timeout=30000)
            with suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=8000)
            browser._human_wait(1, 2)

        label = args.label
        value = args.value
        _log(f"selecting SKU: label='{label}' value='{value}'")
        success = browser.select_sku(label, value)
        browser._human_wait(1, 2.5)

        screenshot = browser.capture_viewport_screenshot(
            f"sku_{state.current_item_index}_{state.task_id}"
        )
        page_text = browser.get_page_text(2000)
        new_price = browser._extract_detail_price(page)
        sku_groups = browser.get_sku_structure()

        if state.current_item_index is not None:
            item = state.items[state.current_item_index]
            if new_price is not None:
                item["price_snippet"] = f"¥{new_price:.2f}"
                item["price_value"] = new_price

        state.stage = VisualStage.SKU_SELECTING
        state_mgr.save(state)

        _output_json(_visual_result_to_output(
            "success" if success else "partial_success",
            state.task_id, "sku_selecting",
            screenshot=screenshot,
            page_text=page_text,
            hints={
                "step": "sku_selection",
                "selection_made": success,
                "label_queried": label,
                "value_queried": value,
                "new_price": new_price,
                "remaining_sku_groups": sku_groups,
                "next": "Read screenshot. If more SKU selections needed: repeat 'sku-select'. Otherwise: 'cart-add --task-id {}'".format(state.task_id),
            },
            data={
                "sku_selected": success,
                "new_price": new_price,
                "sku_groups": sku_groups,
            },
        ))

    except Exception as exc:
        _output_json(_visual_result_to_output(
            "failed", state.task_id, state.stage.value if state else "idle",
            error={"code": "WORKFLOW_ERROR", "message": str(exc)},
        ))
    finally:
        browser.close()


# ──────────────────────────────────────────────
# Visual sub-command: cart-add
# ──────────────────────────────────────────────


def _cmd_cart_add(args: argparse.Namespace) -> None:
    from models import VisualStage
    from taobao_selectors import ADD_TO_CART_BUTTONS, CART_CONFIRM_POPUP

    state_mgr = VisualStateManager(args.task_id)
    state = state_mgr.load()
    if state is None:
        _output_json(_visual_result_to_output(
            "failed", args.task_id, "idle",
            error={"code": "NO_VISUAL_STATE", "message": "没有视觉任务状态"},
        ))
        return

    browser, session_manager, session_flow = _open_browser_and_restore_session(
        state.session_state_path, args.headless, "storage_state",
    )

    try:
        blocked = _handle_login_and_captcha(
            browser, session_flow, not args.no_manual_approval,
            True, None, state.task_id,
        )
        if blocked:
            _output_json(blocked)
            return

        page = browser._ensure_page()
        if state.current_url:
            page.goto(state.current_url, wait_until="domcontentloaded", timeout=30000)
            with suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=8000)
            browser._handle_captcha_if_present(page)
            browser._human_wait(1, 2)

        button = browser._find_first_visible_locator(page, ADD_TO_CART_BUTTONS)
        if button is None:
            screenshot = browser.capture_viewport_screenshot(f"cart_add_fail_{state.task_id}")
            _output_json(_visual_result_to_output(
                "failed", state.task_id, state.stage.value,
                screenshot=screenshot,
                error={"code": "NO_ADD_TO_CART_BUTTON", "message": "找不到加入购物车按钮，请检查截图确认页面状态"},
                hints={"step": "add_to_cart", "suggestion": "Use 'decide' to click by text or scroll to find the button"},
            ))
            return

        browser._human_click(page, button)
        browser._human_wait(1.5, 3)

        # Check for success popup
        confirmed = browser._find_first_visible_locator(page, CART_CONFIRM_POPUP)

        screenshot = browser.capture_viewport_screenshot(
            f"cart_add_{state.current_item_index}_{state.task_id}"
        )
        page_text = browser.get_page_text(1000)

        if state.current_item_index is not None:
            state.items[state.current_item_index]["cart_added"] = True
        state.stage = VisualStage.ADDING_TO_CART
        state_mgr.save(state)

        _output_json(_visual_result_to_output(
            "success" if confirmed else "partial_success",
            state.task_id, "adding_to_cart",
            screenshot=screenshot,
            page_text=page_text,
            hints={
                "step": "add_to_cart",
                "confirmation_seen": confirmed is not None,
                "item_index": state.current_item_index,
                "next": "Read screenshot to confirm. Then: 'cart-view --task-id {}' to review, or 'open --task-id {} --index N' for next item.".format(state.task_id, state.task_id),
            },
            data={"cart_added": confirmed is not None},
        ))

    except Exception as exc:
        _output_json(_visual_result_to_output(
            "failed", state.task_id, state.stage.value if state else "idle",
            error={"code": "WORKFLOW_ERROR", "message": str(exc)},
        ))
    finally:
        browser.close()


# ──────────────────────────────────────────────
# Visual sub-command: cart-view
# ──────────────────────────────────────────────


def _cmd_cart_view(args: argparse.Namespace) -> None:
    from models import VisualStage
    from taobao_selectors import CART_ITEM_SELECTORS

    state_mgr = VisualStateManager(args.task_id)
    state = state_mgr.load()
    if state is None:
        _output_json(_visual_result_to_output(
            "failed", args.task_id, "idle",
            error={"code": "NO_VISUAL_STATE", "message": "没有视觉任务状态"},
        ))
        return

    browser, session_manager, session_flow = _open_browser_and_restore_session(
        state.session_state_path, args.headless, "storage_state",
    )

    try:
        blocked = _handle_login_and_captcha(
            browser, session_flow, not args.no_manual_approval,
            True, None, state.task_id,
        )
        if blocked:
            _output_json(blocked)
            return

        page = browser._ensure_page()
        page.goto("https://cart.taobao.com/cart.htm", wait_until="domcontentloaded", timeout=20000)
        with suppress(Exception):
            page.wait_for_load_state("networkidle", timeout=10000)
        browser._handle_captcha_if_present(page)
        browser._human_wait(2, 4)

        screenshot = browser.capture_viewport_screenshot(f"cart_{state.task_id}")
        page_text = browser.get_page_text(2000)

        item_count = 0
        for sel in CART_ITEM_SELECTORS:
            with suppress(Exception):
                item_count = page.locator(sel).count()
                if item_count > 0:
                    break

        state.stage = VisualStage.CART_REVIEW
        state_mgr.save(state)

        # Count items successfully added
        added_count = sum(1 for item in state.items if item.get("cart_added"))

        _output_json(_visual_result_to_output(
            "success", state.task_id, "cart_review",
            screenshot=screenshot,
            page_text=page_text,
            hints={
                "step": "cart_review",
                "cart_item_count_detected": item_count,
                "items_added_in_session": added_count,
                "next": "Read screenshot to confirm all items are in cart. Report to user. If more items needed: 'open --task-id {} --index N'.".format(state.task_id),
            },
            data={
                "cart_item_count": item_count,
                "items_added": added_count,
                "items": state.items,
            },
        ))

    except Exception as exc:
        _output_json(_visual_result_to_output(
            "failed", state.task_id, state.stage.value if state else "idle",
            error={"code": "WORKFLOW_ERROR", "message": str(exc)},
        ))
    finally:
        browser.close()


# ──────────────────────────────────────────────
# Visual sub-command: dom (extract visible DOM)
# ──────────────────────────────────────────────


def _cmd_dom(args: argparse.Namespace) -> None:
    state_mgr = VisualStateManager(args.task_id)
    state = state_mgr.load()

    browser, session_manager, session_flow = _open_browser_and_restore_session(
        (state.session_state_path if state else _DEFAULT_SESSION_PATH),
        args.headless, "storage_state",
    )

    try:
        blocked = _handle_login_and_captcha(
            browser, session_flow, not args.no_manual_approval,
            True, None, args.task_id,
        )
        if blocked:
            _output_json(blocked)
            return

        page = browser._ensure_page()
        if args.url:
            page.goto(args.url, wait_until="domcontentloaded", timeout=30000)
            with suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=8000)
            browser._human_wait(1, 2)
        elif state and state.current_url:
            page.goto(state.current_url, wait_until="domcontentloaded", timeout=30000)
            with suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=8000)
            browser._human_wait(1, 2)

        dom_data = browser.get_visible_dom()
        screenshot = browser.capture_viewport_screenshot(f"dom_{args.task_id}")

        stage = state.stage.value if state else "idle"
        _output_json(_visual_result_to_output(
            "success", args.task_id, stage,
            screenshot=screenshot,
            hints={
                "step": "dom_extraction",
                "elements_found": len(dom_data.get("elements", [])),
                "url": dom_data.get("url", ""),
            },
            data={"dom": dom_data},
        ))

    except Exception as exc:
        _output_json(_visual_result_to_output(
            "failed", args.task_id, "idle",
            error={"code": "WORKFLOW_ERROR", "message": str(exc)},
        ))
    finally:
        browser.close()


# ──────────────────────────────────────────────
# Visual sub-command: wait
# ──────────────────────────────────────────────


def _cmd_wait(args: argparse.Namespace) -> None:
    state_mgr = VisualStateManager(args.task_id)
    state = state_mgr.load()

    browser, session_manager, session_flow = _open_browser_and_restore_session(
        (state.session_state_path if state else _DEFAULT_SESSION_PATH),
        args.headless, "storage_state",
    )

    try:
        page = browser._ensure_page()
        if args.url:
            page.goto(args.url, wait_until="domcontentloaded", timeout=30000)
        elif state and state.current_url:
            page.goto(state.current_url, wait_until="domcontentloaded", timeout=30000)

        condition = args.condition or ""
        timeout_ms = args.timeout_ms or 10000
        waited = False

        if condition.startswith("selector:"):
            waited = browser.wait_for_element(selector=condition[9:], timeout_ms=timeout_ms)
        elif condition.startswith("text:"):
            waited = browser.wait_for_element(text=condition[5:], timeout_ms=timeout_ms)
        elif condition in ("networkidle", "network_idle"):
            with suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=timeout_ms)
                waited = True
        else:
            browser._human_wait(float(args.wait_seconds or 2), float(args.wait_seconds or 2) + 0.5)
            waited = True

        screenshot = browser.capture_viewport_screenshot(f"wait_{args.task_id}")
        stage = state.stage.value if state else "idle"

        _output_json(_visual_result_to_output(
            "success" if waited else "partial_success",
            args.task_id, stage,
            screenshot=screenshot,
            hints={
                "step": "wait",
                "condition": condition,
                "condition_met": waited,
            },
            data={"waited": waited},
        ))

    except Exception as exc:
        _output_json(_visual_result_to_output(
            "failed", args.task_id, "idle",
            error={"code": "WORKFLOW_ERROR", "message": str(exc)},
        ))
    finally:
        browser.close()


# ──────────────────────────────────────────────
# Visual sub-command: decide (generic action)
# ──────────────────────────────────────────────


def _cmd_decide(args: argparse.Namespace) -> None:
    """Generic action executor for unexpected page states."""
    state_mgr = VisualStateManager(args.task_id)
    state = state_mgr.load()

    browser, session_manager, session_flow = _open_browser_and_restore_session(
        (state.session_state_path if state else _DEFAULT_SESSION_PATH),
        args.headless, "storage_state",
    )

    try:
        blocked = _handle_login_and_captcha(
            browser, session_flow, not args.no_manual_approval,
            True, None, args.task_id,
        )
        if blocked:
            _output_json(blocked)
            return

        page = browser._ensure_page()
        if args.url:
            page.goto(args.url, wait_until="domcontentloaded", timeout=30000)
            browser._human_wait(1, 2)
        elif state and state.current_url:
            page.goto(state.current_url, wait_until="domcontentloaded", timeout=30000)
            browser._human_wait(1, 2)

        action = args.action
        value = args.value or ""
        result_status = "success"
        result_data: dict[str, Any] = {"action": action, "value": value}

        if action == "click":
            clicked = browser.click_element_by_text(value)
            result_status = "success" if clicked else "error"
            result_data["clicked"] = clicked
        elif action == "scroll":
            parts = value.split(":") if value else ["down", "500"]
            direction = parts[0] if len(parts) > 0 else "down"
            amount = int(parts[1]) if len(parts) > 1 else 500
            browser.scroll_page(direction, amount)
            result_data["direction"] = direction
            result_data["amount"] = amount
        elif action == "hover":
            # Hover over element containing text
            if value:
                with suppress(Exception):
                    locator = page.locator(f"text={value}").first
                    locator.hover(timeout=5000)
                    browser._human_wait(0.5, 1)
            result_data["hovered"] = bool(value)
        elif action == "press":
            page.keyboard.press(value or "Enter")
            browser._human_wait(0.3, 0.8)
        elif action == "type":
            parts = value.split("||", 1)
            selector_text = parts[0] if len(parts) > 0 else ""
            input_text = parts[1] if len(parts) > 1 else ""
            if selector_text:
                with suppress(Exception):
                    page.locator(f"text={selector_text}").first.fill(input_text)
            result_data["typed"] = input_text
        elif action == "navigate":
            if value:
                page.goto(value, wait_until="domcontentloaded", timeout=30000)
                browser._human_wait(1, 2)
                result_data["navigated_to"] = value
            else:
                result_status = "error"
                result_data["error"] = "No URL provided for navigate action"
        elif action == "screenshot":
            pass  # Screenshot always taken below
        else:
            result_status = "error"
            result_data["error"] = f"Unknown action: {action}"

        browser._human_wait(0.5, 1.5)
        screenshot = browser.capture_viewport_screenshot(f"decide_{action}_{args.task_id}")
        page_text = browser.get_page_text(2000)
        stage = state.stage.value if state else "idle"

        _output_json(_visual_result_to_output(
            result_status, args.task_id, stage,
            screenshot=screenshot,
            page_text=page_text,
            hints={
                "step": f"decide:{action}",
                "action": action,
                "value": value,
                "result": result_status,
            },
            data=result_data,
        ))

    except Exception as exc:
        _output_json(_visual_result_to_output(
            "failed", args.task_id, "idle",
            error={"code": "WORKFLOW_ERROR", "message": str(exc)},
        ))
    finally:
        browser.close()


def _result_to_output(
    result, context, skipped_items: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "status": result.status,
        "task_id": result.task_id,
        "session": {
            "status": result.session_status,
            "logged_in": result.login_status == "success",
        },
        "search": {
            "status": result.search_status,
            "keyword": context.search_keyword,
            "candidates_found": len(result.matched_items) + (len(skipped_items) if skipped_items else 0),
        },
        "items": [
            {
                "title": item.title,
                "item_id": item.item_id,
                "price": item.price,
                "price_value": item.price_value,
                "sales_count": item.sales_count,
                "rating": item.rating,
                "free_shipping": item.free_shipping,
                "is_tmall": item.is_tmall,
                "url": item.url,
                "cart_added": item.cart_added,
            }
            for item in result.matched_items
        ],
        "skipped": skipped_items or [],
        "evidence": result.evidence,
        "steps": [
            {
                "name": s.name,
                "status": s.status,
                "message": s.message,
                "details": s.details,
            }
            for s in result.steps
        ],
        "error": result.error,
        "resume_stage": None,
    }


# ──────────────────────────────────────────────
# Subcommand: resume
# ──────────────────────────────────────────────


def _cmd_resume(args: argparse.Namespace) -> None:
    state = _load_workflow_state()
    if state is None:
        _output_json({
            "status": "error",
            "error": {"code": "NO_STATE", "message": "没有可恢复的工作流状态，请先运行 search"},
        })
        return

    _log(f"resuming from stage: {state['stage']}")
    saved_args = state.get("args", {})

    # Reconstruct args and re-run search — session is now logged in
    # We rebuild the namespace from saved args and current args
    search_args = _build_search_args(saved_args)
    _cmd_search(search_args)


def _build_search_args(saved: dict[str, Any]) -> argparse.Namespace:
    """Rebuild search namespace from saved workflow state."""
    parser = _build_search_parser()
    ns_dict = {
        "task_id": saved.get("task_id"),
        "keyword": saved.get("search_keyword", "Sony headphones"),
        "rating_threshold": saved.get("rating_threshold", 0.0),
        "max_candidates": saved.get("max_candidates", 5),
        "no_screenshot": not saved.get("need_screenshot", True),
        "no_manual_approval": not saved.get("manual_approval_required", True),
        "report_channel": saved.get("report_channel", "feishu"),
        "session_state_path": saved.get("session_state_path", _DEFAULT_SESSION_PATH),
        "session_strategy": saved.get("session_strategy", "storage_state"),
        "no_session_auto_save": not saved.get("session_auto_save", True),
        "price_min": saved.get("price_min"),
        "price_max": saved.get("price_max"),
        "min_sales": saved.get("min_sales"),
        "require_free_shipping": saved.get("require_free_shipping", False),
        "require_tmall": _bool_to_yes_no(saved.get("require_tmall")),
        "sku_keywords": saved.get("sku_keywords"),
        "headless": (saved.get("constraints") or {}).get("headless", False),
        "command": "search",
    }
    return argparse.Namespace(**ns_dict)


def _bool_to_yes_no(value: bool | None) -> str | None:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return None


# ──────────────────────────────────────────────
# Subcommand: check-session
# ──────────────────────────────────────────────


def _cmd_check_session(args: argparse.Namespace) -> None:
    session_path = _resolve_path(args.session_state_path or _DEFAULT_SESSION_PATH)

    if not session_path.exists():
        _output_json({
            "status": "success",
            "session_exists": False,
            "session_path": str(session_path),
            "message": "会话文件不存在，首次使用需要手动登录",
        })
        return

    try:
        with session_path.open("r", encoding="utf-8") as f:
            state = json.load(f)

        cookies = state.get("cookies", []) if isinstance(state, dict) else []
        _output_json({
            "status": "success",
            "session_exists": True,
            "session_path": str(session_path),
            "cookie_count": len(cookies),
            "message": f"会话文件存在，包含 {len(cookies)} 个 cookie",
        })
    except Exception as exc:
        _output_json({
            "status": "error",
            "session_exists": True,
            "session_path": str(session_path),
            "error": {"code": "SESSION_READ_ERROR", "message": str(exc)},
        })


# ──────────────────────────────────────────────
# Subcommand: clear-session
# ──────────────────────────────────────────────


def _cmd_clear_session(args: argparse.Namespace) -> None:
    session_path = _resolve_path(args.session_state_path or _DEFAULT_SESSION_PATH)
    removed = False
    if session_path.exists():
        session_path.unlink()
        removed = True
    _clear_workflow_state()
    _output_json({
        "status": "success",
        "session_removed": removed,
        "session_path": str(session_path),
        "message": "会话已清除" if removed else "会话文件不存在，无需清除",
    })


# ──────────────────────────────────────────────
# CLI Argument Builders
# ──────────────────────────────────────────────


def _define_search_args(parser: argparse.ArgumentParser) -> None:
    """Single source of truth for all search subcommand arguments."""
    parser.add_argument("--keyword", default="Sony headphones", help="Search keyword")
    parser.add_argument("--rating-threshold", type=float, default=0.0, help="Min rating threshold, 0=no filter")
    parser.add_argument("--max-candidates", type=int, default=5, help="Max candidates to inspect")
    parser.add_argument("--no-screenshot", action="store_true", help="Disable evidence screenshots")
    parser.add_argument("--no-manual-approval", action="store_true", help="Disable manual takeover wait")
    parser.add_argument("--session-state-path", default=_DEFAULT_SESSION_PATH, help="Session file path")
    parser.add_argument("--session-strategy", default="storage_state",
                        choices=["storage_state", "cookie_localstorage", "none"])
    parser.add_argument("--no-session-auto-save", action="store_true", help="Disable auto-save session after login")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    parser.add_argument("--price-min", type=float, help="Minimum price filter (CNY)")
    parser.add_argument("--price-max", type=float, help="Maximum price filter (CNY)")
    parser.add_argument("--min-sales", type=int, help="Minimum sales count filter")
    parser.add_argument("--require-free-shipping", action="store_true", help="Only free shipping items")
    parser.add_argument("--require-tmall", type=str, choices=["yes", "no"], help="Filter by Tmall/Taobao store")
    parser.add_argument("--sku-keywords", type=str, help="SKU spec keywords, space-separated")
    parser.add_argument("--task-id", help="Task identifier")
    parser.add_argument("--report-channel", default="feishu", choices=["feishu"])


def _build_search_parser() -> argparse.ArgumentParser:
    """Create a standalone parser for resume argument reconstruction."""
    parser = argparse.ArgumentParser(add_help=False)
    _define_search_args(parser)
    return parser


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Taobao browser automation executor — pure hands, no brain."
    )
    subparsers = parser.add_subparsers(dest="command", help="Subcommand")

    # search (traditional + visual mode)
    search_parser = subparsers.add_parser("search", help="Run search -> filter -> add-to-cart pipeline")
    _define_search_args(search_parser)
    search_parser.add_argument("--visual", action="store_true",
                               help="Visual agent mode: step-by-step with screenshots for AI decision-making")

    # resume
    resume_parser = subparsers.add_parser("resume", help="Resume workflow after human intervention")
    resume_parser.add_argument("--session-state-path", default=_DEFAULT_SESSION_PATH)

    # check-session
    check_parser = subparsers.add_parser("check-session", help="Check session file status")
    check_parser.add_argument("--session-state-path", default=_DEFAULT_SESSION_PATH)

    # clear-session
    clear_parser = subparsers.add_parser("clear-session", help="Clear persisted session")
    clear_parser.add_argument("--session-state-path", default=_DEFAULT_SESSION_PATH)

    # ── Visual agent sub-commands ──

    # open
    open_parser = subparsers.add_parser("open", help="[Visual] Open Nth search result, screenshot detail page")
    open_parser.add_argument("--task-id", required=True, help="Task ID from search --visual")
    open_parser.add_argument("--index", type=int, required=True, help="Item index from search results")
    _add_visual_common_args(open_parser)

    # sku-select
    sku_parser = subparsers.add_parser("sku-select", help="[Visual] Select SKU option by group label and value text")
    sku_parser.add_argument("--task-id", required=True)
    sku_parser.add_argument("--label", required=True, help="SKU group label, e.g. '颜色' '存储' '版本'")
    sku_parser.add_argument("--value", required=True, help="SKU option value, e.g. '黑色' '512G' 'M4'")
    _add_visual_common_args(sku_parser)

    # cart-add
    cart_add_parser = subparsers.add_parser("cart-add", help="[Visual] Click add-to-cart on current item")
    cart_add_parser.add_argument("--task-id", required=True)
    _add_visual_common_args(cart_add_parser)

    # cart-view
    cart_view_parser = subparsers.add_parser("cart-view", help="[Visual] Open cart page, screenshot contents")
    cart_view_parser.add_argument("--task-id", required=True)
    _add_visual_common_args(cart_view_parser)

    # dom
    dom_parser = subparsers.add_parser("dom", help="[Visual] Extract visible DOM with semantic annotations")
    dom_parser.add_argument("--task-id", required=True)
    dom_parser.add_argument("--url", help="URL to navigate to (default: current state URL)")
    _add_visual_common_args(dom_parser)

    # wait
    wait_parser = subparsers.add_parser("wait", help="[Visual] Wait for condition (selector/text/networkidle)")
    wait_parser.add_argument("--task-id", required=True)
    wait_parser.add_argument("--condition", help="e.g. 'selector:.sku-item', 'text:加入购物车', 'networkidle'")
    wait_parser.add_argument("--timeout-ms", type=int, default=10000)
    wait_parser.add_argument("--wait-seconds", type=float, help="Simple wait in seconds")
    wait_parser.add_argument("--url", help="URL to navigate to first")
    _add_visual_common_args(wait_parser)

    # decide (generic action escape hatch)
    decide_parser = subparsers.add_parser("decide", help="[Visual] Generic action: click/scroll/hover/press/type/navigate")
    decide_parser.add_argument("--task-id", required=True)
    decide_parser.add_argument("--action", required=True,
                               choices=["click", "scroll", "hover", "press", "type", "navigate", "screenshot"])
    decide_parser.add_argument("--value", help="Action parameter (text to click, 'down:500' for scroll, key name, etc.)")
    decide_parser.add_argument("--url", help="URL for navigate action")
    _add_visual_common_args(decide_parser)

    return parser


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────


def main() -> int:
    global _JSON_OUT

    parser = _build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 1

    # Capture real stdout before redirecting sys.stdout → stderr.
    # browser_adapter writes diagnostics via print() which lands on sys.stdout;
    # we redirect that to stderr so only _output_json() writes to the real stdout.
    _JSON_OUT = sys.stdout
    sys.stdout = sys.stderr

    try:
        if args.command == "search":
            if getattr(args, "visual", False):
                _cmd_search_visual(args)
            else:
                _cmd_search(args)
        elif args.command == "resume":
            _cmd_resume(args)
        elif args.command == "check-session":
            _cmd_check_session(args)
        elif args.command == "clear-session":
            _cmd_clear_session(args)
        elif args.command == "open":
            _cmd_open(args)
        elif args.command == "sku-select":
            _cmd_sku_select(args)
        elif args.command == "cart-add":
            _cmd_cart_add(args)
        elif args.command == "cart-view":
            _cmd_cart_view(args)
        elif args.command == "dom":
            _cmd_dom(args)
        elif args.command == "wait":
            _cmd_wait(args)
        elif args.command == "decide":
            _cmd_decide(args)
        else:
            parser.print_help()
            return 1
    finally:
        sys.stdout = _JSON_OUT

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
