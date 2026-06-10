from __future__ import annotations

import json
import math
import random
import re
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, quote, urljoin, urlparse

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright
from playwright_stealth import Stealth

from models import MatchedItem
from taobao_selectors import (
    ACCESS_BLOCKED_SIGNALS, ADD_TO_CART_BUTTONS, CAPTCHA_BG_SELECTORS,
    CAPTCHA_PANEL_SELECTORS, CAPTCHA_SLIDER_BTN_SELECTORS,
    CART_CONFIRM_POPUP, CART_ITEM_SELECTORS, LOGGED_IN_INDICATORS,
    LOGIN_COOKIE_NAMES, LOGIN_PAGE_URL_SIGNALS, MIDDLEWARE_OVERLAY_HIDE_JS,
    NOT_LOGGED_IN_TEXT, POPUP_CLOSE_BUTTONS, PRICE_SELECTORS,
    PRODUCT_CARD_CLIMB_SELECTORS, PRODUCT_LINK_SELECTORS, RATING_SELECTORS,
    SALES_COUNT_SELECTORS, SEARCH_INPUT, SEARCH_SUBMIT,
    SKU_GROUP_LABEL_SELECTORS, SKU_OPTION_SELECTORS, SKU_VALUE_SELECTOR,
)
from session_manager import SessionSnapshot
from slider_solver import SliderSolver

_SCRIPTS_DIR = Path(__file__).parent.resolve()


