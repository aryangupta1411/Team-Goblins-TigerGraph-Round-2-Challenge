"""agent/llm.py — the LLM backend seam (LLM_BACKEND = cli | api | mock).

No network and no CLI subprocess: these tests cover selection, the usage shim that feeds
`RunLog.record_llm`, and the fact that the cli backend wraps the *same* typed tool objects the
api backend runs, so a graph call is recorded identically whichever backend is in play.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agent import llm  # noqa: E402
from agent.config import Settings  # noqa: E402
from agent.runlog import RunLog  # noqa: E402


def test_resolve_backend_defaults_to_cli_and_to_mock_under_run_mode_mock():
    assert llm.resolve_backend(Settings(run_mode="live", llm_backend="", gemini_api_key="", model="claude-sonnet-5")) == "cli"
    assert llm.resolve_backend(Settings(run_mode="mock", llm_backend="", gemini_api_key="", model="claude-sonnet-5")) == "mock"
    # explicit wins both ways: a live model over mock (DuckDB) facts, or the API SDK
    assert llm.resolve_backend(Settings(run_mode="mock", llm_backend="cli")) == "cli"
    assert llm.resolve_backend(Settings(run_mode="live", llm_backend="API")) == "api"
    assert llm.resolve_backend(Settings(run_mode="live", llm_backend="mock")) == "mock"
    assert llm.resolve_backend(Settings(run_mode="live", llm_backend="gemini")) == "gemini"
    assert llm.resolve_backend(Settings(run_mode="mock", llm_backend="google")) == "gemini"
    assert llm.resolve_backend(Settings(run_mode="live", llm_backend="", gemini_api_key="g-key", model="claude-sonnet-5")) == "gemini"
    assert llm.resolve_backend(Settings(run_mode="live", llm_backend="", gemini_api_key="", model="gemini-2.5-flash")) == "gemini"


def test_make_backend_mock_needs_no_api_key():
    b = llm.make_backend(Settings(run_mode="mock", llm_backend="mock", anthropic_api_key=""))
    assert isinstance(b, llm.ApiBackend) and b.name == "mock"
    assert type(b.client).__name__ == "FakeAnthropic"


def test_make_backend_cli_is_the_default_and_builds_no_anthropic_client():
    b = llm.make_backend(Settings(run_mode="live", llm_backend="", anthropic_api_key="", gemini_api_key="", model="claude-sonnet-5"))
    assert isinstance(b, llm.CliBackend) and b.name == "cli"
    assert not hasattr(b, "client")


def test_make_backend_gemini():
    b = llm.make_backend(Settings(run_mode="live", llm_backend="gemini", gemini_api_key="test-key"))
    assert isinstance(b, llm.GeminiBackend) and b.name == "gemini"
    assert llm._gemini_model("claude-sonnet-5") == "gemini-2.5-flash"
    assert llm._gemini_model("models/gemini-2.5-pro") == "gemini-2.5-pro"


def test_wrap_client_passes_a_backend_through_and_adapts_a_raw_client():
    b = llm.CliBackend(Settings())
    assert llm.wrap_client(b) is b
    raw = object()
    assert isinstance(llm.wrap_client(raw, Settings(run_mode="mock")), llm.ApiBackend)


def test_usage_shim_feeds_runlog_record_llm(tmp_path):
    class R:
        usage = {"input_tokens": 7, "output_tokens": 11, "cache_read_input_tokens": 100,
                 "cache_creation_input_tokens": 5}
        session_id = "s-1"
        stop_reason = "end_turn"

    log = RunLog("r", "HHG-014", tmp_path / "r" / "HHG-014")
    log.record_llm("P4", llm._usage_response("P4", R(), "claude-sonnet-5"))
    assert log.tokens == 7 + 11 + 100 + 5
    assert log.cache_read_tokens == 100 and log.llm_calls == 1


def test_usage_shim_records_zero_when_the_cli_reports_nothing(tmp_path):
    class R:
        usage = None
        session_id = ""
        stop_reason = None

    log = RunLog("r", "HHG-014", tmp_path / "r2" / "HHG-014")
    log.record_llm("P9", llm._usage_response("P9", R(), "m"))
    assert log.tokens == 0


def test_sdk_tool_wraps_the_same_typed_tool_body():
    from anthropic.lib.tools import BetaAsyncFunctionTool

    async def region_history(c: str) -> str:
        return '{"rows": []}'

    t = BetaAsyncFunctionTool(region_history, name="region_history", description="d",
                              input_schema={"type": "object", "properties": {"c": {"type": "string"}},
                                            "required": ["c"], "additionalProperties": False})
    wrapped = llm._sdk_tool(t)
    assert wrapped.name == "region_history"
    out = asyncio.run(wrapped.handler({"c": "C1"}))
    assert out["content"][0]["text"] == '{"rows": []}'


def test_sdk_tool_turns_a_failure_into_a_tool_result_instead_of_killing_the_loop():
    from anthropic.lib.tools import BetaAsyncFunctionTool

    async def ring_profile(c: str) -> str:
        raise RuntimeError("boom")

    t = BetaAsyncFunctionTool(ring_profile, name="ring_profile", description="d",
                              input_schema={"type": "object", "properties": {"c": {"type": "string"}},
                                            "required": ["c"], "additionalProperties": False})
    out = asyncio.run(llm._sdk_tool(t).handler({"c": "C1"}))
    assert "ring_profile failed" in out["content"][0]["text"]


def test_gemini_schema_inlines_defs_and_strips_additional_properties():
    from agent.schemas import LLM_SCHEMAS

    for s in LLM_SCHEMAS:
        cleaned = llm._gemini_schema(s.model_json_schema())
        text = str(cleaned)
        assert "$defs" not in text and "$ref" not in text and "additionalProperties" not in text


def test_gemini_parse_and_investigate_loop(tmp_path):
    from anthropic.lib.tools import BetaAsyncFunctionTool

    from agent.schemas import Closing

    seen_payloads: list[dict] = []

    async def fake_transport(model: str, payload: dict) -> dict:
        seen_payloads.append(payload)
        if "tools" in payload:
            # First turn: call region_history; second turn: return final text
            last_parts = payload["contents"][-1]["parts"]
            if not any("functionResponse" in p for p in last_parts):
                return {
                    "candidates": [{
                        "finishReason": "STOP",
                        "content": {
                            "role": "model",
                            "parts": [
                                {"thought": True, "text": "checking region"},
                                {"functionCall": {"name": "region_history", "args": {"c": "C1"}}, "thoughtSignature": "sig-123"},
                            ],
                        },
                    }],
                    "usageMetadata": {"promptTokenCount": 50, "candidatesTokenCount": 10, "cachedContentTokenCount": 20},
                }
            return {
                "candidates": [{"finishReason": "STOP", "content": {"role": "model", "parts": [{"text": "Done investigating."}]}}],
                "usageMetadata": {"promptTokenCount": 60, "candidatesTokenCount": 5},
            }
        return {
            "candidates": [{
                "finishReason": "STOP",
                "content": {
                    "role": "model",
                    "parts": [{"text": '{"summary": "Two sentences. Verdict is fraud.", "stop_reason": "§6 test 1", "similar_prior_cases_used": ["CC-0001"]}'}],
                },
            }],
            "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 25, "thoughtsTokenCount": 15},
        }

    called_with: list[str] = []

    async def region_history(c: str) -> str:
        called_with.append(c)
        return '{"hint": "home"}'

    t = BetaAsyncFunctionTool(region_history, name="region_history", description="d",
                              input_schema={"type": "object", "properties": {"c": {"type": "string"}},
                                            "required": ["c"], "additionalProperties": False})
    log = RunLog("r", "HHG-014", tmp_path / "r-gem" / "HHG-014")
    backend = llm.GeminiBackend(Settings(llm_backend="gemini", gemini_api_key="k"), transport=fake_transport)
    history: list[dict] = []
    system = [{"type": "text", "text": "system prompt"}]

    asyncio.run(backend.investigate(
        phase="P2", user_text="investigate", history=history, system=system,
        tools=[t], model="gemini-2.5-flash", log=log, max_iterations=4,
    ))
    assert called_with == ["C1"]
    # Verify thoughtSignature was preserved in the second turn sent to Gemini
    second_turn_model_parts = seen_payloads[1]["contents"][1]["parts"]
    assert any(p.get("thoughtSignature") == "sig-123" for p in second_turn_model_parts)

    parsed = asyncio.run(backend.parse(
        phase="P9", schema=Closing, user_text="close", history=history,
        system=system, tool_defs=[], model="gemini-2.5-flash", log=log,
    ))
    assert isinstance(parsed, Closing) and parsed.similar_prior_cases_used == ["CC-0001"]
    assert log.llm_calls == 3
    assert log.tokens == (30 + 20 + 10) + (60 + 5) + (100 + 25 + 15)
