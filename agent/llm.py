"""LLM backend seam — the phase machine asks for two things and does not care who answers.

    parse(phase, schema, user_text, ...)   -> a validated Pydantic object   (P4/P5/P7/P8/P9)
    investigate(user_text, tools, ...)     -> runs the typed-tool loop      (P2)

Four backends, chosen by `LLM_BACKEND` (contracts §D):

  gemini  Google Gemini API (`generativelanguage.googleapis.com/v1beta`), authenticated via
          `GEMINI_API_KEY` (or `GOOGLE_API_KEY`). Uses `responseMimeType="application/json"` with
          `responseSchema` for structured output phases and `functionDeclarations` for the P2
          typed-tool loop, preserving `thoughtSignature` across multi-turn tool calls.
  cli     `claude-agent-sdk` (default when no Gemini key/model is set): drives the bundled Claude
          Code CLI, which authenticates with the user's Claude subscription — **no ANTHROPIC_API_KEY**.
          Structured phases use the Agent SDK's JSON-schema output (`output_format={"type": "json_schema", ...}`
          -> `ResultMessage.structured_output`); P2 uses an in-process SDK MCP server that wraps the
          very same typed query tools (`agent/mcp_client.make_query_tools`,
          `agent/tools_local.make_local_tools`), so every graph call still goes through
          `call_query_tool` -> `run_query` -> `RunLog.record_tool` with unchanged row shapes and the
          harness keeps all writer calls to itself.
  api     the Anthropic Python SDK 1.7.0 path this module was carved out of, moved verbatim:
          `client.beta.messages.parse` and `client.beta.messages.tool_runner`. Needs ANTHROPIC_API_KEY.
  mock    the `api` code driven by `agent/fake_anthropic.FakeAnthropic` — RUN_MODE=mock, no keys,
          no network (this is what `--mock` has always done).

Isolation of the cli backend (so a run is reproducible and never picks up this machine's setup):
  setting_sources=[]   no ~/.claude settings, no CLAUDE.md, no project settings
  tools=[]             every built-in tool (Read/Write/Bash/Glob/Grep/WebSearch/...) is off
  allowed_tools        exactly our `mcp__hhgoa__<query>` wrappers
  can_use_tool         a second gate that denies anything else and notes it in the run log
  cwd                  the repo root; the agent never reads from it (no file tools are served)

One CLI session per case: the first call creates it, later phases `resume` it, so the P0–P10
conversation stays append-only and the CLI's own prompt cache is reused across phases.
"""
from __future__ import annotations

import asyncio
import json
import time
import warnings
from types import SimpleNamespace
from typing import Any

from agent.config import DEFAULT_GEMINI_MODEL, SETTINGS, Settings

MCP_SERVER_NAME = "hhgoa"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# A tool named in `allowed_tools` is auto-approved before `can_use_tool` runs; the SDK warns about
# that on every call. That is exactly the wiring we want (allowlist approves our typed queries, the
# callback denies everything else), so the advisory is silenced rather than answered.
try:                                                     # pragma: no cover - SDK version guard
    from claude_agent_sdk import CanUseToolShadowedWarning as _Shadowed

    warnings.filterwarnings("ignore", category=_Shadowed)
except Exception:                                        # pragma: no cover
    pass

# A structured phase needs one turn to think and one to emit the StructuredOutput call; a tool call
# the model slips in (it has none served on these calls) must not cost it the answer.
PARSE_MAX_TURNS = 6


def resolve_backend(settings: Settings = SETTINGS) -> str:
    """`LLM_BACKEND` = gemini | cli | api | mock; unset (or "auto") means mock in RUN_MODE=mock,
    gemini when GEMINI_API_KEY / a gemini-* model is set, else cli."""
    name = (settings.llm_backend or "").strip().lower()
    if name in ("gemini", "google"):
        return "gemini"
    if name in ("cli", "api", "mock"):
        return name
    if settings.mock:
        return "mock"
    if settings.gemini_api_key or (settings.model or "").lower().startswith("gemini"):
        return "gemini"
    return "cli"


