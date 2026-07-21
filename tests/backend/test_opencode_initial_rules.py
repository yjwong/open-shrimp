"""Initial-permission-rule construction for the OpenCode client.

OpenCode's evaluator picks the LAST matching rule, so rule order is the
contract under test: the ask baseline first, allows after, and
``disallowed_tools`` denies at the very end (a ``deny`` on pattern ``*``
also removes the tool from the model's tool list entirely).
"""

from __future__ import annotations

from open_shrimp.backend.opencode.client import OpenCodeClient
from open_shrimp.backend.protocol import BackendOptions


def _rules(**kwargs) -> list[dict]:
    opts = BackendOptions(cwd="/work", model="prov/model", **kwargs)
    return OpenCodeClient(opts)._build_initial_rules()


def _last_matching(rules: list[dict], permission: str) -> dict:
    import fnmatch

    matches = [
        r for r in rules if fnmatch.fnmatch(permission, r["permission"])
    ]
    assert matches, f"no rule matches {permission!r}"
    return matches[-1]


def test_disallowed_tools_become_last_position_deny_rules():
    rules = _rules(disallowed_tools=["websearch", "webfetch"])
    assert _last_matching(rules, "websearch") == {
        "permission": "websearch", "pattern": "*", "action": "deny",
    }
    assert _last_matching(rules, "webfetch") == {
        "permission": "webfetch", "pattern": "*", "action": "deny",
    }


def test_disallowed_deny_wins_over_allowed_tools_allow():
    """A tool both allowed and disallowed ends up denied — the deny is
    appended after the allow, and last-match wins."""
    rules = _rules(
        allowed_tools=["webfetch"], disallowed_tools=["webfetch"],
    )
    assert _last_matching(rules, "webfetch")["action"] == "deny"


def test_disallowed_accepts_hooks_and_mcp_forms():
    rules = _rules(
        disallowed_tools=["WebFetch", "mcp__playwright__browser_navigate"],
    )
    assert _last_matching(rules, "webfetch")["action"] == "deny"
    assert _last_matching(rules, "playwright_browser_navigate") == {
        "permission": "playwright_browser_navigate",
        "pattern": "*",
        "action": "deny",
    }


def test_no_disallowed_tools_keeps_ask_baseline():
    rules = _rules()
    assert _last_matching(rules, "websearch")["action"] == "ask"
    assert not any(r["action"] == "deny" for r in rules)
