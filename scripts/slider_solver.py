from __future__ import annotations

import random
import sys
import time
from contextlib import suppress
from typing import Any

import cv2
import ddddocr
import numpy as np
from playwright.sync_api import Page

from taobao_selectors import (
    CAPTCHA_BG_SELECTORS, CAPTCHA_PANEL_SELECTORS, CAPTCHA_REFRESH_SELECTORS,
    CAPTCHA_SLICE_SELECTORS, CAPTCHA_SLIDER_BTN_SELECTORS, CAPTCHA_SUCCESS_SELECTORS,
    CAPTCHA_TEXT_FALLBACKS,
)


class SliderSolver:
    """Automatic slider CAPTCHA solver using ddddocr (ML) + OpenCV (fallback)."""

    def __init__(self, method: str = "ddddocr") -> None:
        self.method = method
        self._det: ddddocr.DdddOcr | None = None
        if method == "ddddocr":
            self._det = ddddocr.DdddOcr(det=False, ocr=False, show_ad=False)

    # ──────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────

    def is_captcha_present(self, page: Page) -> bool:
        """Check if a slider CAPTCHA is currently visible on the page."""
        # Priority 1: GeeTest panel containers (class-based)
        for selector in CAPTCHA_PANEL_SELECTORS:
            with suppress(Exception):
                if page.locator(selector).first.is_visible(timeout=800):
                    return True

        # Priority 2: Slider button elements (class-based)
        for selector in CAPTCHA_SLIDER_BTN_SELECTORS:
            with suppress(Exception):
                if page.locator(selector).first.is_visible(timeout=800):
                    return True

        # Priority 3: Text fallback
        for text_sel in CAPTCHA_TEXT_FALLBACKS:
            with suppress(Exception):
                if page.locator(text_sel).first.is_visible(timeout=500):
                    return True

        return False

    def solve(self, page: Page, max_retries: int = 3) -> bool:
        """
        Detect and solve slider CAPTCHA on the current page.

        Returns True if solved successfully, False if all retries failed.
        """
        for attempt in range(max_retries):
            print(f"[slider] Attempt {attempt + 1}/{max_retries}", file=sys.stderr)

            # Find CAPTCHA elements
            bg_el, slice_el, slider_btn = self._find_captcha_elements(page)
            if slider_btn is None:
                print("[slider] No slider button found", file=sys.stderr)
                return False

            # Screenshot the background and slider piece
            bg_bytes = self._screenshot_element(page, bg_el)
            slice_bytes = self._screenshot_element(page, slice_el) if slice_el else None

            if bg_bytes is None:
                print("[slider] Failed to screenshot background", file=sys.stderr)
                continue

            # Detect gap position
            gap_x = self._detect_gap(bg_bytes, slice_bytes)
            if gap_x is None or gap_x <= 0:
                print("[slider] Gap detection failed, retrying...", file=sys.stderr)
                self._refresh_captcha(page)
                time.sleep(1)
                continue

            # Calculate drag distance (account for element scaling)
            bg_box = bg_el.bounding_box() if bg_el else None
            if bg_box and bg_bytes:
                bg_img = cv2.imdecode(np.frombuffer(bg_bytes, np.uint8), cv2.IMREAD_COLOR)
                if bg_img is not None:
                    scale = bg_box["width"] / bg_img.shape[1]
                    drag_distance = int(gap_x * scale)
                else:
                    drag_distance = gap_x
            else:
                drag_distance = gap_x

            print(f"[slider] Gap at x={gap_x}, drag distance={drag_distance}px", file=sys.stderr)

            # Perform human-like drag
            self._human_drag(page, slider_btn, drag_distance)

            # Wait and check result
            time.sleep(2)

            if self._check_solved(page):
                print("[slider] CAPTCHA solved successfully!", file=sys.stderr)
                return True
            else:
                print("[slider] Solve failed, retrying...", file=sys.stderr)
                self._refresh_captcha(page)
                time.sleep(1)

        print("[slider] All retries exhausted", file=sys.stderr)
        return False

    # ──────────────────────────────────────────────
    # Element Detection
    # ──────────────────────────────────────────────

    def _find_captcha_elements(self, page: Page) -> tuple:
        """Find background, slice, and slider button elements."""
        bg_el = None
        slice_el = None
        slider_btn = None

        for sel in CAPTCHA_BG_SELECTORS:
            with suppress(Exception):
                loc = page.locator(sel).first
                if loc.is_visible(timeout=1500):
                    bg_el = loc
                    print(f"[slider] found bg: {sel}", file=sys.stderr)
                    break

        for sel in CAPTCHA_SLICE_SELECTORS:
            with suppress(Exception):
                loc = page.locator(sel).first
                if loc.is_visible(timeout=1500):
                    slice_el = loc
                    print(f"[slider] found slice: {sel}", file=sys.stderr)
                    break

        for sel in CAPTCHA_SLIDER_BTN_SELECTORS:
            with suppress(Exception):
                loc = page.locator(sel).first
                if loc.is_visible(timeout=1500):
                    slider_btn = loc
                    print(f"[slider] found slider btn: {sel}", file=sys.stderr)
                    break

        # Last resort: try to find any canvas element (GeeTest uses canvas)
        if bg_el is None:
            with suppress(Exception):
                canvases = page.locator("canvas").all()
                for c in canvases:
                    with suppress(Exception):
                        if c.is_visible(timeout=500):
                            bg_el = c
                            print("[slider] found bg via canvas fallback", file=sys.stderr)
                            break

        return bg_el, slice_el, slider_btn

    def _screenshot_element(self, page: Page, locator) -> bytes | None:
        """Take a screenshot of a specific element, return as bytes."""
        if locator is None:
            return None
        with suppress(Exception):
            return locator.screenshot()
        return None

    # ──────────────────────────────────────────────
    # Gap Detection
    # ──────────────────────────────────────────────

    def _detect_gap(self, bg_bytes: bytes, slice_bytes: bytes | None = None) -> int | None:
        """Detect the x-position of the gap in the background image."""
        if self.method == "ddddocr" and self._det is not None:
            return self._detect_gap_ddddocr(bg_bytes, slice_bytes)
        return self._detect_gap_opencv(bg_bytes, slice_bytes)

    def _detect_gap_ddddocr(self, bg_bytes: bytes, slice_bytes: bytes | None = None) -> int | None:
        """Use ddddocr ML model to detect gap position."""
        try:
            if slice_bytes:
                result = self._det.slide_match(slice_bytes, bg_bytes, simple_target=True)
                if result and "target" in result:
                    x = result["target"][0]
                    print(f"[slider/ddddocr] Gap at x={x}", file=sys.stderr)
                    return x

            # Fallback: use ddddocr's detection without slice
            result = self._det.slide_match(bg_bytes, bg_bytes, simple_target=True)
            if result and "target" in result:
                return result["target"][0]
        except Exception as e:
            print(f"[slider/ddddocr] Error: {e}", file=sys.stderr)
        return None

    def _detect_gap_opencv(self, bg_bytes: bytes, slice_bytes: bytes | None = None) -> int | None:
        """Use OpenCV Canny edge detection + template matching."""
        try:
            bg_arr = np.frombuffer(bg_bytes, np.uint8)
            bg = cv2.imdecode(bg_arr, cv2.IMREAD_COLOR)
            if bg is None:
                return None

            bg_gray = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY)
            bg_blur = cv2.GaussianBlur(bg_gray, (5, 5), 0)
            bg_edge = cv2.Canny(bg_blur, 100, 200)

            if slice_bytes:
                # Template matching with the slider piece
                slider_arr = np.frombuffer(slice_bytes, np.uint8)
                slider = cv2.imdecode(slider_arr, cv2.IMREAD_UNCHANGED)
                if slider is not None:
                    if len(slider.shape) == 3 and slider.shape[2] == 4:
                        slider = slider[:, :, :3]
                    slider_gray = cv2.cvtColor(slider, cv2.COLOR_BGR2GRAY)
                    slider_edge = cv2.Canny(cv2.GaussianBlur(slider_gray, (5, 5), 0), 100, 200)

                    result = cv2.matchTemplate(bg_edge, slider_edge, cv2.TM_CCOEFF_NORMED)
                    _, max_val, _, max_loc = cv2.minMaxLoc(result)
                    print(f"[slider/opencv] Gap at x={max_loc[0]}, confidence={max_val:.4f}", file=sys.stderr)
                    return max_loc[0]

            # Without slice: detect the gap by finding the darkest vertical column
            # (the gap is usually darker than the surrounding area)
            sobel_x = cv2.Sobel(bg_gray, cv2.CV_64F, 1, 0, ksize=3)
            abs_sobel = np.abs(sobel_x).astype(np.uint8)
            col_sums = np.sum(abs_sobel, axis=0)

            # Find the region with highest edge activity (gap edges)
            min_x = int(bg.shape[1] * 0.2)  # Skip leftmost 20%
            max_x = int(bg.shape[1] * 0.9)
            search_region = col_sums[min_x:max_x]
            gap_x = int(np.argmax(search_region)) + min_x
            print(f"[slider/opencv] Estimated gap at x={gap_x}", file=sys.stderr)
            return gap_x

        except Exception as e:
            print(f"[slider/opencv] Error: {e}", file=sys.stderr)
            return None

    # ──────────────────────────────────────────────
    # Human-Like Drag
    # ──────────────────────────────────────────────

    def _human_drag(self, page: Page, slider_btn, distance: int) -> None:
        """Perform a human-like drag using Bezier curve trajectory."""
        box = slider_btn.bounding_box()
        if box is None:
            return

        start_x = box["x"] + box["width"] / 2
        start_y = box["y"] + box["height"] / 2
        end_x = start_x + distance
        end_y = start_y

        # Generate Bezier trajectory
        trajectory = self._generate_trajectory((start_x, start_y), (end_x, end_y))

        # Move to slider button
        page.mouse.move(start_x, start_y)
        time.sleep(random.uniform(0.1, 0.3))

        # Press and hold
        page.mouse.down()
        time.sleep(random.uniform(0.05, 0.15))

        # Follow trajectory
        for x, y, delay_ms in trajectory:
            page.mouse.move(x, y)
            time.sleep(delay_ms / 1000.0)

        # Release
        time.sleep(random.uniform(0.03, 0.1))
        page.mouse.up()

    def _generate_trajectory(
        self, start: tuple[float, float], end: tuple[float, float]
    ) -> list[tuple[float, float, int]]:
        """Generate a human-like Bezier curve trajectory with variable timing."""
        steps = random.randint(30, 55)

        # Random control points for natural arc
        cp1 = (
            start[0] + (end[0] - start[0]) * random.uniform(0.15, 0.35),
            start[1] + random.uniform(-18, 18),
        )
        cp2 = (
            start[0] + (end[0] - start[0]) * random.uniform(0.65, 0.85),
            start[1] + random.uniform(-12, 12),
        )

        points = []
        for i in range(steps + 1):
            t = i / steps

            # Cubic Bezier
            x = (
                (1 - t) ** 3 * start[0]
                + 3 * (1 - t) ** 2 * t * cp1[0]
                + 3 * (1 - t) * t ** 2 * cp2[0]
                + t ** 3 * end[0]
            )
            y = (
                (1 - t) ** 3 * start[1]
                + 3 * (1 - t) ** 2 * t * cp1[1]
                + 3 * (1 - t) * t ** 2 * cp2[1]
                + t ** 3 * end[1]
            )

            # Micro-jitter (human hand tremor)
            x += random.gauss(0, 0.8)
            y += random.gauss(0, 0.8)

            # Variable timing: slow start → fast middle → slow end
            if i == 0:
                delay = random.randint(30, 80)
            elif t < 0.3:
                delay = random.randint(8, 20)
            elif t < 0.7:
                delay = random.randint(3, 10)
            elif t < 0.95:
                delay = random.randint(8, 25)
            else:
                delay = random.randint(20, 60)

            points.append((x, y, delay))

        # 60% chance of overshoot + correction
        if random.random() < 0.6:
            overshoot_x = end[0] + random.uniform(2, 8)
            points.insert(
                -1,
                (overshoot_x, end[1] + random.gauss(0, 1), random.randint(15, 40)),
            )
            points.append(
                (end[0] + random.gauss(0, 0.5), end[1] + random.gauss(0, 0.5), random.randint(10, 30))
            )

        return points

    # ──────────────────────────────────────────────
    # Result Checking
    # ──────────────────────────────────────────────

    def _check_solved(self, page: Page) -> bool:
        """Check if the CAPTCHA was solved successfully."""
        for sel in CAPTCHA_SUCCESS_SELECTORS:
            with suppress(Exception):
                if page.locator(sel).first.is_visible(timeout=2000):
                    return True

        if not self.is_captcha_present(page):
            return True

        for text in ["验证成功", "通过验证", "success"]:
            with suppress(Exception):
                if page.locator(f"text={text}").first.is_visible(timeout=1000):
                    return True

        return False

    def _refresh_captcha(self, page: Page) -> None:
        """Click the refresh button to get a new CAPTCHA."""
        for sel in CAPTCHA_REFRESH_SELECTORS:
            with suppress(Exception):
                btn = page.locator(sel).first
                if btn.is_visible(timeout=1000):
                    btn.click()
                    print("[slider] Refreshed CAPTCHA", file=sys.stderr)
                    return
