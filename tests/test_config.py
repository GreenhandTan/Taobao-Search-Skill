from __future__ import annotations

import pytest

from scripts.config import OpenClawSkillConfig, _optional_bool, _optional_float, _optional_int, _optional_str


class TestOptionalCoercions:
    def test_optional_str_none(self):
        assert _optional_str(None) is None

    def test_optional_str_value(self):
        assert _optional_str("  hello  ") == "hello"

    def test_optional_str_empty(self):
        assert _optional_str("   ") is None

    def test_optional_float_none(self):
        assert _optional_float(None) is None

    def test_optional_float_value(self):
        assert _optional_float("3.14") == 3.14
        assert _optional_float(42) == 42.0

    def test_optional_float_invalid(self):
        assert _optional_float("abc") is None

    def test_optional_int_none(self):
        assert _optional_int(None) is None

    def test_optional_int_value(self):
        assert _optional_int("42") == 42
        assert _optional_int(99) == 99

    def test_optional_int_invalid(self):
        assert _optional_int("abc") is None

    def test_optional_bool_none(self):
        assert _optional_bool(None) is None

    def test_optional_bool_bool_values(self):
        assert _optional_bool(True) is True
        assert _optional_bool(False) is False

    def test_optional_bool_strings(self):
        assert _optional_bool("true") is True
        assert _optional_bool("True") is True
        assert _optional_bool("1") is True
        assert _optional_bool("yes") is True
        assert _optional_bool("false") is False
        assert _optional_bool("False") is False
        assert _optional_bool("0") is False
        assert _optional_bool("no") is False

    def test_optional_bool_invalid_string(self):
        assert _optional_bool("maybe") is None


class TestConfigFromPayload:
    def test_empty_payload(self):
        config = OpenClawSkillConfig.from_payload({})
        assert config.search_keyword == "Sony headphones"
        assert config.rating_threshold == 0.0
        assert config.max_candidates == 5
        assert config.need_screenshot is True
        assert config.manual_approval_required is True
        assert config.report_channel == "feishu"
        assert config.browser_name == "chromium"
        assert config.session_strategy == "storage_state"
        assert config.session_auto_save is True
        assert config.price_min is None
        assert config.price_max is None
        assert config.min_sales is None
        assert config.require_free_shipping is False
        assert config.require_tmall is None
        assert config.sku_keywords is None

    def test_full_payload(self):
        payload = {
            "task_id": "task-123",
            "feishu_message_id": "msg-456",
            "search_keyword": "苹果手机",
            "rating_threshold": 0.95,
            "max_candidates": 10,
            "need_screenshot": False,
            "manual_approval_required": False,
            "report_channel": "feishu",
            "session_state_path": "/custom/path.json",
            "session_strategy": "cookie_localstorage",
            "session_auto_save": False,
            "price_min": 100.0,
            "price_max": 5000.0,
            "min_sales": 200,
            "require_free_shipping": True,
            "require_tmall": True,
            "sku_keywords": "16G 512G",
            "constraints": {
                "browser": "chromium",
                "headless": True,
            },
        }
        config = OpenClawSkillConfig.from_payload(payload)
        assert config.task_id == "task-123"
        assert config.search_keyword == "苹果手机"
        assert config.rating_threshold == 0.95
        assert config.max_candidates == 10
        assert config.need_screenshot is False
        assert config.manual_approval_required is False
        assert config.session_strategy == "cookie_localstorage"
        assert config.session_auto_save is False
        assert config.price_min == 100.0
        assert config.price_max == 5000.0
        assert config.min_sales == 200
        assert config.require_free_shipping is True
        assert config.require_tmall is True
        assert config.sku_keywords == "16G 512G"

    def test_require_tmall_string_conversion(self):
        assert OpenClawSkillConfig.from_payload({"require_tmall": "yes"}).require_tmall is True
        assert OpenClawSkillConfig.from_payload({"require_tmall": "1"}).require_tmall is True
        assert OpenClawSkillConfig.from_payload({"require_tmall": "no"}).require_tmall is False
        assert OpenClawSkillConfig.from_payload({"require_tmall": "0"}).require_tmall is False
        assert OpenClawSkillConfig.from_payload({"require_tmall": "maybe"}).require_tmall is None

    def test_price_min_max_coercion(self):
        config = OpenClawSkillConfig.from_payload({"price_min": "100.5", "price_max": "999.9"})
        assert config.price_min == 100.5
        assert config.price_max == 999.9
