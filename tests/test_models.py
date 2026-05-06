from __future__ import annotations

from scripts.models import MatchedItem, StepRecord, WorkflowResult


class TestStepRecord:
    def test_create(self):
        step = StepRecord(name="search", status="success", message="done", artifact="/tmp/img.png", details={"keyword": "test"})
        assert step.name == "search"
        assert step.status == "success"
        assert step.message == "done"
        assert step.artifact == "/tmp/img.png"
        assert step.details == {"keyword": "test"}

    def test_defaults(self):
        step = StepRecord(name="step1", status="pending")
        assert step.message == ""
        assert step.artifact is None
        assert step.details == {}


class TestWorkflowResult:
    def test_initial_status(self):
        result = WorkflowResult(task_id="task-1")
        assert result.task_id == "task-1"
        assert result.status == "failed"
        assert result.matched_items == []
        assert result.evidence == []
        assert result.steps == []
        assert result.error is None

    def test_add_step(self):
        result = WorkflowResult(task_id="task-1")
        result.add_step("login_check", "success", message="已登录")
        assert len(result.steps) == 1
        assert result.steps[0].name == "login_check"
        assert result.steps[0].status == "success"
        assert result.steps[0].message == "已登录"

    def test_add_step_with_details(self):
        result = WorkflowResult(task_id="task-1")
        result.add_step("search", "success", keyword="test", candidates=5)
        assert result.steps[0].details == {"keyword": "test", "candidates": 5}


class TestMatchedItem:
    def test_defaults(self):
        item = MatchedItem(title="Test")
        assert item.title == "Test"
        assert item.item_id is None
        assert item.price is None
        assert item.price_value is None
        assert item.sales_count is None
        assert item.rating is None
        assert item.free_shipping is False
        assert item.is_tmall is False
        assert item.url is None
        assert item.cart_added is False