def make_backend(settings: Settings = SETTINGS, client: Any = None) -> Any:
    """The backend `agent.bench` hands to `PhaseMachine`. `client` overrides the api/mock/gemini client."""
    name = resolve_backend(settings)
    if name == "gemini":
        return GeminiBackend(settings, transport=client)
    if name == "cli":
        return CliBackend(settings)
    if client is None:
        if name == "mock":
            from agent.fake_anthropic import FakeAnthropic

            client = FakeAnthropic()
        else:
            import anthropic

            client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key or None, max_retries=4, timeout=600.0)
    return ApiBackend(client, settings, name=name)


def wrap_client(client: Any, settings: Settings = SETTINGS) -> Any:
    """Accept either a backend or a bare Anthropic-style client (back-compat for PhaseMachine)."""
    if hasattr(client, "parse") and hasattr(client, "investigate"):
        return client
    return ApiBackend(client, settings, name="mock" if settings.mock else "api")


# ---------------------------------------------------------------- api / mock backend

class ApiBackend:
    """`client.beta.messages.parse` / `.tool_runner` — the original phase-machine code, moved."""

    name = "api"

    def __init__(self, client: Any, settings: Settings = SETTINGS, name: str = "api") -> None:
        self.client = client
        self.s = settings
        self.name = name

    def new_case(self) -> None:
        return None

    def _llm_kwargs(self, system: list[dict], model: str) -> dict:
        return {"model": model, "max_tokens": self.s.max_tokens, "system": system,
                "thinking": {"type": "adaptive"}, "output_config": {"effort": self.s.effort}}

    async def parse(self, *, phase: str, schema: type, user_text: str, history: list[dict],
                    system: list[dict], tool_defs: list[dict], model: str, log: Any) -> Any:
        history.append({"role": "user", "content": user_text})
        last_err = ""
        for _attempt in range(3):
            t0 = time.time()
            resp = await self.client.beta.messages.parse(
                **self._llm_kwargs(system, model),
                messages=history,
                tools=tool_defs,
                tool_choice={"type": "none"},
                output_format=schema,
            )
            log.record_llm(phase, resp, time.time() - t0)
            history.append(resp.to_param())
            if getattr(resp, "stop_reason", "") == "refusal":
                raise RuntimeError(f"{phase}: model refused (stop_details={getattr(resp, 'stop_details', None)})")
            parsed = getattr(resp, "parsed_output", None)
            if parsed is not None:
                return parsed
            last_err = f"no parsed output (stop_reason={getattr(resp, 'stop_reason', '')})"
            history.append({"role": "user", "content": f"Your previous reply could not be parsed ({last_err}). Reply again with only the requested structured output."})
        raise RuntimeError(f"{phase}: structured output failed after 3 attempts: {last_err}")

    async def investigate(self, *, phase: str, user_text: str, history: list[dict], system: list[dict],
                          tools: list[Any], model: str, log: Any, max_iterations: int) -> None:
        history.append({"role": "user", "content": user_text})
        runner = self.client.beta.messages.tool_runner(
            **self._llm_kwargs(system, model),
            tools=tools,
            messages=history,
            max_iterations=max_iterations,
        )
        t0 = time.time()
        async for msg in runner:
            log.record_llm(phase, msg, time.time() - t0)
            t0 = time.time()
            history.append(msg.to_param())
            tr = await runner.generate_tool_call_response()
            if tr is not None:
                history.append(tr)
            if getattr(msg, "stop_reason", "") == "refusal":
                raise RuntimeError(f"{phase}: model refused")


# ---------------------------------------------------------------- cli backend

def _system_text(system: list[dict] | str) -> str:
    if isinstance(system, str):
        return system
    return "\n\n".join(b.get("text", "") for b in system)


