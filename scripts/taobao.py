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
from contextlib import suppress
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).parent.resolve()
_WORKFLOW_STATE_FILE = ".cache/taobao-search-skill/workflow-state.json"
_DEFAULT_SESSION_PATH = ".cache/taobao-search-skill/taobao-session.json"
_DEFAULT_ARTIFACT_DIR = ".cache/taobao-search-skill/artifacts"

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

    # search
    search_parser = subparsers.add_parser("search", help="Run search -> filter -> add-to-cart pipeline")
    _define_search_args(search_parser)

    # resume
    resume_parser = subparsers.add_parser("resume", help="Resume workflow after human intervention")
    resume_parser.add_argument("--session-state-path", default=_DEFAULT_SESSION_PATH)

    # check-session
    check_parser = subparsers.add_parser("check-session", help="Check session file status")
    check_parser.add_argument("--session-state-path", default=_DEFAULT_SESSION_PATH)

    # clear-session
    clear_parser = subparsers.add_parser("clear-session", help="Clear persisted session")
    clear_parser.add_argument("--session-state-path", default=_DEFAULT_SESSION_PATH)

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
            _cmd_search(args)
        elif args.command == "resume":
            _cmd_resume(args)
        elif args.command == "check-session":
            _cmd_check_session(args)
        elif args.command == "clear-session":
            _cmd_clear_session(args)
        else:
            parser.print_help()
            return 1
    finally:
        sys.stdout = _JSON_OUT

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
