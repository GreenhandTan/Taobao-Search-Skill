#!/usr/bin/env python3
"""Taobao browser automation — session mode.

Launches a persistent browser session that accepts JSON commands via stdin
and returns JSON results via stdout. The browser stays alive across commands,
eliminating repeated startup overhead.

Usage:
    python scripts/taobao.py session [--task-id ID] [--headless]
    python scripts/taobao.py check-session [--session-state-path PATH]
    python scripts/taobao.py clear-session [--session-state-path PATH]

Session protocol (stdin → stdout, one JSON per line):
    → {"cmd": "search", "keyword": "MacBook Air M4"}
    ← {"status": "success", "data": {"items": [...], "items_count": 8}, ...}
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).parent.resolve()
_DEFAULT_SESSION_PATH = ".cache/taobao-search-skill/taobao-session.json"
_DEFAULT_ARTIFACT_DIR = ".cache/taobao-search-skill/artifacts"

# Real stdout reference — set in main() before redirecting sys.stdout → stderr.
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
    """Write JSON to the real stdout (preserved before redirect)."""
    with suppress(BrokenPipeError):
        json.dump(data, _JSON_OUT, ensure_ascii=False, indent=2)
        _JSON_OUT.write("\n")
        _JSON_OUT.flush()


def _log(msg: str) -> None:
    """Write diagnostic messages to stderr so they never pollute JSON stdout."""
    print(f"[taobao] {msg}", file=sys.stderr)


# ──────────────────────────────────────────────
# Session Handler
# ──────────────────────────────────────────────


class SessionHandler:
    """Persistent browser session that processes JSON commands from stdin."""

    def __init__(
        self,
        task_id: str,
        session_state_path: str = _DEFAULT_SESSION_PATH,
        headless: bool = False,
    ) -> None:
        self.task_id = task_id
        self.session_state_path = session_state_path
        self.headless = headless

        from browser_adapter import BrowserAdapter
        from session_flow import SessionFlow
        from session_manager import SessionManager

        self.browser = BrowserAdapter(
            headless=headless,
            artifact_dir=_DEFAULT_ARTIFACT_DIR,
        )
        session_manager = SessionManager(session_state_path)
        self.session_flow = SessionFlow(self.browser, session_manager)

        # Session state (in-memory, no file serialization)
        self.items: list[dict[str, Any]] = []
        self.current_index: int | None = None
        self.sku_groups: list[dict[str, Any]] = []

    def run(self) -> None:
        """Main loop: read JSON commands from stdin, execute, write results."""
        _log(f"session starting (task_id={self.task_id})")

        self.browser.open()
        self.browser.navigate_to_taobao()
        self.session_flow.try_restore()

        if not self.browser.is_logged_in():
            _log("login required — waiting for user")
            self.browser.ensure_login(manual_approval_required=True, force_manual=True)
            if self.browser.is_logged_in():
                self.session_flow.capture_after_login()
                _log("login successful, session captured")
            else:
                _log("login failed or timed out")

        _log("session ready, waiting for commands")

        try:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    request = json.loads(line)
                except json.JSONDecodeError as exc:
                    self._error(f"Invalid JSON: {exc}")
                    continue

                cmd = request.get("cmd", "")
                handler = self._HANDLERS.get(cmd)
                if handler is None:
                    self._error(f"Unknown command: {cmd}")
                    continue

                try:
                    handler(self, request)
                except Exception as exc:
                    self._error(str(exc))

        finally:
            self.cleanup()

    # ── Response helpers ──

    def _respond(self, **kwargs: Any) -> None:
        data = {
            "status": "success",
            "task_id": self.task_id,
            "screenshot": kwargs.get("screenshot"),
            "page_text_summary": (kwargs.get("page_text") or "")[:500] or None,
            "data": kwargs.get("data", {}),
        }
        _output_json(data)

    def _error(self, message: str, **kwargs: Any) -> None:
        data = {
            "status": "error",
            "task_id": self.task_id,
            "error": {"message": message, **kwargs},
        }
        _output_json(data)

    def _screenshot(self, name: str) -> str:
        return self.browser.capture_viewport_screenshot(f"{name}_{self.task_id}")

    def _page(self):
        return self.browser._ensure_page()

    # ── Command handlers ──

    def _cmd_search(self, req: dict[str, Any]) -> None:
        keyword = req.get("keyword", "")
        if not keyword:
            self._error("Missing 'keyword'")
            return

        page = self._page()

        self.browser.search(keyword)
        self.browser.wait_for_results()
        screenshot = self._screenshot("search")
        page_text = self.browser.get_page_text(2000)

        candidates = self.browser.collect_candidates(
            keyword,
            max_candidates=req.get("max_candidates", 20),
            price_min=req.get("price_min"),
            price_max=req.get("price_max"),
            min_sales=req.get("min_sales"),
            require_free_shipping=req.get("require_free_shipping", False),
            require_tmall=req.get("require_tmall"),
        )

        self.items = [
            {
                "index": idx,
                "title": item.title,
                "url": item.url,
                "price": item.price,
                "price_value": item.price_value,
                "sales_count": item.sales_count,
                "rating": item.rating,
                "is_tmall": item.is_tmall,
                "free_shipping": item.free_shipping,
            }
            for idx, item in enumerate(candidates)
        ]

        self._respond(
            screenshot=screenshot,
            page_text=page_text,
            data={
                "keyword": keyword,
                "items": self.items,
                "items_count": len(self.items),
            },
        )

    def _cmd_open(self, req: dict[str, Any]) -> None:
        if not self.items:
            self._error("No search results. Run 'search' first.")
            return

        index = req.get("index", 0)
        if index < 0 or index >= len(self.items):
            self._error(f"Index {index} out of range (0-{len(self.items)-1})")
            return

        item = self.items[index]
        url = item.get("url", "")
        if not url:
            self._error("Item has no URL")
            return

        page = self._page()
        _log(f"opening item[{index}]: {item.get('title', '')[:40]}")
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        with suppress(Exception):
            page.wait_for_load_state("networkidle", timeout=10000)
        self.browser._handle_captcha_if_present(page)
        self.browser._simulate_browsing(page, max_scroll=800)
        self.browser._human_wait(0.5, 1.5)

        screenshot = self._screenshot(f"item_{index}")
        page_text = self.browser.get_page_text(3000)
        sku_groups = self.browser.get_sku_structure()

        detail_price = self.browser._extract_detail_price(page)
        if detail_price is not None:
            item["price"] = f"¥{detail_price:.2f}"
            item["price_value"] = detail_price

        self.current_index = index
        self.sku_groups = sku_groups

        self._respond(
            screenshot=screenshot,
            page_text=page_text,
            data={
                "item": item,
                "sku_groups": sku_groups,
                "detail_price": detail_price,
            },
        )

    def _cmd_select_sku(self, req: dict[str, Any]) -> None:
        if self.current_index is None:
            self._error("No item open. Run 'open' first.")
            return

        # Normalize: single {label,value} or array [{label,value}]
        raw = req.get("selections", req)
        if isinstance(raw, dict):
            selections = [raw]
        elif isinstance(raw, list):
            selections = raw
        else:
            self._error("'selections' must be {label,value} or [{label,value}]")
            return

        item = self.items[self.current_index]
        url = item.get("url", "")
        if url:
            page = self._page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            with suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=8000)
            self.browser._human_wait(0.5, 1.5)

        all_ok, results = self.browser.select_skus(selections)
        self.browser._human_wait(1.0, 2.0)

        screenshot = self._screenshot(f"sku_{self.current_index}")
        page_text = self.browser.get_page_text(2000)

        page = self._page()
        final_price = self.browser._extract_detail_price(page)
        if final_price is not None:
            item["price"] = f"¥{final_price:.2f}"
            item["price_value"] = final_price

        self.sku_groups = self.browser.get_sku_structure()

        self._respond(
            screenshot=screenshot,
            page_text=page_text,
            data={
                "all_selected": all_ok,
                "selections": results,
                "final_price": final_price,
                "sku_groups": self.sku_groups,
            },
        )

    def _cmd_cart_add(self, req: dict[str, Any]) -> None:
        from taobao_selectors import ADD_TO_CART_BUTTONS, CART_CONFIRM_POPUP

        if self.current_index is None:
            self._error("No item open. Run 'open' first.")
            return

        item = self.items[self.current_index]
        url = item.get("url", "")
        if url:
            page = self._page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            with suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=8000)
            self.browser._handle_captcha_if_present(page)
            self.browser._human_wait(1.0, 2.0)

        page = self._page()
        button = self.browser._find_first_visible_locator(page, ADD_TO_CART_BUTTONS)
        if button is None:
            screenshot = self._screenshot("cart_fail")
            self._error(
                "Add-to-cart button not found",
                screenshot=screenshot,
            )
            return

        self.browser._human_click(page, button)
        self.browser._human_wait(1.5, 3.0)

        confirmed = None
        for retry in range(5):
            confirmed = self.browser._find_first_visible_locator(page, CART_CONFIRM_POPUP)
            if confirmed:
                break
            self.browser._human_wait(0.5, 1.0)
            if retry == 2:
                self.browser._human_click(page, button)
                self.browser._human_wait(1.0, 1.5)

        screenshot = self._screenshot("cart_add")
        page_text = self.browser.get_page_text(1000)

        self._respond(
            screenshot=screenshot,
            page_text=page_text,
            data={
                "cart_added": confirmed is not None,
                "confirmed": confirmed is not None,
                "item_index": self.current_index,
            },
        )

    def _cmd_cart_view(self, req: dict[str, Any]) -> None:
        from taobao_selectors import CART_ITEM_SELECTORS

        page = self._page()
        page.goto(
            "https://cart.taobao.com/cart.htm",
            wait_until="domcontentloaded",
            timeout=20000,
        )
        with suppress(Exception):
            page.wait_for_load_state("networkidle", timeout=10000)
        self.browser._handle_captcha_if_present(page)
        self.browser._human_wait(2.0, 4.0)

        screenshot = self._screenshot("cart")
        page_text = self.browser.get_page_text(2000)

        item_count = 0
        for sel in CART_ITEM_SELECTORS:
            with suppress(Exception):
                item_count = page.locator(sel).count()
                if item_count > 0:
                    break

        self._respond(
            screenshot=screenshot,
            page_text=page_text,
            data={
                "cart_item_count": item_count,
                "items": self.items,
            },
        )

    def _cmd_dom(self, req: dict[str, Any]) -> None:
        url = req.get("url")
        if url:
            page = self._page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            with suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=8000)
            self.browser._human_wait(1.0, 2.0)
        elif self.current_index is not None:
            item_url = self.items[self.current_index].get("url", "")
            if item_url:
                page = self._page()
                page.goto(item_url, wait_until="domcontentloaded", timeout=30000)
                with suppress(Exception):
                    page.wait_for_load_state("networkidle", timeout=8000)
                self.browser._human_wait(1.0, 2.0)

        dom_data = self.browser.get_visible_dom()
        screenshot = self._screenshot("dom")

        self._respond(
            screenshot=screenshot,
            data={"dom": dom_data},
        )

    def _cmd_screenshot(self, req: dict[str, Any]) -> None:
        screenshot = self._screenshot("manual")
        page_text = self.browser.get_page_text(2000)
        self._respond(screenshot=screenshot, page_text=page_text, data={})

    def _cmd_quit(self, req: dict[str, Any]) -> None:
        _log("quit received, closing session")
        self.cleanup()
        sys.exit(0)

    # Command dispatch table
    _HANDLERS = {
        "search": _cmd_search,
        "open": _cmd_open,
        "select-sku": _cmd_select_sku,
        "cart-add": _cmd_cart_add,
        "cart-view": _cmd_cart_view,
        "dom": _cmd_dom,
        "screenshot": _cmd_screenshot,
        "quit": _cmd_quit,
    }

    def cleanup(self) -> None:
        with suppress(Exception):
            self.browser.close()
        _log("session closed")


# ──────────────────────────────────────────────
# CLI commands: check-session, clear-session
# ──────────────────────────────────────────────


def _cmd_check_session(args: argparse.Namespace) -> None:
    session_path = _resolve_path(args.session_state_path or _DEFAULT_SESSION_PATH)

    if not session_path.exists():
        _output_json({
            "status": "success",
            "session_exists": False,
            "session_path": str(session_path),
            "message": "Session file does not exist. First use requires manual login.",
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
            "message": f"Session file exists with {len(cookies)} cookies.",
        })
    except Exception as exc:
        _output_json({
            "status": "error",
            "session_exists": True,
            "session_path": str(session_path),
            "error": {"code": "SESSION_READ_ERROR", "message": str(exc)},
        })


def _cmd_clear_session(args: argparse.Namespace) -> None:
    session_path = _resolve_path(args.session_state_path or _DEFAULT_SESSION_PATH)
    removed = False
    if session_path.exists():
        session_path.unlink()
        removed = True
    _output_json({
        "status": "success",
        "session_removed": removed,
        "session_path": str(session_path),
        "message": "Session cleared." if removed else "No session file to clear.",
    })


# ──────────────────────────────────────────────
# CLI Argument Parser
# ──────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Taobao browser automation — persistent session mode."
    )
    subparsers = parser.add_subparsers(dest="command", help="Subcommand")

    # session
    session_parser = subparsers.add_parser(
        "session",
        help="Start a persistent browser session (reads JSON commands from stdin)",
    )
    session_parser.add_argument(
        "--task-id",
        default=f"taobao-{uuid.uuid4().hex[:8]}",
        help="Task identifier (auto-generated if not provided)",
    )
    session_parser.add_argument(
        "--headless", action="store_true", help="Run browser in headless mode"
    )
    session_parser.add_argument(
        "--session-state-path",
        default=_DEFAULT_SESSION_PATH,
        help="Session file path",
    )

    # check-session
    check_parser = subparsers.add_parser(
        "check-session", help="Check session file status"
    )
    check_parser.add_argument(
        "--session-state-path", default=_DEFAULT_SESSION_PATH
    )

    # clear-session
    clear_parser = subparsers.add_parser(
        "clear-session", help="Clear persisted session"
    )
    clear_parser.add_argument(
        "--session-state-path", default=_DEFAULT_SESSION_PATH
    )

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
    # This ensures only _output_json() writes to the real stdout.
    _JSON_OUT = sys.stdout
    sys.stdout = sys.stderr

    try:
        if args.command == "session":
            handler = SessionHandler(
                task_id=args.task_id,
                session_state_path=args.session_state_path,
                headless=args.headless,
            )
            handler.run()
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