def _usage_response(phase: str, result: Any, model: str) -> Any:
    """`RunLog.record_llm` reads `.usage.{input,output,cache_read_input,cache_creation_input}_tokens`,
    `.model`, `.id`, `.stop_reason`. The Agent SDK reports usage once per `query()` call on the
    ResultMessage (the per-AssistantMessage `usage` repeats within one API response and would
    double-count), so one llm row is written per phase call. Missing figures are recorded as 0."""
    u = getattr(result, "usage", None) or {}
    return SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=int(u.get("input_tokens", 0) or 0),
            output_tokens=int(u.get("output_tokens", 0) or 0),
            cache_read_input_tokens=int(u.get("cache_read_input_tokens", 0) or 0),
            cache_creation_input_tokens=int(u.get("cache_creation_input_tokens", 0) or 0),
        ),
        model=model,
        id=getattr(result, "session_id", "") or "",
        stop_reason=getattr(result, "stop_reason", "") or "",
    )


def _sdk_tool(t: Any) -> Any:
    """Wrap one typed Anthropic tool (`BetaAsyncFunctionTool`) as an in-process SDK MCP tool.

    The body is the *same* coroutine the api backend runs, so budget, clamping, fact recording and
    `RunLog.record_tool` are identical; only the transport differs.
    """
    from claude_agent_sdk import tool as sdk_tool_decorator

    spec = t.to_dict()

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            out = await t.call(dict(args or {}))
        except Exception as e:  # never kill the loop on one bad call
            out = json.dumps({"error": f"{spec['name']} failed: {str(e)[:400]}"})
        text = out if isinstance(out, str) else json.dumps(out, default=str)
        return {"content": [{"type": "text", "text": text}]}

    handler.__name__ = spec["name"]
    return sdk_tool_decorator(spec["name"], spec.get("description", ""), spec["input_schema"])(handler)


class CliBackend:
    """claude-agent-sdk over the bundled Claude Code CLI — subscription auth, no API key."""

    name = "cli"

    def __init__(self, settings: Settings = SETTINGS) -> None:
        self.s = settings
        self.session_id: str | None = None

    def new_case(self) -> None:
        """One CLI session per case (P0–P10); the next case starts a fresh one."""
        self.session_id = None

    # -- options ---------------------------------------------------------
    def _options(self, *, system: list[dict], model: str, schema: dict | None = None,
                 mcp: dict | None = None, allowed: list[str] | None = None,
                 max_turns: int = 1, log: Any = None) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions, PermissionResultAllow, PermissionResultDeny

        allowed = allowed or []
        allowed_set = set(allowed)

        async def can_use_tool(name: str, _input: dict, _ctx: Any):
            if name in allowed_set:
                return PermissionResultAllow()
            if log is not None:
                log.note("cli backend denied a tool the agent asked for", tool=name)
            return PermissionResultDeny(message=f"{name} is not available in this harness.")

        kw: dict[str, Any] = {
            "system_prompt": {"type": "custom", "prompt": _system_text(system)},
            "model": model,
            "tools": [],                       # no built-in Read/Write/Bash/... tools
            "allowed_tools": allowed,
            "setting_sources": [],             # ignore the user's settings and CLAUDE.md
            "mcp_servers": mcp or {},
            "max_turns": max_turns,
            "cwd": str(self.s.runs_dir.parent),
            "effort": self.s.effort,
            "thinking": {"type": "adaptive"},
            "can_use_tool": can_use_tool,
            "resume": self.session_id,
            "env": {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(self.s.max_tokens)},
        }
        if schema is not None:
            kw["output_format"] = {"type": "json_schema", "schema": schema}
        return ClaudeAgentOptions(**kw)

    async def _run(self, *, prompt: str, options: Any, phase: str, model: str, log: Any) -> Any:
        """One `query()` call; returns the ResultMessage and records exactly one llm row."""
        from claude_agent_sdk import ResultMessage, query

        t0 = time.time()
        result = None
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, ResultMessage):
                result = msg
        if result is None:
            raise RuntimeError(f"{phase}: the Claude CLI returned no result message")
        self.session_id = result.session_id or self.session_id
        log.record_llm(phase, _usage_response(phase, result, model), time.time() - t0)
        if result.is_error:
            raise RuntimeError(f"{phase}: CLI error ({result.subtype}): {result.errors or result.result}")
        return result

    # -- interface -------------------------------------------------------
    async def parse(self, *, phase: str, schema: type, user_text: str, history: list[dict],
                    system: list[dict], tool_defs: list[dict], model: str, log: Any) -> Any:
        history.append({"role": "user", "content": user_text})
        js = schema.model_json_schema()
        prompt, last_err = user_text, ""
        for _attempt in range(3):
            result = await self._run(prompt=prompt, phase=phase, model=model, log=log,
                                     options=self._options(system=system, model=model, schema=js,
                                                           max_turns=PARSE_MAX_TURNS, log=log))
            raw = result.structured_output
            if raw is not None:
                try:
                    parsed = schema.model_validate(raw)
                except Exception as e:
                    last_err = f"structured output did not validate: {str(e)[:300]}"
                else:
                    history.append({"role": "assistant", "content": json.dumps(raw, default=str)})
                    return parsed
            else:
                last_err = f"no structured output (stop_reason={result.stop_reason})"
            log.note(f"{phase}: retrying structured output", error=last_err)
            prompt = f"{user_text}\n\nYour previous reply could not be used ({last_err}). Reply again with only the requested structured output."
        raise RuntimeError(f"{phase}: structured output failed after 3 attempts: {last_err}")

    async def investigate(self, *, phase: str, user_text: str, history: list[dict], system: list[dict],
                          tools: list[Any], model: str, log: Any, max_iterations: int) -> None:
        from claude_agent_sdk import create_sdk_mcp_server

        history.append({"role": "user", "content": user_text})
        server = create_sdk_mcp_server(MCP_SERVER_NAME, "1.0.0", [_sdk_tool(t) for t in tools])
        allowed = [f"mcp__{MCP_SERVER_NAME}__{t.to_dict()['name']}" for t in tools]
        result = await self._run(
            prompt=user_text, phase=phase, model=model, log=log,
            options=self._options(system=system, model=model, mcp={MCP_SERVER_NAME: server},
                                  allowed=allowed, max_turns=max_iterations, log=log),
        )
        text = (result.result or "").strip()
        if text:
            history.append({"role": "assistant", "content": text})