class BrowserAdapter:
    def __init__(self, browser_name: str = "chromium", headless: bool = False, artifact_dir: str = ".cache/taobao-search-skill/artifacts") -> None:
        self.browser_name = browser_name
        self.headless = headless
        artifact_path = Path(artifact_dir)
        if not artifact_path.is_absolute():
            artifact_path = _SCRIPTS_DIR / artifact_path
        self.artifact_dir = artifact_path
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._slider_solver = SliderSolver(method="ddddocr")

    # ──────────────────────────────────────────────
    # Browser Lifecycle
    # ──────────────────────────────────────────────

    def open(self) -> None:
        if self._browser is not None:
            return

        self.artifact_dir.mkdir(parents=True, exist_ok=True)

        # Wrap Playwright with stealth — injects 20 anti-detection evasion patches
        self._playwright = Stealth(
            navigator_languages_override=("zh-CN", "zh"),
            navigator_platform_override="Win32",
            navigator_vendor_override="Google Inc.",
            webgl_vendor_override="Intel Inc.",
            webgl_renderer_override="Intel Iris OpenGL Engine",
        ).use_sync(sync_playwright()).start()

        browser_type = getattr(self._playwright, self.browser_name, None)
        if browser_type is None:
            raise ValueError(f"Unsupported browser type: {self.browser_name}")

        # Anti-detection launch args
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-infobars",
            "--no-first-run",
            "--window-size=1920,1080",
        ]

        launch_errors: list[str] = []
        launch_attempts: list[dict[str, Any]] = [
            {"headless": self.headless, "args": launch_args},
            {"headless": self.headless, "channel": "msedge", "args": launch_args},
            {"headless": self.headless, "channel": "chrome", "args": launch_args},
        ]

        for launch_kwargs in launch_attempts:
            try:
                self._browser = browser_type.launch(**launch_kwargs)
                print(f"[browser] open {self.browser_name} browser session with stealth", file=sys.stderr)
                return
            except Exception as exc:
                launch_errors.append(f"{launch_kwargs}: {exc}")

        raise RuntimeError("Unable to launch a browser backend. Attempts: " + " | ".join(launch_errors))

    def _create_context(self, storage_state: dict[str, Any] | None = None) -> None:
        if self._browser is None:
            raise RuntimeError("Browser is not opened")

        if self._context is not None:
            with suppress(Exception):
                self._context.close()

        # Randomize viewport slightly to avoid fingerprint consistency
        w = random.randint(1900, 1940)
        h = random.randint(1060, 1100)

        # Use realistic, recent Chrome user agent
        chrome_ver = random.choice(["131.0.0.0", "130.0.0.0", "129.0.0.0", "128.0.0.0"])

        context_kwargs: dict[str, Any] = {
            "viewport": {"width": w, "height": h},
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "ignore_https_errors": True,
            "user_agent": (
                f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_ver} Safari/537.36"
            ),
        }
        if storage_state is not None:
            context_kwargs["storage_state"] = storage_state

        self._context = self._browser.new_context(**context_kwargs)
        self._context.set_default_timeout(15000)
        self._page = self._context.new_page()

    def _ensure_page(self) -> Page:
        if self._page is None:
            self._create_context()
        assert self._page is not None
        return self._page

    def close(self) -> None:
        with suppress(Exception):
            if self._context is not None:
                self._context.close()
        with suppress(Exception):
            if self._browser is not None:
                self._browser.close()
        with suppress(Exception):
            if self._playwright is not None:
                self._playwright.stop()
        self._context = None
        self._browser = None
        self._page = None
        self._playwright = None

    # ──────────────────────────────────────────────
    # Session Management
    # ──────────────────────────────────────────────

    def restore_session(self, snapshot: SessionSnapshot) -> bool:
        self.open()
        self._create_context(storage_state=snapshot.storage_state)
        print("[browser] restore session from persisted state", file=sys.stderr)
        return True

    def capture_session(self) -> SessionSnapshot:
        context = self._context
        if context is None:
            raise RuntimeError("Browser context is not initialized")
        print("[browser] capture current browser session", file=sys.stderr)
        return SessionSnapshot(storage_state=context.storage_state())

    # ──────────────────────────────────────────────
    # Login
    # ──────────────────────────────────────────────

    def navigate_to_taobao(self) -> None:
        page = self._ensure_page()
        page.goto("https://www.taobao.com", wait_until="domcontentloaded", timeout=30000)
        with suppress(Exception):
            page.wait_for_load_state("networkidle", timeout=5000)
        # Auto-solve CAPTCHA if triggered on navigation
        self._handle_captcha_if_present(page)

    def is_logged_in(self) -> bool:
        page = self._ensure_page()
        if self._looks_logged_in(page):
            print("[browser] check login status => logged in", file=sys.stderr)
            return True
        print("[browser] check login status => not logged in", file=sys.stderr)
        return False

    def ensure_login(self, manual_approval_required: bool, force_manual: bool = False) -> str:
        page = self._ensure_page()
        if not force_manual and self._looks_logged_in(page):
            return "success"
        if manual_approval_required:
            print("[browser] waiting for human takeover or approved login flow", file=sys.stderr)
            self._wait_for_user_login(page)
            return "success" if self._looks_logged_in(page) else "waiting_manual"
        return "success"

    def _looks_logged_in(self, page: Page) -> bool:
        url = page.url.lower()
        if any(signal in url for signal in LOGIN_PAGE_URL_SIGNALS):
            return False
        with suppress(Exception):
            for sel in NOT_LOGGED_IN_TEXT:
                if page.locator(sel).first.is_visible():
                    return False
        for selector in LOGGED_IN_INDICATORS:
            with suppress(Exception):
                if page.locator(selector).first.is_visible(timeout=2000):
                    return True
        with suppress(Exception):
            cookies = page.context.cookies()
            for cookie in cookies:
                if cookie.get("name") in LOGIN_COOKIE_NAMES and cookie.get("value"):
                    if "login" not in url:
                        return True
        return False

    def _wait_for_user_login(self, page: Page) -> None:
        print("[browser] ============================================", file=sys.stderr)
        print("[browser] 请在弹出的浏览器窗口中手动完成淘宝登录", file=sys.stderr)
        print("[browser] 登录完成后脚本会自动检测并继续", file=sys.stderr)
        print("[browser] ============================================", file=sys.stderr)

        with suppress(Exception):
            login_link = page.locator(NOT_LOGGED_IN_TEXT[0]).first
            if login_link.is_visible():
                self._human_click(page, login_link)
                page.wait_for_timeout(2000)

        check_interval = 30
        max_checks = 7  # up to ~3.5 min
        total_slept = 0

        # Immediate first check — user might already be logged in from a popup
        if self._looks_logged_in(page):
            print("[browser] 登录成功! (立即检测到)", file=sys.stderr)
            return

        for i in range(max_checks):
            time.sleep(check_interval)
            total_slept += check_interval
            print(f"[browser] 等待登录中... 第 {i+1}/{max_checks} 次检测 (已等待 {total_slept}s/{max_checks * check_interval}s)", file=sys.stderr)

            # Check current page without navigating — don't interrupt user's login flow
            if self._looks_logged_in(page):
                print(f"[browser] 登录成功! (等待约 {total_slept}s)", file=sys.stderr)
                return

            # If we're still on a login domain, user hasn't finished — keep waiting
            url = page.url.lower()
            if "login.taobao.com" in url or "login.tmall.com" in url or "login" in url:
                continue

            # On a non-login page but not detected: gently refresh to sync state
            with suppress(Exception):
                page.reload(wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(1500)
            if self._looks_logged_in(page):
                print(f"[browser] 登录成功! (等待约 {total_slept}s, 刷新后检测到)", file=sys.stderr)
                return

        print("[browser] 登录超时，请重试", file=sys.stderr)

    # ──────────────────────────────────────────────
    # Search
    # ──────────────────────────────────────────────

    def search(self, keyword: str) -> str:
        page = self._ensure_page()

        # Step 1: Ensure we are on the taobao homepage
        if "taobao.com" not in page.url or "s.taobao.com" in page.url:
            print("[browser] navigating to taobao homepage before search", file=sys.stderr)
            page.goto("https://www.taobao.com", wait_until="domcontentloaded", timeout=30000)
            self._human_wait(2, 4)

        # Step 2: Dismiss any popups/overlays
        for _ in range(3):
            dismissed = False
            for sel in POPUP_CLOSE_BUTTONS:
                with suppress(Exception):
                    btn = self._find_first_visible_locator(page, [sel])
                    if btn is not None:
                        self._human_click(page, btn)
                        self._human_wait(0.3, 0.8)
                        dismissed = True
            if not dismissed:
                break

        # Step 3: Hide overlay divs that intercept clicks
        with suppress(Exception):
            page.evaluate(MIDDLEWARE_OVERLAY_HIDE_JS)

        self._human_wait(0.5, 1.5)
        self._random_mouse_move(page)

        # Step 4: Locate search input and interact human-like
        search_input = self._find_first_visible_locator(page, SEARCH_INPUT)
        if search_input is None:
            raise RuntimeError("Unable to locate Taobao search input")

        # Human-like: move mouse to input, click, then type
        self._human_click(page, search_input)
        self._human_wait(0.3, 0.8)

        # Clear existing text
        with suppress(Exception):
            page.keyboard.press("Control+a")
            self._human_wait(0.1, 0.3)

        # Type keyword with human-like delays
        self._human_type(page, keyword)
        self._human_wait(0.5, 1.5)

        print(f"[browser] search keyword: {keyword}", file=sys.stderr)

        if random.random() < 0.5:
            self._random_mouse_move(page)

        # Dismiss search suggestion dropdown that may intercept Enter
        with suppress(Exception):
            page.keyboard.press("Escape")
            self._human_wait(0.3, 0.6)

        # Step 5: Re-focus input then submit
        with suppress(Exception):
            search_input.focus()
            self._human_wait(0.1, 0.3)

        try:
            with page.expect_navigation(wait_until="domcontentloaded", timeout=12000):
                page.keyboard.press("Enter")
        except Exception:
            pass

        # If Enter didn't navigate, try programmatic form submit
        if "s.taobao.com/search" not in page.url:
            print("[browser] Enter key did not navigate, trying form submit", file=sys.stderr)
            with suppress(Exception):
                page.evaluate("""() => {
                    const form = document.querySelector('#J_TSearchForm')
                        || document.querySelector('form[action*="search"]')
                        || document.querySelector('form[class*="search"]');
                    if (form) { form.submit(); return true; }
                    return false;
                }""")
            try:
                page.wait_for_url("**/search**", timeout=10000)
            except Exception:
                pass

        # If form submit didn't work, try clicking search button
        if "s.taobao.com/search" not in page.url:
            print("[browser] form submit did not navigate, trying search button", file=sys.stderr)
            for btn_sel in SEARCH_SUBMIT:
                with suppress(Exception):
                    btn = self._find_first_visible_locator(page, [btn_sel])
                    if btn is not None:
                        self._human_click(page, btn)
                        self._human_wait(1, 2)
                        break
            try:
                page.wait_for_url("**/search**", timeout=10000)
            except Exception:
                pass

        # Last resort: direct URL navigation
        if "s.taobao.com/search" not in page.url:
            print("[browser] all interactive methods failed, using direct URL", file=sys.stderr)
            search_url = f"https://s.taobao.com/search?q={quote(keyword)}"
            page.goto(search_url, wait_until="domcontentloaded", timeout=30000)

        # Auto-solve CAPTCHA if triggered by search navigation
        self._handle_captcha_if_present(page)

        # Step 6: Wait for results to load
        with suppress(Exception):
            page.wait_for_load_state("networkidle", timeout=10000)
        self._human_wait(2, 4)

        # Human-like scroll to trigger lazy loading
        self._human_scroll(page, target_y=random.randint(800, 1200))

        print(f"[browser] current url: {page.url}", file=sys.stderr)
        return "success"

    def wait_for_results(self) -> str:
        page = self._ensure_page()
        with suppress(Exception):
            page.wait_for_load_state("domcontentloaded", timeout=10000)
        with suppress(Exception):
            page.wait_for_load_state("networkidle", timeout=10000)
        if not self._find_candidate_links(page):
            print("[browser] results page ready but no candidates found yet", file=sys.stderr)
        else:
            print("[browser] wait for results page ready", file=sys.stderr)
        return "success"

    def ensure_search_access(self, manual_approval_required: bool) -> bool:
        page = self._ensure_page()
        if not self._looks_access_blocked(page):
            return True
        print("[browser] access blocked by Taobao risk control", file=sys.stderr)

        # Try auto-solving CAPTCHA first
        if self._handle_captcha_if_present(page):
            print("[browser] CAPTCHA auto-solved, access restored", file=sys.stderr)
            return True

        if not manual_approval_required:
            return False
        print("[browser] waiting for user to pass manual verification", file=sys.stderr)
        self._wait_for_access_recovery(page)
        return not self._looks_access_blocked(page)

    # ──────────────────────────────────────────────
    # Candidate Collection & Cart
    # ──────────────────────────────────────────────

    def collect_candidates(self, keyword: str, max_candidates: int,
                           price_min: float | None = None, price_max: float | None = None,
                           min_sales: int | None = None, require_free_shipping: bool = False,
                           require_tmall: bool | None = None) -> list[MatchedItem]:
        page = self._ensure_page()
        links = self._find_candidate_links(page)
        candidates: list[MatchedItem] = []
        seen_urls: set[str] = set()
        keyword_tokens = self._build_keyword_tokens(keyword)
        skipped_price = 0
        skipped_sales = 0
        skipped_shipping = 0
        skipped_tmall = 0

        if not links:
            anchor_count = page.locator("a").count()
            item_count = page.locator(PRODUCT_LINK_SELECTORS[0]).count()
            tmall_count = page.locator(PRODUCT_LINK_SELECTORS[1]).count()
            print(f"[browser] candidate diagnostics => url={page.url}, anchors={anchor_count}, item_links={item_count}, tmall_links={tmall_count}", file=sys.stderr)
            with suppress(Exception):
                samples = page.evaluate(
                    """() => Array.from(document.querySelectorAll('a'))
                        .slice(0, 12)
                        .map((a) => ({ href: a.getAttribute('href') || '', text: (a.textContent || '').trim().slice(0, 40) }))"""
                )
                print(f"[browser] anchor samples => {samples}", file=sys.stderr)

        for link in links:
            if len(candidates) >= max_candidates:
                break
            href = link.get("href")
            title = (link.get("title") or link.get("text") or "").strip()
            if not href or href in seen_urls or not title:
                continue
            text = link.get("text", "")
            card_text = link.get("card_text", text)
            combined_text = f"{title}\n{text}\n{card_text}"
            if keyword_tokens and not self._matches_keyword(title, text, keyword_tokens):
                continue
            absolute_href = urljoin(page.url, href)
            seen_urls.add(absolute_href)

            rating = self._extract_rating(combined_text)
            price_str, price_val = self._extract_price(card_text)
            sales = self._extract_sales_count(card_text)
            free_shipping = self._check_free_shipping(card_text)
            is_tmall = self._check_is_tmall(absolute_href, card_text)

            # Apply filters
            if require_tmall is True and not is_tmall:
                skipped_tmall += 1
                continue
            if require_tmall is False and is_tmall:
                skipped_tmall += 1
                continue
            if price_min is not None and price_val is not None and price_val < price_min:
                skipped_price += 1
                continue
            if price_max is not None and price_val is not None and price_val > price_max:
                skipped_price += 1
                continue
            if require_free_shipping and not free_shipping:
                skipped_shipping += 1
                continue
            if min_sales is not None and (sales is None or sales < min_sales):
                skipped_sales += 1
                continue

            item = MatchedItem(
                title=title, url=absolute_href, rating=rating,
                price=price_str, price_value=price_val,
                sales_count=sales, free_shipping=free_shipping, is_tmall=is_tmall,
            )
            candidates.append(item)
            if len(candidates) % 3 == 0:
                self._human_wait(0.3, 1)

        filter_msgs = []
        if skipped_price: filter_msgs.append(f"price={skipped_price}")
        if skipped_sales: filter_msgs.append(f"sales={skipped_sales}")
        if skipped_shipping: filter_msgs.append(f"shipping={skipped_shipping}")
        if skipped_tmall: filter_msgs.append(f"tmall={skipped_tmall}")
        filter_info = f", skipped({', '.join(filter_msgs)})" if filter_msgs else ""

        print(f"[browser] collect up to {max_candidates} candidates, found={len(candidates)}{filter_info}", file=sys.stderr)
        return candidates

    def _extract_detail_price(self, page: Page) -> float | None:
        price_text = page.evaluate("""(selectors) => {
            for (const sel of selectors) {
                const el = document.querySelector(sel);
                if (el && el.textContent.trim()) return el.textContent.trim();
            }
            return null;
        }""", PRICE_SELECTORS)
        if not price_text:
            return None
        match = re.search(r'([\d,]+(?:\.\d{2})?)', price_text.replace(',', ''))
        if not match:
            return None
        try:
            val = float(match.group(1))
            return val if val >= 0.01 else None
        except ValueError:
            return None

    def enrich_item_rating(self, item: MatchedItem) -> float | None:
        page = self._ensure_page()
        if not item.url:
            return None
        try:
            self._human_wait(0.5, 1.5)
            self._random_mouse_move(page)
            self._human_wait(0.2, 0.5)

            page.goto(item.url, wait_until="domcontentloaded", timeout=20000)
            with suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=8000)
            self._handle_captcha_if_present(page)

            self._simulate_browsing(page, max_scroll=600)

            # Extract item_id from URL query param
            if not item.item_id:
                parsed = urlparse(item.url)
                params = parse_qs(parsed.query)
                id_vals = params.get("id", [])
                if id_vals:
                    item.item_id = id_vals[0]

            # Extract price from detail page
            if not item.price or item.price_value is None:
                detail_price = self._extract_detail_price(page)
                if detail_price is not None:
                    item.price = f"¥{detail_price:.2f}"
                    item.price_value = detail_price

            # Extract sales count from detail page if not found on search page
            if item.sales_count is None:
                sales_text = page.evaluate("""(selectors) => {
                    let text = '';
                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (el) text += ' ' + (el.textContent || '');
                    }
                    return text || document.body.innerText;
                }""", SALES_COUNT_SELECTORS)
                item.sales_count = self._extract_sales_count(sales_text)

            text = page.evaluate("""(selectors) => {
                const body = document.body.innerText;
                const ratingEls = document.querySelectorAll(selectors.join(','));
                let extra = '';
                ratingEls.forEach(el => { extra += ' ' + el.textContent; });
                return body + extra;
            }""", RATING_SELECTORS)

            rating = self._extract_rating(text)
            if rating is not None:
                item.rating = rating
                print(f"[browser] enriched rating for [{item.title[:30]}]: {rating*100:.0f}%", file=sys.stderr)
            else:
                print(f"[browser] no rating found for [{item.title[:30]}]", file=sys.stderr)
            return rating
        except Exception as e:
            print(f"[browser] enrich rating failed for [{item.title[:30]}]: {e}", file=sys.stderr)
            return None

    def add_to_cart(self, item: MatchedItem, sku_keywords: str | None = None,
                    price_min: float | None = None, price_max: float | None = None) -> bool | None:
        """Add item to cart. Returns True on success, False on SKU/price mismatch, None on error."""
        page = self._ensure_page()
        if item.url:
            self._human_wait(2, 5)
            self._random_mouse_move(page)
            self._human_wait(0.3, 0.8)

            page.goto(item.url, wait_until="domcontentloaded", timeout=30000)
            with suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=10000)
            self._handle_captcha_if_present(page)

            self._simulate_browsing(page, max_scroll=1000)

        sku_ok = self._select_default_sku(page, sku_keywords=sku_keywords)
        if sku_ok is False:
            print(f"[browser] SKU mismatch for [{item.title[:30]}]: no option matching '{sku_keywords}'", file=sys.stderr)
            return False

        # Verify actual SKU price against constraints
        if price_min is not None or price_max is not None:
            detail_price = self._extract_detail_price(page)
            if detail_price is not None:
                item.price = f"¥{detail_price:.2f}"
                item.price_value = detail_price
                if price_min is not None and detail_price < price_min:
                    print(f"[browser] SKU price {detail_price:.2f} < min {price_min:.2f} for [{item.title[:30]}]", file=sys.stderr)
                    return False
                if price_max is not None and detail_price > price_max:
                    print(f"[browser] SKU price {detail_price:.2f} > max {price_max:.2f} for [{item.title[:30]}]", file=sys.stderr)
                    return False

        button = self._find_first_visible_locator(page, ADD_TO_CART_BUTTONS)
        if button is None:
            print(f"[browser] add item to cart failed: {item.title}", file=sys.stderr)
            return False

        self._human_click(page, button)
        self._human_wait(1, 2.5)
        item.cart_added = True
        print(f"[browser] add item to cart: {item.title}", file=sys.stderr)
        return True

    def _select_default_sku(self, page: Page, sku_keywords: str | None = None) -> bool | None:
        """Select SKU value items. Returns True/None/False."""
        # Wait for individual SKU value elements to appear
        has_sku = False
        with suppress(Exception):
            page.wait_for_selector(SKU_VALUE_SELECTOR, timeout=8000)
            has_sku = True
        if not has_sku:
            with suppress(Exception):
                page.wait_for_selector('[class*="skuItem"]', timeout=5000)
                has_sku = True
        if not has_sku:
            return None

        tokens = None
        if sku_keywords:
            tokens = [t.strip().upper() for t in sku_keywords.split() if t.strip()]
            print(f"[browser] SKU keywords: {tokens}", file=sys.stderr)

        # Find individual value elements (not group wrappers)
        value_items = page.locator(SKU_VALUE_SELECTOR).all()
        if not value_items:
            for sel in SKU_OPTION_SELECTORS:
                value_items = page.locator(sel).all()
                if value_items:
                    break

        if not value_items:
            return None

        # Build list of (text, element) for visible value items
        visible_items: list[tuple[str, any]] = []
        for item in value_items:
            with suppress(Exception):
                if item.is_visible(timeout=800):
                    text = (item.inner_text() or '').strip()
                    if text and len(text) < 40:  # skip multi-line group wrappers
                        visible_items.append((text, item))

        if not visible_items:
            return None

        if tokens:
            matched = 0
            for token in tokens:
                for i, (text, el) in enumerate(visible_items):
                    if token in text.upper():
                        if self._is_sku_selected(el):
                            matched += 1
                            print(f"[browser] SKU already selected '{token}' -> '{text}'", file=sys.stderr)
                            break
                        with suppress(Exception):
                            self._human_click(page, el)
                            self._human_wait(0.3, 0.6)
                            matched += 1
                            print(f"[browser] SKU matched '{token}' -> '{text}'", file=sys.stderr)
                            break
            if matched == 0:
                print(f"[browser] SKU match failed: none of {tokens} found in {[t for t,_ in visible_items]}", file=sys.stderr)
                return False
            print(f"[browser] SKU matched {matched}/{len(tokens)} keywords", file=sys.stderr)
        else:
            el = visible_items[0][1]
            if not self._is_sku_selected(el):
                with suppress(Exception):
                    self._human_click(page, el)
                    self._human_wait(0.3, 0.6)
            print(f"[browser] SKU default selected: '{visible_items[0][0]}'", file=sys.stderr)

        return True

    @staticmethod
    def _is_sku_selected(locator) -> bool:
        """Check if a SKU value item is already selected (has isSelected class)."""
        with suppress(Exception):
            cls = locator.get_attribute("class") or ""
            return "isSelected" in cls or "selected" in cls.lower() or "active" in cls.lower()
        return False

    def confirm_cart_state(self) -> str:
        page = self._ensure_page()
        try:
            page.goto("https://cart.taobao.com/cart.htm", wait_until="domcontentloaded", timeout=20000)
            with suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=10000)
            self._handle_captcha_if_present(page)
            self._human_wait(1.5, 3)

            for sel in CART_ITEM_SELECTORS:
                with suppress(Exception):
                    locator = page.locator(sel).first
                    if locator.is_visible(timeout=3000):
                        count = page.locator(sel).count()
                        print(f"[browser] confirm cart state => success ({count} items)", file=sys.stderr)
                        return "success"

            if "cart.taobao.com" in page.url or "cart.tmall.com" in page.url:
                print("[browser] confirm cart state => empty (on cart page but no items)", file=sys.stderr)
                return "empty"
            else:
                print("[browser] confirm cart state => error (did not reach cart page)", file=sys.stderr)
                return "error"
        except Exception as e:
            print(f"[browser] confirm cart state => error: {e}", file=sys.stderr)
            return "error"

    def capture_evidence(self, name: str) -> str:
        page = self._ensure_page()
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        path = self.artifact_dir / f"{name}.png"
        page.screenshot(path=str(path), full_page=True)
        print(f"[browser] capture evidence: {path}", file=sys.stderr)
        return str(path)

    # ──────────────────────────────────────────────
    # Visual Agent: Perception (Screenshot + DOM)
    # ──────────────────────────────────────────────

    def capture_viewport_screenshot(self, name: str) -> str:
        page = self._ensure_page()
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        path = self.artifact_dir / f"{name}.png"
        page.screenshot(path=str(path), full_page=False)
        print(f"[browser] capture viewport screenshot: {path}", file=sys.stderr)
        return str(path)

    def get_page_text(self, max_chars: int = 2000) -> str:
        page = self._ensure_page()
        with suppress(Exception):
            text = page.evaluate("""() => {
                const body = document.body;
                if (!body) return '';
                const clone = body.cloneNode(true);
                clone.querySelectorAll('script, style, noscript, [hidden], [aria-hidden="true"]').forEach(el => el.remove());
                return (clone.innerText || '').trim();
            }""")
            if text:
                return text[:max_chars]
        return ""

    def get_visible_dom(self) -> dict[str, Any]:
        """Extract visible DOM with semantic annotations: buttons, links, inputs, SKU options.

        Returns a compact representation the AI can use alongside screenshots to identify
        clickable elements and understand page structure.
        """
        page = self._ensure_page()
        with suppress(Exception):
            return page.evaluate("""() => {
                const viewportH = window.innerHeight;
                const viewportW = window.innerWidth;

                function isVisible(el) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) return false;
                    if (rect.bottom < 0 || rect.top > viewportH) return false;
                    if (rect.right < 0 || rect.left > viewportW) return false;
                    const style = window.getComputedStyle(el);
                    if (style.visibility === 'hidden' || style.display === 'none') return false;
                    if (parseFloat(style.opacity) === 0) return false;
                    // Check if obscured by another element at center
                    const cx = rect.left + rect.width / 2;
                    const cy = rect.top + rect.height / 2;
                    const topEl = document.elementFromPoint(cx, cy);
                    if (topEl && topEl !== el && !el.contains(topEl)) return false;
                    return true;
                }

                function getSemanticRole(el) {
                    const tag = el.tagName.toLowerCase();
                    const role = el.getAttribute('role') || '';
                    const type = (el.getAttribute('type') || '').toLowerCase();
                    if (tag === 'button' || role === 'button') return 'button';
                    if (tag === 'a' && el.getAttribute('href')) return 'link';
                    if (tag === 'input' && (type === 'text' || type === 'search')) return 'text_input';
                    if (tag === 'input' && type === 'submit') return 'button';
                    if (tag === 'select') return 'select';
                    if (tag === 'textarea') return 'text_input';
                    return tag;
                }

                function getSkuInfo(el) {
                    const cls = (el.getAttribute('class') || '').toLowerCase();
                    const text = (el.innerText || el.textContent || '').trim().slice(0, 60);
                    if (cls.includes('sku') || cls.includes('prop')) return 'sku_option';
                    if (cls.includes('valueitem') || cls.includes('skuvalue')) return 'sku_value';
                    return null;
                }

                const result = {url: window.location.href, title: document.title, elements: []};

                // Buttons
                document.querySelectorAll('button, [role="button"], a.btn, span.btn').forEach(el => {
                    if (!isVisible(el)) return;
                    const rect = el.getBoundingClientRect();
                    const text = (el.innerText || el.textContent || '').trim().slice(0, 80);
                    if (!text) return;
                    result.elements.push({
                        role: 'button',
                        text: text,
                        selector: el.id ? '#' + el.id : null,
                        box: {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)}
                    });
                });

                // Links
                document.querySelectorAll('a[href]').forEach(el => {
                    if (!isVisible(el)) return;
                    const text = (el.innerText || el.textContent || '').trim().slice(0, 80);
                    const href = el.getAttribute('href') || '';
                    if (!text && !href) return;
                    const rect = el.getBoundingClientRect();
                    result.elements.push({
                        role: 'link',
                        text: text,
                        href: href.slice(0, 120),
                        box: {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)}
                    });
                });

                // Inputs
                document.querySelectorAll('input:not([type="hidden"]), textarea, select').forEach(el => {
                    if (!isVisible(el)) return;
                    const rect = el.getBoundingClientRect();
                    const placeholder = el.getAttribute('placeholder') || '';
                    result.elements.push({
                        role: getSemanticRole(el),
                        placeholder: placeholder.slice(0, 60),
                        selector: el.id ? '#' + el.id : (el.name ? '[name="' + el.name + '"]' : null),
                        box: {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)}
                    });
                });

                // SKU options (Taobao-specific)
                document.querySelectorAll('[class*="skuItem"], [class*="valueItem"], li[class*="sku"], .J_TSaleProp li, .tb-prop li').forEach(el => {
                    if (!isVisible(el)) return;
                    const text = (el.innerText || el.textContent || '').trim().slice(0, 40);
                    if (!text || text.length > 40) return;
                    const rect = el.getBoundingClientRect();
                    const cls = el.getAttribute('class') || '';
                    const disabled = cls.includes('disabled') || cls.includes('out-of');
                    const selected = cls.includes('selected') || cls.includes('active') || cls.includes('isSelected');
                    result.elements.push({
                        role: 'sku_option',
                        text: text,
                        disabled: disabled,
                        selected: selected,
                        box: {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)}
                    });
                });

                // Price elements
                const priceSelectors = [
                    '#J_StrPr498', '.tm-price', '.tb-rmb-num',
                    '[class*="price"]:not([class*="price-"])',
                    '.tm-promo-price .tm-price', '.tb-item-price',
                    '.sku-price .price-value', '.J_original_price'
                ];
                for (const sel of priceSelectors) {
                    const el = document.querySelector(sel);
                    if (el && isVisible(el)) {
                        const rect = el.getBoundingClientRect();
                        const text = (el.innerText || el.textContent || '').trim().slice(0, 30);
                        if (text) {
                            result.elements.push({
                                role: 'price',
                                text: text,
                                selector: sel,
                                box: {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)}
                            });
                        }
                    }
                }

                return result;
            }""")
        return {"url": "", "title": "", "elements": []}

    def get_sku_structure(self) -> list[dict[str, Any]]:
        """Extract SKU groups with labels and selectable values from the current page."""
        page = self._ensure_page()
        with suppress(Exception):
            page.wait_for_selector('[class*="sku"]', timeout=5000)
        with suppress(Exception):
            groups = page.evaluate("""() => {
                const groups = [];
                // Find SKU property groups (dl.prop structure or skuGroup containers)
                const groupEls = document.querySelectorAll('dl[class*="prop"], [class*="skuGroup"], .J_TSaleProp');
                for (const g of groupEls) {
                    const labelEl = g.querySelector('dt, [class*="label"], [class*="title"]');
                    const label = labelEl ? labelEl.textContent.trim().replace(/[:：]$/, '').trim() : '';
                    const values = [];
                    const valueEls = g.querySelectorAll('dd a, li a, [class*="valueItem"]:not([class*="label"]), [class*="skuItem"]');
                    for (const v of valueEls) {
                        const text = (v.innerText || v.textContent || '').trim();
                        if (!text || text.length > 40) continue;
                        const cls = v.getAttribute('class') || '';
                        values.push({
                            text: text,
                            disabled: cls.includes('disabled') || cls.includes('out-of'),
                            selected: cls.includes('selected') || cls.includes('active') || cls.includes('isSelected')
                        });
                    }
                    if (label || values.length) groups.push({label, values});
                }
                return groups;
            }""")
            if groups:
                return groups
        return []

    def get_element_state(self, text: str) -> dict[str, Any] | None:
        """Check if an element with given text is visible, clickable, and not obscured."""
        page = self._ensure_page()
        with suppress(Exception):
            return page.evaluate("""(searchText) => {
                const candidates = [];
                const walker = document.createTreeWalker(
                    document.body, NodeFilter.SHOW_ELEMENT,
                    {acceptNode: n => n.innerText && n.innerText.trim().includes(searchText) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP}
                );
                let node;
                while ((node = walker.nextNode()) && candidates.length < 10) {
                    const rect = node.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    const style = window.getComputedStyle(node);
                    if (style.display === 'none' || style.visibility === 'hidden') continue;
                    const cx = rect.left + rect.width / 2;
                    const cy = rect.top + rect.height / 2;
                    const topEl = document.elementFromPoint(cx, cy);
                    candidates.push({
                        tag: node.tagName.toLowerCase(),
                        text: (node.innerText || '').trim().slice(0, 60),
                        visible: true,
                        clickable: topEl === node || node.contains(topEl),
                        box: {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)}
                    });
                }
                return candidates.length > 0 ? candidates[0] : null;
            }""", text)
        return None

    # ──────────────────────────────────────────────
    # Visual Agent: Atomic Actions
    # ──────────────────────────────────────────────

    def select_sku(self, label_keyword: str, value_keyword: str) -> bool:
        """Select a SKU option by group label and value text.

        Strategy 1: Playwright locators with scrollIntoView + human click.
        Strategy 2: JS fallback with proper visibility check and mouse events.
        """
        page = self._ensure_page()

        # Strategy 1: Playwright locators
        from taobao_selectors import SKU_GROUP_LABEL_SELECTORS
        with suppress(Exception):
            for group_sel in SKU_GROUP_LABEL_SELECTORS:
                group_loc = page.locator(group_sel).first
                if not group_loc.is_visible(timeout=2000):
                    continue
                label_text = group_loc.inner_text()
                if label_keyword not in label_text:
                    continue
                # Found matching group — look for the value option within the parent
                parent = group_loc.locator("xpath=..")
                value_loc = parent.locator(f"text={value_keyword}").first
                if value_loc.is_visible(timeout=2000):
                    # Check not disabled
                    cls = value_loc.get_attribute("class") or ""
                    if "disabled" not in cls and "out-of" not in cls:
                        value_loc.scroll_into_view_if_needed()
                        self._human_click(page, value_loc)
                        self._human_wait(0.5, 1.0)
                        page.wait_for_timeout(500)
                        print(f"[browser] select_sku: Playwright click label='{label_keyword}' value='{value_keyword}'", file=sys.stderr)
                        return True

        # Strategy 2: JS fallback with proper visibility + scrollIntoView + mouse events
        with suppress(Exception):
            result = page.evaluate("""([labelKw, valueKw]) => {
                const viewportH = window.innerHeight;
                const viewportW = window.innerWidth;

                function isVisible(el) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) return false;
                    if (rect.bottom < 0 || rect.top > viewportH) return false;
                    if (rect.right < 0 || rect.left > viewportW) return false;
                    const style = window.getComputedStyle(el);
                    if (style.visibility === 'hidden' || style.display === 'none') return false;
                    if (parseFloat(style.opacity) === 0) return false;
                    const cx = rect.left + rect.width / 2;
                    const cy = rect.top + rect.height / 2;
                    const topEl = document.elementFromPoint(cx, cy);
                    if (topEl && topEl !== el && !el.contains(topEl)) return false;
                    return true;
                }

                const groups = document.querySelectorAll('dl[class*="prop"], [class*="skuGroup"], .J_TSaleProp');
                for (const g of groups) {
                    const labelEl = g.querySelector('dt, [class*="label"], [class*="title"]');
                    const label = labelEl ? labelEl.textContent.trim() : '';
                    if (!label.includes(labelKw)) continue;
                    const values = g.querySelectorAll('dd a, li a, [class*="valueItem"], [class*="skuItem"]');
                    for (const v of values) {
                        const text = (v.innerText || v.textContent || '').trim();
                        const cls = v.getAttribute('class') || '';
                        if (text.includes(valueKw) && !cls.includes('disabled') && !cls.includes('out-of')) {
                            if (!isVisible(v)) continue;
                            v.scrollIntoView({behavior: "instant", block: "center"});
                            v.dispatchEvent(new MouseEvent("mousedown", {bubbles: true, cancelable: true}));
                            v.dispatchEvent(new MouseEvent("mouseup", {bubbles: true, cancelable: true}));
                            v.dispatchEvent(new MouseEvent("click", {bubbles: true, cancelable: true}));
                            return {clicked: true, label: label, value: text};
                        }
                    }
                }
                return {clicked: false};
            }""", [label_keyword, value_keyword])
            if isinstance(result, dict) and result.get("clicked"):
                print(f"[browser] select_sku: JS fallback click label='{result.get('label','')}' value='{result.get('value','')}'", file=sys.stderr)
                return True
            print(f"[browser] select_sku: no match for label='{label_keyword}' value='{value_keyword}'", file=sys.stderr)
        return False

    def select_skus(self, selections: list[dict[str, str]]) -> tuple[bool, list[dict[str, Any]]]:
        """Select multiple SKU options sequentially.

        Args:
            selections: [{"label": "芯片", "value": "M4"}, {"label": "内存", "value": "16G"}, ...]

        Returns:
            (all_ok, results) where results = [{"label":"...", "value":"...", "ok": True/False}, ...]
        """
        results: list[dict[str, Any]] = []
        for sel in selections:
            label = sel.get("label", "")
            value = sel.get("value", "")
            ok = self.select_sku(label, value)
            results.append({"label": label, "value": value, "ok": ok})
            if ok:
                self._human_wait(0.8, 1.5)
        all_ok = all(r["ok"] for r in results)
        return all_ok, results

    def click_element_by_text(self, text: str, role: str = "any") -> bool:
        """Find and click a visible element containing the given text."""
        page = self._ensure_page()
        # Try Playwright locator first (handles shadow DOM, iframes)
        with suppress(Exception):
            locator = page.locator(f"text={text}").first
            if locator.is_visible(timeout=3000):
                self._human_click(page, locator)
                self._human_wait(0.3, 0.8)
                return True
        # Try role-specific locators
        roles = [role] if role != "any" else ["button", "link"]
        for r in roles:
            with suppress(Exception):
                locator = page.locator(f"{r}:has-text('{text}')").first
                if locator.is_visible(timeout=2000):
                    self._human_click(page, locator)
                    self._human_wait(0.3, 0.8)
                    return True
        # Fallback: evaluate click by text match with proper visibility check
        with suppress(Exception):
            clicked = page.evaluate("""(t) => {
                const viewportH = window.innerHeight;
                const viewportW = window.innerWidth;

                function isVisible(el) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) return false;
                    if (rect.bottom < 0 || rect.top > viewportH) return false;
                    if (rect.right < 0 || rect.left > viewportW) return false;
                    const style = window.getComputedStyle(el);
                    if (style.visibility === 'hidden' || style.display === 'none') return false;
                    if (parseFloat(style.opacity) === 0) return false;
                    const cx = rect.left + rect.width / 2;
                    const cy = rect.top + rect.height / 2;
                    const topEl = document.elementFromPoint(cx, cy);
                    if (topEl && topEl !== el && !el.contains(topEl)) return false;
                    return true;
                }

                const interactiveSelectors = 'button, a[href], input, select, textarea, [role="button"], [role="link"], [onclick]';
                const els = document.querySelectorAll(interactiveSelectors);
                for (const el of els) {
                    if ((el.innerText || el.textContent || '').trim().includes(t)) {
                        if (!isVisible(el)) continue;
                        el.scrollIntoView({behavior: "instant", block: "center"});
                        el.dispatchEvent(new MouseEvent("mousedown", {bubbles: true, cancelable: true}));
                        el.dispatchEvent(new MouseEvent("mouseup", {bubbles: true, cancelable: true}));
                        el.dispatchEvent(new MouseEvent("click", {bubbles: true, cancelable: true}));
                        return true;
                    }
                }
                return false;
            }""", text)
            if clicked:
                self._human_wait(0.3, 0.8)
                return True
        return False

    def scroll_page(self, direction: str = "down", amount: int = 500,
                    selector: str | None = None) -> None:
        """Scroll the page or a specific container. direction: 'up' or 'down'."""
        page = self._ensure_page()
        sign = -1 if direction == "up" else 1
        if selector:
            with suppress(Exception):
                page.evaluate(
                    """(s, amt) => {
                        const el = document.querySelector(s);
                        if (el) { el.scrollBy(0, amt); return true; }
                        return false;
                    }""",
                    [selector, sign * amount]
                )
        else:
            self._human_scroll(page, page.evaluate("window.scrollY") + sign * amount)

    def wait_for_element(self, text: str | None = None, selector: str | None = None,
                         timeout_ms: int = 10000) -> bool:
        """Wait for an element to appear on the page."""
        page = self._ensure_page()
        if selector:
            with suppress(Exception):
                page.wait_for_selector(selector, timeout=timeout_ms)
                return True
        if text:
            with suppress(Exception):
                page.wait_for_selector(f"text={text}", timeout=timeout_ms)
                return True
        return False

    # ──────────────────────────────────────────────
    # Human-Like Behavior: Bezier Mouse Movement
    # ──────────────────────────────────────────────

    def _bezier_curve(self, start: tuple[float, float], end: tuple[float, float], steps: int = 20) -> list[tuple[float, float]]:
        """Generate a cubic Bezier curve path from start to end with random control points."""
        sx, sy = start
        ex, ey = end
        dist = math.hypot(ex - sx, ey - sy)

        # Random control points — create natural curvature
        jitter = max(dist * 0.3, 30)
        cp1 = (
            sx + (ex - sx) * random.uniform(0.1, 0.4) + random.uniform(-jitter, jitter),
            sy + (ey - sy) * random.uniform(0.1, 0.4) + random.uniform(-jitter, jitter),
        )
        cp2 = (
            sx + (ex - sx) * random.uniform(0.6, 0.9) + random.uniform(-jitter, jitter),
            sy + (ey - sy) * random.uniform(0.6, 0.9) + random.uniform(-jitter, jitter),
        )

        points = []
        for i in range(steps + 1):
            t = i / steps
            # Ease-in-out: accelerate then decelerate
            t_eased = t * t * (3 - 2 * t)

            x = (1 - t_eased) ** 3 * sx + 3 * (1 - t_eased) ** 2 * t_eased * cp1[0] + 3 * (1 - t_eased) * t_eased ** 2 * cp2[0] + t_eased ** 3 * ex
            y = (1 - t_eased) ** 3 * sy + 3 * (1 - t_eased) ** 2 * t_eased * cp1[1] + 3 * (1 - t_eased) * t_eased ** 2 * cp2[1] + t_eased ** 3 * ey

            # Add slight random jitter
            x += random.uniform(-1.5, 1.5)
            y += random.uniform(-1.5, 1.5)

            points.append((x, y))

        return points

    def _human_click(self, page: Page, locator, timeout: int = 5000) -> None:
        """Click a locator with human-like Bezier mouse movement."""
        try:
            box = locator.bounding_box(timeout=timeout)
            if box is None:
                locator.click(timeout=timeout)
                return

            # Click at a random point within the element (not always center)
            target_x = box["x"] + box["width"] * random.uniform(0.25, 0.75)
            target_y = box["y"] + box["height"] * random.uniform(0.3, 0.7)

            # Get current mouse position (default to a random start)
            current_x = random.randint(400, 800)
            current_y = random.randint(300, 600)

            # Generate and follow Bezier curve
            points = self._bezier_curve((current_x, current_y), (target_x, target_y), steps=random.randint(15, 30))
            for px, py in points:
                page.mouse.move(px, py)
                time.sleep(random.uniform(0.005, 0.02))

            # Small pause before clicking
            time.sleep(random.uniform(0.05, 0.15))
            page.mouse.click(target_x, target_y)

        except Exception:
            # Fallback to standard click
            with suppress(Exception):
                locator.click(timeout=timeout)

    def _human_type(self, page: Page, text: str) -> None:
        """Type text with human-like variable delays and occasional pauses."""
        for i, char in enumerate(text):
            page.keyboard.type(char)

            # Variable delay between keystrokes
            base_delay = random.uniform(0.03, 0.12)

            # Occasional longer pause (simulating thinking)
            if random.random() < 0.08:
                base_delay = random.uniform(0.2, 0.5)

            time.sleep(base_delay)

    def _human_scroll(self, page: Page, target_y: int = 800) -> None:
        """Scroll the page with human-like variable speed and slight back-scrolling."""
        current_y = page.evaluate("window.scrollY")
        remaining = target_y - current_y

        while abs(remaining) > 50:
            # Variable scroll increment
            step = min(abs(remaining), random.randint(80, 350))
            direction = 1 if remaining > 0 else -1

            # Occasionally scroll back slightly
            if random.random() < 0.15 and abs(remaining) > 200:
                step = -random.randint(20, 60)
                direction = -direction

            page.evaluate(f"window.scrollBy(0, {step * direction})")
            time.sleep(random.uniform(0.3, 1.0))

            current_y = page.evaluate("window.scrollY")
            remaining = target_y - current_y

    def _human_wait(self, min_s: float = 0.5, max_s: float = 2.0) -> None:
        """Wait a random duration to simulate human pause."""
        time.sleep(random.uniform(min_s, max_s))

    def _random_mouse_move(self, page: Page) -> None:
        """Move mouse to a random viewport position (simulating user looking around)."""
        viewport = page.viewport_size
        if viewport is None:
            return
        target_x = random.randint(100, viewport["width"] - 100)
        target_y = random.randint(200, viewport["height"] - 200)
        start_x = random.randint(200, 600)
        start_y = random.randint(200, 500)
        points = self._bezier_curve(
            (start_x, start_y), (target_x, target_y), steps=random.randint(10, 20)
        )
        for px, py in points:
            page.mouse.move(px, py)
            time.sleep(random.uniform(0.008, 0.025))

    def _simulate_browsing(self, page: Page, max_scroll: int = 1200) -> None:
        """Simulate a user browsing the page: scroll in stages, pause, look around."""
        scroll_targets = [
            random.randint(200, 400),
            random.randint(500, 800),
            random.randint(800, max_scroll),
        ]
        for target in scroll_targets:
            self._human_scroll(page, target)
            self._human_wait(0.5, 1.5)
            if random.random() < 0.4:
                self._random_mouse_move(page)
        self._human_scroll(page, random.randint(0, 300))
        self._human_wait(0.3, 1)

    # ──────────────────────────────────────────────
    # Access Control Detection
    # ──────────────────────────────────────────────

    def _wait_for_access_recovery(self, page: Page) -> None:
        if not self._looks_access_blocked(page):
            return
        check_interval = 30
        max_checks = 7
        for i in range(max_checks):
            time.sleep(check_interval)
            total_slept = (i + 1) * check_interval
            print(f"[browser] 等待手动验证通过... 第 {i+1}/{max_checks} 次检测 (已等待 {total_slept}s)", file=sys.stderr)
            if not self._looks_access_blocked(page):
                print(f"[browser] 验证通过! (等待约 {total_slept}s)", file=sys.stderr)
                return

    def _looks_access_blocked(self, page: Page) -> bool:
        # Priority 1: CSS class-based CAPTCHA detection (robust against text changes)
        all_captcha_selectors = CAPTCHA_PANEL_SELECTORS + CAPTCHA_SLIDER_BTN_SELECTORS + CAPTCHA_BG_SELECTORS
        for selector in all_captcha_selectors:
            with suppress(Exception):
                if page.locator(selector).first.is_visible(timeout=1000):
                    return True
        # Priority 2: Text-based signals (fallback)
        for selector in ACCESS_BLOCKED_SIGNALS:
            with suppress(Exception):
                if page.locator(selector).first.is_visible():
                    return True
        # Priority 3: Page title check
        title = ""
        with suppress(Exception):
            title = page.title().lower()
        if "access denied" in title or "验证" in title:
            return True
        return False

    def _handle_captcha_if_present(self, page: Page) -> bool:
        """Detect and auto-solve slider CAPTCHA if present. Returns True if solved."""
        if self._slider_solver.is_captcha_present(page):
            print("[browser] slider CAPTCHA detected, attempting auto-solve...", file=sys.stderr)
            return self._slider_solver.solve(page, max_retries=3)
        return False

    # ──────────────────────────────────────────────
    # DOM Helpers
    # ──────────────────────────────────────────────

    def _find_first_visible_locator(self, page: Page, selectors: Iterable[str]):
        for selector in selectors:
            with suppress(Exception):
                locator = page.locator(selector).first
                if locator.is_visible():
                    return locator
        return None

    def _find_candidate_links(self, page: Page) -> list[dict[str, str]]:
        with suppress(Exception):
            results = page.evaluate("""(selectors, cardSelectors) => {
                const results = [];
                const seen = new Set();
                for (const sel of selectors) {
                    const anchors = document.querySelectorAll(sel);
                    for (const a of anchors) {
                        const href = (a.getAttribute('href') || '').trim();
                        if (!href || seen.has(href) || seen.size >= 50) continue;
                        seen.add(href);
                        const text = (a.innerText || '').trim();
                        const title = text.split('\\n')[0].trim();
                        if (!title) continue;
                        let card = null;
                        for (const cs of cardSelectors) {
                            card = a.closest(cs);
                            if (card) break;
                        }
                        if (!card) {
                            let p = a.parentElement;
                            for (let i = 0; i < 3 && p; i++) { p = p.parentElement; }
                            card = p || a.parentElement;
                        }
                        const cardText = card ? (card.innerText || '').trim() : text;
                        results.push({ href, text, title, card_text: cardText });
                        if (results.length >= 50) break;
                    }
                    if (results.length > 0) break;
                }
                return results;
            }""", [PRODUCT_LINK_SELECTORS, PRODUCT_CARD_CLIMB_SELECTORS])
            if isinstance(results, list) and results:
                print(f"[browser] found {len(results)} candidate links via evaluate", file=sys.stderr)
                return results

        # Fallback to Playwright locators
        links: list[dict[str, str]] = []
        for selector in PRODUCT_LINK_SELECTORS:
            with suppress(Exception):
                for locator in page.locator(selector).all()[:40]:
                    with suppress(Exception):
                        href = locator.get_attribute("href") or ""
                        text = (locator.inner_text() or "").strip()
                        title = text.split("\n")[0].strip() if text else ""
                        if href and title:
                            links.append({"href": href, "text": text, "title": title, "card_text": text})
                if links:
                    break
        return links

    # ──────────────────────────────────────────────
    # Text Processing
    # ──────────────────────────────────────────────

    # ──────────────────────────────────────────────
    # Search Result Card Field Extractors
    # ──────────────────────────────────────────────

    def _extract_price(self, card_text: str) -> tuple[str | None, float | None]:
        patterns = [
            r'¥\s*([\d,]+(?:\.\d{1,2})?)',
            r'￥\s*([\d,]+(?:\.\d{1,2})?)',
            r'([\d,]+(?:\.\d{1,2})?)\s*元',
            r'([\d,]+(?:\.\d{1,2})?)\s*起',
            r'价格[：:]\s*([\d,]+(?:\.\d{1,2})?)',
        ]
        for pattern in patterns:
            match = re.search(pattern, card_text)
            if match:
                raw = match.group(1).replace(",", "")
                try:
                    val = float(raw)
                    if 0.01 <= val <= 999999:
                        return f"¥{val:.2f}", val
                except ValueError:
                    continue

        # Try ranges like "299-499"
        range_match = re.search(r'¥?\s*([\d,]+(?:\.\d{1,2})?)\s*[-~—]\s*¥?\s*([\d,]+(?:\.\d{1,2})?)', card_text)
        if range_match:
            try:
                low = float(range_match.group(1).replace(",", ""))
                return f"¥{low:.2f}", low
            except ValueError:
                pass
        return None, None

    def _extract_sales_count(self, card_text: str) -> int | None:
        patterns = [
            r'([\d.]+)\s*万?\+\s*人付款',
            r'([\d.]+)万\+?\s*人付款',
            r'月销\s*([\d.]+)\s*万?\s*',
            r'已售\s*([\d.]+)\s*万?\s*',
            r'销量\s*([\d.]+)\s*万?\s*',
            r'收货\s*([\d.]+)\s*万?\s*',
            r'付款\s*([\d.]+)\s*万?\s*',
        ]
        for pattern in patterns:
            match = re.search(pattern, card_text)
            if match:
                raw = match.group(1)
                try:
                    val = float(raw)
                except ValueError:
                    continue
                # Check for "万" unit
                span = card_text[match.start():match.end()]
                if "万" in span:
                    val *= 10000
                return int(val)

        # Pattern: "1000+人付款" or "1.2万人付款"
        pattern2 = r'([\d.]+)\s*w?\s*人付款'
        match = re.search(pattern2, card_text)
        if match:
            try:
                val = float(match.group(1))
                if "万" in card_text[max(0, match.start()-5):match.end()]:
                    val *= 10000
                return int(val)
            except ValueError:
                pass
        return None

    def _check_free_shipping(self, card_text: str) -> bool:
        return bool(re.search(r'包邮|免邮|运费\s*[0０]|免运费', card_text))

    def _check_is_tmall(self, url: str, card_text: str) -> bool:
        if "tmall.com" in url.lower():
            return True
        return bool(re.search(r'天猫|Tmall|TMALL', card_text))

    # ──────────────────────────────────────────────

    def _build_keyword_tokens(self, keyword: str) -> list[str]:
        normalized = "".join(keyword.split())
        if len(normalized) < 2:
            return [normalized] if normalized else []
        tokens = {normalized}
        for index in range(len(normalized) - 1):
            chunk = normalized[index : index + 2]
            if len(chunk) == 2:
                tokens.add(chunk)
        return [token for token in tokens if token]

    def _matches_keyword(self, title: str, text: str, tokens: list[str]) -> bool:
        haystack = f"{title}\n{text}"
        return any(token in haystack for token in tokens)

    def _extract_rating(self, text: str) -> float | None:
        patterns = [
            r"好评率\s*([\d.]+)%",
            r"([\d.]+)%\s*好评",
            r"好评\s*([\d.]+)%",
            r"好评率[：:]\s*([\d.]+)%",
            r"好评率[：:]\s*([\d.]+)(?![.\d])",
            r"好评[率]?[：:]\s*([\d.]+)%",
            r"用户评价[：:]\s*([\d.]+)%",
            r"([\d.]+)%\s*[正好评]",
            r"好评.*?([\d.]+)%",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                try:
                    val = float(match.group(1))
                    if 0 < val <= 1.0:
                        return val
                    elif val > 1.0:
                        return val / 100.0
                except ValueError:
                    continue
        return None
