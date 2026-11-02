"""Golden-task / trajectory evaluation stub tests (mock LLM)."""

from __future__ import annotations

from pathlib import Path

import pytest

from proagent_os.db import get_db, new_id, utcnow
from proagent_os.eval import (
    EvalResult,
    GoldenTask,
    compare_trajectory,
    load_golden_tasks,
    run_golden_task,
)
from proagent_os.eval.golden import _attach_judge, maybe_record_eval_metering
from proagent_os.metering import usage_report
from proagent_os.eval.judge import (
    JudgeParseError,
    JudgeVerdict,
    LlmJudge,
    judge_for_settings,
    parse_judge_output,
)
from proagent_os.llm.base import LLMResponse
from proagent_os.llm.mock import (
    MOCK_JUDGE_FAIL_RATIONALE,
    MOCK_JUDGE_PASS_RATIONALE,
    MockLLMProvider,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "eval"
ORCH_FIXTURE_IDS = ("conversion_diagnosis", "research_kb_clock")


def test_compare_trajectory_ordered_subset_pass():
    result = compare_trajectory(
        ["kb_search", "clock", "emit_recommendation"],
        ["kb_search", "clock"],
        task_id="t1",
    )
    assert result.passed
    assert result.match == "ordered_subset"
    assert result.judge_mode == "ordered_subset"
    assert result.trajectory_passed is True
    assert result.judge_rationale == ""
    assert result.judge_fallback == ""
    assert result.diffs == []
    assert result.actual_tools[-1] == "emit_recommendation"


def test_compare_trajectory_ordered_subset_allows_gaps():
    result = compare_trajectory(
        ["http_fetch", "kb_search", "file_read", "clock"],
        ["kb_search", "clock"],
        task_id="gaps",
    )
    assert result.passed


def test_compare_trajectory_ordered_subset_fail_wrong_order():
    result = compare_trajectory(
        ["clock", "kb_search"],
        ["kb_search", "clock"],
        task_id="order",
    )
    assert not result.passed
    assert result.diffs
    assert "clock" in result.diffs[0] or "missing" in result.diffs[0]


def test_compare_trajectory_ordered_subset_fail_missing():
    result = compare_trajectory(
        ["kb_search"],
        ["kb_search", "clock"],
        task_id="missing",
    )
    assert not result.passed
    assert any("clock" in d for d in result.diffs)


def test_compare_rejects_unknown_match_mode():
    result = compare_trajectory(["a"], ["a"], match="exact", task_id="x")
    assert not result.passed
    assert "unsupported" in result.diffs[0]


def test_load_golden_tasks_from_fixtures():
    tasks = load_golden_tasks(FIXTURES)
    ids = {t.id for t in tasks}
    assert "conversion_diagnosis" in ids
    assert "research_kb_clock" in ids
    assert "synthetic_trajectory_only" in ids
    conv = next(t for t in tasks if t.id == "conversion_diagnosis")
    assert conv.match == "ordered_subset"
    assert conv.expected_tools == ["kb_search", "clock", "emit_recommendation"]
    assert conv.seed_kb


def test_golden_task_from_mapping_requires_id():
    with pytest.raises(ValueError, match="missing id"):
        GoldenTask.from_mapping({"task": "x", "expected_tools": []})


def _insert_tenant(name: str) -> tuple[str, str]:
    tid = new_id("ten")
    api_key = new_id("pk")
    get_db().insert(
        "tenants",
        {
            "id": tid,
            "name": name,
            "api_key": api_key,
            "goals": "",
            "constraints": "",
            "tone": "professional",
            "created_at": utcnow(),
            "key_status": "active",
        },
    )
    return tid, api_key


def _eval_run_rows(tenant_id: str) -> list[dict]:
    return get_db().fetchall(
        """
        SELECT kind, quantity FROM usage_events
        WHERE tenant_id=? AND kind='eval_run'
        ORDER BY rowid
        """,
        (tenant_id,),
    )


def _eval_total(report: dict) -> dict | None:
    for row in report["totals"]:
        if row["kind"] == "eval_run":
            return row
    return None


def test_maybe_record_eval_metering_writes_case_count():
    """Quantity is the number of cases in the finished call, scoped by tenant."""
    assert maybe_record_eval_metering(None, task_id="t", passed=True) is None
    assert maybe_record_eval_metering("", task_id="t", passed=False) is None

    owner, _owner_key = _insert_tenant("Eval Meter")
    other, _other_key = _insert_tenant("Eval Other")
    with pytest.raises(ValueError, match="cases"):
        maybe_record_eval_metering(owner, task_id="t", passed=True, cases=0)

    one = maybe_record_eval_metering(owner, task_id="case-a", passed=True, cases=1)
    assert one is not None
    assert one["recorded"] is True
    assert one["kind"] == "eval_run"
    assert one["quantity"] == 1
    assert one["billing"] == "stub"
    assert one["tenant_id"] == owner
    assert one["task_id"] == "case-a"
    assert one["passed"] is True
    assert one["event_id"]

    # One finish covering three cases stores 3, not a flat 1 for the batch.
    batch = maybe_record_eval_metering(owner, task_id="suite", passed=False, cases=3)
    assert batch is not None
    assert batch["quantity"] == 3
    assert batch["passed"] is False

    other_row = maybe_record_eval_metering(other, task_id="case-b", passed=True)
    assert other_row is not None
    assert other_row["quantity"] == 1

    assert _eval_run_rows(owner) == [
        {"kind": "eval_run", "quantity": 1},
        {"kind": "eval_run", "quantity": 3},
    ]
    assert _eval_run_rows(other) == [{"kind": "eval_run", "quantity": 1}]

    owner_report = usage_report(owner)
    assert owner_report["tenant_id"] == owner
    assert owner_report["billing"] == "stub"
    assert _eval_total(owner_report) == {"kind": "eval_run", "events": 2, "quantity": 4}
    assert _eval_total(usage_report(other)) == {"kind": "eval_run", "events": 1, "quantity": 1}

    everyone = usage_report(None)
    quantities = {
        (row["tenant_id"], row["kind"]): row["quantity"] for row in everyone["totals"]
    }
    assert quantities[(owner, "eval_run")] == 4
    assert quantities[(other, "eval_run")] == 1


def test_eval_result_to_dict():
    r = EvalResult(
        task_id="t",
        passed=True,
        match="ordered_subset",
        expected_tools=["a"],
        actual_tools=["a"],
    )
    d = r.to_dict()
    assert d["passed"] is True
    assert d["expected_tools"] == ["a"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture_id", ORCH_FIXTURE_IDS)
async def test_run_golden_fixtures_with_mock_orchestrator(fixture_id: str):
    tasks = {t.id: t for t in load_golden_tasks(FIXTURES)}
    golden = tasks[fixture_id]
    result = await run_golden_task(golden)
    assert isinstance(result, EvalResult)
    assert result.task_id == fixture_id
    assert result.match == "ordered_subset"
    assert result.judge_mode == "ordered_subset"
    assert result.trajectory_passed is True
    assert result.judge_rationale == ""
    assert result.judge_fallback == ""
    assert result.final_answer
    assert result.passed, f"diffs={result.diffs} actual={result.actual_tools}"
    assert result.summary.get("orchestrator_status") == "completed"
    assert result.summary.get("eval") == "stub"
    assert result.metering is not None
    assert result.metering["recorded"] is True
    assert result.metering["kind"] == "eval_run"
    assert result.metering["quantity"] == 1
    assert result.metering["billing"] == "stub"
    report = result.to_dict()
    assert report["judge_mode"] == "ordered_subset"
    assert report["passed"] is True


def _use_llm_judge(monkeypatch) -> None:
    monkeypatch.setenv("PROAGENT_EVAL_JUDGE", "llm")
    monkeypatch.setenv("PROAGENT_LLM_PROVIDER", "mock")
    from proagent_os.config import get_settings
    from proagent_os.llm.factory import reset_llm_cache

    get_settings.cache_clear()
    reset_llm_cache()


def test_eval_judge_setting_defaults_to_ordered_subset(monkeypatch):
    monkeypatch.delenv("PROAGENT_EVAL_JUDGE", raising=False)
    from proagent_os.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    assert settings.eval_judge == "ordered_subset"
    assert judge_for_settings(settings) is None

    monkeypatch.setenv("PROAGENT_EVAL_JUDGE", " LLM ")
    get_settings.cache_clear()
    chosen = judge_for_settings(get_settings())
    assert isinstance(chosen, LlmJudge)


def test_parse_judge_output_accepts_fenced_json():
    verdict = parse_judge_output(
        '```json\n{"passed": false, "rationale": "Tools were out of order."}\n```'
    )
    assert verdict == JudgeVerdict(passed=False, rationale="Tools were out of order.")
    prose = parse_judge_output('Note:\n{"passed": true, "rationale": "Looks right."}\nThanks')
    assert prose.passed is True


def test_parse_judge_output_rejects_unstructured_text():
    with pytest.raises(JudgeParseError, match="unparseable"):
        parse_judge_output("I think this passed.")
    with pytest.raises(JudgeParseError, match="boolean"):
        parse_judge_output('{"passed": "true", "rationale": "coerced"}')
    with pytest.raises(JudgeParseError, match="rationale"):
        parse_judge_output('{"passed": true}')
    with pytest.raises(JudgeParseError, match="empty"):
        parse_judge_output("  ")


class _RecordingProvider:
    def __init__(self, inner):
        self.inner = inner
        self.kwargs = None
        self.messages = None

    async def chat(self, messages, **kwargs):
        self.messages = messages
        self.kwargs = kwargs
        return await self.inner.chat(messages, **kwargs)


@pytest.mark.asyncio
async def test_mock_judge_pass_and_fail_without_tools():
    provider = _RecordingProvider(MockLLMProvider())
    judge = LlmJudge(provider=provider)
    passed = await judge.judge(
        task="Why is conversion low?",
        expected_tools=["kb_search", "clock"],
        actual_tools=["http_fetch", "kb_search", "clock"],
        final_answer="Activation is the lever.",
    )
    assert passed.passed is True
    assert passed.rationale == MOCK_JUDGE_PASS_RATIONALE
    assert provider.kwargs["tools"] is None
    assert provider.kwargs["tool_choice"] is None
    assert provider.kwargs["temperature"] == 0.0
    assert provider.messages[0].role == "system"
    assert "you are an evaluation judge" in (provider.messages[0].content or "").lower()

    failed = await judge.judge(
        task="Why is conversion low?",
        expected_tools=["kb_search", "clock"],
        actual_tools=["clock", "kb_search"],
        final_answer="Activation is the lever.",
    )
    assert failed.passed is False
    assert failed.rationale == MOCK_JUDGE_FAIL_RATIONALE


@pytest.mark.asyncio
async def test_mock_judge_fails_empty_answer_when_trajectory_matches():
    result = compare_trajectory(
        ["kb_search", "clock"],
        ["kb_search", "clock"],
        task_id="empty-answer",
    )
    assert result.passed is True
    judged = await _attach_judge(
        result,
        task="Say something",
        final_answer="   ",
        judge=LlmJudge(provider=MockLLMProvider()),
    )
    assert judged.trajectory_passed is True
    assert judged.passed is False
    assert judged.judge_mode == "llm"
    assert judged.judge_rationale == MOCK_JUDGE_FAIL_RATIONALE
    assert judged.judge_fallback == ""
    assert judged.diffs == []
    assert judged.to_dict()["judge_mode"] == "llm"


@pytest.mark.asyncio
async def test_run_golden_llm_judge_mock_passes_fixture(monkeypatch):
    _use_llm_judge(monkeypatch)
    tasks = {t.id: t for t in load_golden_tasks(FIXTURES)}
    result = await run_golden_task(tasks["research_kb_clock"])
    assert result.judge_mode == "llm"
    assert result.trajectory_passed is True
    assert result.passed is True
    assert result.judge_rationale == MOCK_JUDGE_PASS_RATIONALE
    assert result.judge_fallback == ""
    assert result.match == "ordered_subset"
    assert result.final_answer
    assert result.metering is not None
    assert result.metering["passed"] is True


@pytest.mark.asyncio
async def test_run_golden_llm_judge_mock_fails_missing_tool(monkeypatch):
    _use_llm_judge(monkeypatch)
    golden = GoldenTask(
        id="judge_missing_tool",
        task="Why is conversion low and what should we do?",
        expected_tools=["not_a_real_tool"],
        match="ordered_subset",
    )
    result = await run_golden_task(golden)
    assert result.summary.get("orchestrator_status") == "completed"
    assert result.trajectory_passed is False
    assert result.judge_mode == "llm"
    assert result.passed is False
    assert result.judge_rationale == MOCK_JUDGE_FAIL_RATIONALE
    assert any("not_a_real_tool" in diff for diff in result.diffs)
    assert result.metering["passed"] is False


class _BoomProvider:
    async def chat(self, messages, **kwargs):
        raise RuntimeError("judge model unavailable")


class _GarbageProvider:
    async def chat(self, messages, **kwargs):
        return LLMResponse(content="Looks fine to me, no JSON.", model="garbage")


@pytest.mark.asyncio
async def test_run_golden_judge_falls_back_when_provider_raises():
    tasks = {t.id: t for t in load_golden_tasks(FIXTURES)}
    result = await run_golden_task(
        tasks["research_kb_clock"],
        judge=LlmJudge(provider=_BoomProvider()),
    )
    assert result.trajectory_passed is True
    assert result.passed is True
    assert result.judge_mode == "ordered_subset_fallback"
    assert result.judge_rationale == ""
    assert "RuntimeError" in result.judge_fallback
    assert "judge model unavailable" in result.judge_fallback
    report = result.to_dict()
    assert report["judge_mode"] == "ordered_subset_fallback"
    assert report["passed"] is True


@pytest.mark.asyncio
async def test_run_golden_judge_falls_back_when_output_unparseable():
    tasks = {t.id: t for t in load_golden_tasks(FIXTURES)}
    result = await run_golden_task(
        tasks["conversion_diagnosis"],
        judge=LlmJudge(provider=_GarbageProvider()),
    )
    assert result.trajectory_passed is True
    assert result.passed is True
    assert result.judge_mode == "ordered_subset_fallback"
    assert result.judge_rationale == ""
    assert "unparseable judge output" in result.judge_fallback
    assert result.diffs == []


@pytest.mark.asyncio
async def test_judge_fallback_keeps_trajectory_failure():
    """A broken judge must not turn a trajectory miss into a pass."""
    result = compare_trajectory(["clock"], ["kb_search"], task_id="keep-fail")
    assert result.passed is False
    judged = await _attach_judge(
        result,
        task="Find the policy",
        final_answer="unused",
        judge=LlmJudge(provider=_BoomProvider()),
    )
    assert judged.trajectory_passed is False
    assert judged.passed is False
    assert judged.judge_mode == "ordered_subset_fallback"
    assert "RuntimeError" in judged.judge_fallback


@pytest.mark.asyncio
async def test_run_golden_task_records_eval_run_on_usage_api():
    """The CI helper writes eval_run; GET /api/v1/usage sums cases per tenant."""
    from fastapi.testclient import TestClient

    from proagent_os.main import create_app

    owner, owner_key = _insert_tenant("Golden Owner")
    other, other_key = _insert_tenant("Golden Other")
    golden = next(t for t in load_golden_tasks(FIXTURES) if t.id == "research_kb_clock")

    skipped = await run_golden_task(golden, tenant_id=owner, record_metering=False)
    assert skipped.passed
    assert skipped.metering is None
    assert _eval_run_rows(owner) == []

    result = await run_golden_task(golden, tenant_id=owner)
    assert result.passed
    assert result.metering is not None
    assert result.metering["recorded"] is True
    assert result.metering["kind"] == "eval_run"
    assert result.metering["quantity"] == 1
    assert result.metering["tenant_id"] == owner
    assert result.metering["task_id"] == "research_kb_clock"
    assert result.metering["billing"] == "stub"
    assert _eval_run_rows(owner) == [{"kind": "eval_run", "quantity": 1}]
    assert _eval_total(usage_report(owner)) == {
        "kind": "eval_run",
        "events": 1,
        "quantity": 1,
    }
    assert _eval_total(usage_report(other)) is None

    again = await run_golden_task(golden, tenant_id=owner)
    assert again.metering["quantity"] == 1
    assert _eval_total(usage_report(owner)) == {
        "kind": "eval_run",
        "events": 2,
        "quantity": 2,
    }
    assert _eval_run_rows(other) == []

    with TestClient(create_app()) as client:
        own = client.get("/api/v1/usage", headers={"Authorization": f"Bearer {owner_key}"})
        assert own.status_code == 200, own.text
        body = own.json()
        assert body["billing"] == "stub"
        assert body["payment"] is None
        assert body["tenant_id"] == owner
        assert {"kind": "eval_run", "events": 2, "quantity": 2} in body["totals"]

        other_body = client.get(
            "/api/v1/usage", headers={"Authorization": f"Bearer {other_key}"}
        )
        assert other_body.status_code == 200, other_body.text
        assert other_body.json()["tenant_id"] == other
        assert all(row["kind"] != "eval_run" for row in other_body.json()["totals"])

        forbidden = client.get(
            "/api/v1/usage",
            headers={"Authorization": f"Bearer {other_key}"},
            params={"tenant_id": owner},
        )
        assert forbidden.status_code == 403

        admin = client.get(
            "/api/v1/usage",
            headers={"Authorization": "Bearer test-admin-key"},
            params={"tenant_id": owner},
        )
        assert admin.status_code == 200, admin.text
        assert admin.json()["tenant_id"] == owner
        assert {"kind": "eval_run", "events": 2, "quantity": 2} in admin.json()["totals"]