# ---------------------------------------------------------------- gemini backend

_GEMINI_STRIP_KEYS = frozenset({"$defs", "definitions", "$schema", "additionalProperties", "title", "default"})
_GEMINI_REFUSALS = frozenset({"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"})


def _gemini_model(model: str) -> str:
    """Normalise `model` for the Gemini REST endpoint, replacing a leftover Claude default."""
    m = (model or "").strip()
    if m.startswith("models/"):
        m = m[len("models/"):]
    if not m or m.lower().startswith("claude"):
        return DEFAULT_GEMINI_MODEL
    return m


def _gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline `$defs`/`$ref` and strip keywords that Gemini's OpenAPI 3.0 `Schema` object rejects."""
    defs = dict(schema.get("$defs") or schema.get("definitions") or {})

    def walk(node: Any) -> Any:
        if not isinstance(node, dict):
            return [walk(x) for x in node] if isinstance(node, list) else node
        if "$ref" in node:
            ref = str(node["$ref"]).rsplit("/", 1)[-1]
            target = dict(defs.get(ref) or {})
            merged = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
            return walk(merged)
        for union_key in ("anyOf", "oneOf"):
            if union_key in node and isinstance(node[union_key], list):
                non_null = [b for b in node[union_key] if not (isinstance(b, dict) and b.get("type") == "null")]
                has_null = len(non_null) < len(node[union_key])
                base = dict(walk(non_null[0])) if non_null else {"type": "string"}
                if has_null:
                    base["nullable"] = True
                if "description" in node and "description" not in base:
                    base["description"] = node["description"]
                return base
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k in _GEMINI_STRIP_KEYS:
                continue
            if k == "properties" and isinstance(v, dict):
                out["properties"] = {pk: walk(pv) for pk, pv in v.items()}
            elif k == "items":
                out["items"] = walk(v)
            else:
                out[k] = walk(v)
        return out

    return walk(schema)


def _gemini_tool_declarations(tools: list[Any]) -> list[dict[str, Any]]:
    decls: list[dict[str, Any]] = []
    for t in tools:
        spec = t.to_dict()
        params = _gemini_schema(spec.get("input_schema") or {"type": "object", "properties": {}})
        decls.append({
            "name": spec["name"],
            "description": spec.get("description", ""),
            "parameters": params,
        })
    return decls


