"""Graph ids of the agent's own write-backs (AgentCase, CaseEvent, Approval) for an exam case.

`AC-<case_id>` by default. A deployment that runs the agent sets `AGENT_CASE_NAMESPACE` (e.g. `web`) and writes
`AC-web-<case_id>` instead, so its runs never overwrite the submitted run's `AC-<case_id>` write-backs in the shared
graph. CaseEvent (`<AC id>-<seq>`) and Approval (`AP-<AC id>-<ACTION>`) ids derive from the AgentCase id. Every id
still starts with `AC-`, which is how the harness tells agent cases from closed cases.
"""
from __future__ import annotations

import os
import re

_NS_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")


def namespace() -> str:
    """AGENT_CASE_NAMESPACE, or '' when unset. Anything but 1-16 letters, digits or '_' is refused, not cleaned."""
    ns = os.getenv("AGENT_CASE_NAMESPACE", "").strip()
    if ns and not _NS_RE.match(ns):
        raise ValueError(f"AGENT_CASE_NAMESPACE={ns!r}: use 1-16 letters, digits or '_'")
    return ns


def agent_case_id(case_id: str) -> str:
    """The AgentCase id this process writes for `case_id`; an id that is already an AgentCase id is returned as is."""
    if case_id.startswith("AC-"):
        return case_id
    ns = namespace()
    return f"AC-{ns}-{case_id}" if ns else f"AC-{case_id}"


def all_agent_case_ids(case_id: str) -> set[str]:
    """Every AgentCase id an exam case can have in the graph: the default one and this process's namespaced one."""
    return {f"AC-{case_id}", agent_case_id(case_id)}