def _gemini_contents(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert `PhaseMachine.history` into Gemini `contents`, merging adjacent same-role turns."""
    contents: list[dict[str, Any]] = []
    for msg in history:
        role = "model" if msg.get("role") in ("assistant", "model") else "user"
        if "parts" in msg and isinstance(msg["parts"], list):
            parts = list(msg["parts"])
        else:
            raw = msg.get("content", "")
            if isinstance(raw, str):
                parts = [{"text": raw}] if raw else []
            elif isinstance(raw, list):
                parts = [{"text": b.get("text", str(b))} if isinstance(b, dict) else {"text": str(b)} for b in raw]
            else:
                parts = [{"text": json.dumps(raw, default=str)}]
        if not parts:
            continue
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": role, "parts": parts})
    return contents


def _gemini_usage_response(data: dict[str, Any], model: str) -> Any:
    """Map Gemini `usageMetadata` onto the `.usage` shape `RunLog.record_llm` consumes."""
    u = data.get("usageMetadata") or {}
    prompt_total = int(u.get("promptTokenCount", 0) or 0)
    cached = int(u.get("cachedContentTokenCount", 0) or 0)
    inp = max(0, prompt_total - cached)
    out = int(u.get("candidatesTokenCount", 0) or 0) + int(u.get("thoughtsTokenCount", 0) or 0)
    cand = (data.get("candidates") or [{}])[0] if isinstance(data.get("candidates"), list) and data.get("candidates") else {}
    return SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=inp,
            output_tokens=out,
            cache_read_input_tokens=cached,
            cache_creation_input_tokens=0,
        ),
        model=data.get("modelVersion") or model,
        id=data.get("responseId", "") or "",
        stop_reason=cand.get("finishReason", "") or "",
    )


def _gemini_text_from_parts(parts: list[dict[str, Any]]) -> str:
    return "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict) and not p.get("thought") and "text" in p).strip()


class GeminiBackend:
    """Google Gemini API (`generateContent` over v1beta) — needs GEMINI_API_KEY (or GOOGLE_API_KEY)."""

    name = "gemini"

    def __init__(self, settings: Settings = SETTINGS, transport: Any = None) -> None:
        self.s = settings
        self.transport = transport

    def new_case(self) -> None:
        return None

    async def _generate(self, *, model: str, system: list[dict] | str, contents: list[dict],
                        generation_config: dict[str, Any], tools: list[dict] | None = None) -> dict[str, Any]:
        m = _gemini_model(model)
        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": generation_config,
        }
        sys_text = _system_text(system)
        if sys_text:
            payload["systemInstruction"] = {"parts": [{"text": sys_text}]}
        if tools:
            payload["tools"] = [{"functionDeclarations": tools}]
        if self.transport is not None:
            return await self.transport(m, payload)

        api_key = (self.s.gemini_api_key or "").strip()
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY (or GOOGLE_API_KEY) is required when LLM_BACKEND=gemini")

        import httpx

        url = f"{GEMINI_API_BASE}/{m}:generateContent"
        headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
        last_err = ""
        async with httpx.AsyncClient(timeout=600.0) as client:
            for attempt in range(5):
                try:
                    resp = await client.post(url, headers=headers, json=payload)
                except httpx.TransportError as e:
                    last_err = f"network error: {e}"
                    if attempt < 4:
                        await asyncio.sleep(min(2 ** attempt, 16))
                        continue
                    raise RuntimeError(f"Gemini request to {m} failed after 5 attempts: {last_err}") from e
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < 4:
                    retry_after = float(resp.headers.get("retry-after", "0") or 0)
                    await asyncio.sleep(max(retry_after, float(min(2 ** (attempt + 1), 30))))
                    continue
                raise RuntimeError(f"Gemini HTTP {resp.status_code} ({m}): {resp.text[:500]}")
        raise RuntimeError(f"Gemini request to {m} failed: {last_err}")

    async def parse(self, *, phase: str, schema: type, user_text: str, history: list[dict],
                    system: list[dict], tool_defs: list[dict], model: str, log: Any) -> Any:
        history.append({"role": "user", "content": user_text})
        js = _gemini_schema(schema.model_json_schema())
        gen_cfg = {
            "maxOutputTokens": self.s.max_tokens,
            "responseMimeType": "application/json",
            "responseSchema": js,
        }
        m = _gemini_model(model)
        last_err = ""
        for _attempt in range(3):
            t0 = time.time()
            data = await self._generate(
                model=m, system=system, contents=_gemini_contents(history), generation_config=gen_cfg,
            )
            log.record_llm(phase, _gemini_usage_response(data, m), time.time() - t0)
            cands = data.get("candidates") or []
            cand = cands[0] if cands else {}
            finish = str(cand.get("finishReason", "") or "")
            if finish in _GEMINI_REFUSALS:
                raise RuntimeError(f"{phase}: model refused (finishReason={finish})")
            parts = (cand.get("content") or {}).get("parts") or []
            text = _gemini_text_from_parts(parts)
            history.append({"role": "assistant", "content": text or "{}"})
            if text:
                try:
                    return schema.model_validate_json(text)
                except Exception as e:
                    last_err = f"structured output did not validate: {str(e)[:300]}"
            else:
                last_err = f"no structured output (finishReason={finish})"
            if log is not None:
                log.note(f"{phase}: retrying structured output", error=last_err)
            history.append({
                "role": "user",
                "content": f"Your previous reply could not be parsed ({last_err}). Reply again with only the requested structured output.",
            })
        raise RuntimeError(f"{phase}: structured output failed after 3 attempts: {last_err}")

    async def investigate(self, *, phase: str, user_text: str, history: list[dict], system: list[dict],
                          tools: list[Any], model: str, log: Any, max_iterations: int) -> None:
        history.append({"role": "user", "content": user_text})
        tool_decls = _gemini_tool_declarations(tools)
        tool_map = {t.to_dict()["name"]: t for t in tools}
        gen_cfg = {"maxOutputTokens": self.s.max_tokens}
        m = _gemini_model(model)
        for _turn in range(max_iterations):
            t0 = time.time()
            data = await self._generate(
                model=m, system=system, contents=_gemini_contents(history),
                generation_config=gen_cfg, tools=tool_decls,
            )
            log.record_llm(phase, _gemini_usage_response(data, m), time.time() - t0)
            cands = data.get("candidates") or []
            cand = cands[0] if cands else {}
            finish = str(cand.get("finishReason", "") or "")
            if finish in _GEMINI_REFUSALS:
                raise RuntimeError(f"{phase}: model refused (finishReason={finish})")
            content = dict(cand.get("content") or {"role": "model", "parts": []})
            content["role"] = "model"
            parts = list(content.get("parts") or [])
            content["parts"] = parts
            if parts:
                history.append(content)
            calls = [p["functionCall"] for p in parts if isinstance(p, dict) and isinstance(p.get("functionCall"), dict)]
            if not calls:
                break
            fn_responses: list[dict[str, Any]] = []
            for fc in calls:
                fname = str(fc.get("name", ""))
                fargs = dict(fc.get("args") or {})
                tool_obj = tool_map.get(fname)
                if tool_obj is None:
                    raw_out = json.dumps({"error": f"unknown tool: {fname}"})
                else:
                    try:
                        raw_out = await tool_obj.call(fargs)
                    except Exception as e:
                        raw_out = json.dumps({"error": f"{fname} failed: {str(e)[:400]}"})
                if isinstance(raw_out, str):
                    try:
                        parsed_out = json.loads(raw_out)
                        resp_payload = parsed_out if isinstance(parsed_out, dict) else {"result": parsed_out}
                    except Exception:
                        resp_payload = {"result": raw_out}
                elif isinstance(raw_out, dict):
                    resp_payload = raw_out
                else:
                    resp_payload = {"result": raw_out}
                fr: dict[str, Any] = {"name": fname, "response": resp_payload}
                if "id" in fc:
                    fr["id"] = fc["id"]
                fn_responses.append({"functionResponse": fr})
            history.append({"role": "user", "parts": fn_responses})
