"""Read and write .claude/settings.local.json for persistent permission rules.

This mirrors Claude Code's own settings.local.json format so that permission
rules created via OpenShrimp's Telegram UI are also respected by the Claude
CLI (and vice versa).

File format::

    {
      "permissions": {
        "allow": ["Bash(git:*)", "Bash(npm:*)"]
      }
    }

Rule string format: ``ToolName(content:*)`` for prefix rules,
``ToolName(exact command)`` for exact matches, ``ToolName`` for blanket.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from open_shrimp.hooks import ApprovalRule

logger = logging.getLogger(__name__)


def _settings_path(project_dir: str) -> Path:
    """Return the path to .claude/settings.local.json in a project directory."""
    return Path(project_dir) / ".claude" / "settings.local.json"


def _read_settings(project_dir: str) -> dict[str, Any]:
    """Read and parse settings.local.json, returning {} on missing/invalid."""
    path = _settings_path(project_dir)
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write_settings(project_dir: str, data: dict[str, Any]) -> None:
    """Write settings.local.json."""
    path = _settings_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _rule_string_to_approval_rule(rule_str: str) -> ApprovalRule | None:
    """Convert a settings.local.json rule string to an ApprovalRule.

    Only converts Bash prefix rules (``Bash(prefix:*)``) for now.
    Returns None for rules we don't handle.
    """
    if not rule_str.startswith("Bash(") or not rule_str.endswith(")"):
        return None
    content = rule_str[5:-1]  # strip "Bash(" and ")"
    if content.endswith(":*"):
        prefix = content[:-2]
        if prefix:
            return ApprovalRule(tool_name="Bash", pattern=f"{prefix} *")
    return None


def _approval_rule_to_rule_string(rule: ApprovalRule) -> str | None:
    """Convert an ApprovalRule to a settings.local.json rule string.

    Only converts Bash prefix rules (pattern like ``prefix *``) for now.
    Returns None for rules we don't handle.
    """
    if rule.tool_name != "Bash" or rule.pattern is None:
        return None
    # Pattern is "prefix *" — convert to "Bash(prefix:*)"
    if rule.pattern.endswith(" *"):
        prefix = rule.pattern[:-2]
        if prefix:
            return f"Bash({prefix}:*)"
    return None


def _load_persistent_rules_sync(project_dir: str) -> list[ApprovalRule]:
    """Load persistent approval rules from settings.local.json (sync)."""
    settings = _read_settings(project_dir)
    permissions = settings.get("permissions", {})
    allow_list = permissions.get("allow", [])

    rules: list[ApprovalRule] = []
    for rule_str in allow_list:
        if not isinstance(rule_str, str):
            continue
        rule = _rule_string_to_approval_rule(rule_str)
        if rule is not None:
            rules.append(rule)
    return rules


async def load_persistent_rules(project_dir: str) -> list[ApprovalRule]:
    """Load persistent approval rules from settings.local.json."""
    return await asyncio.to_thread(_load_persistent_rules_sync, project_dir)


def _save_persistent_rule_sync(project_dir: str, rule: ApprovalRule) -> bool:
    """Save an approval rule to settings.local.json (sync).

    Appends the rule to permissions.allow if not already present.
    Returns True if the rule was added, False if already present or unsupported.
    """
    rule_str = _approval_rule_to_rule_string(rule)
    if rule_str is None:
        return False

    settings = _read_settings(project_dir)
    permissions = settings.setdefault("permissions", {})
    allow_list = permissions.setdefault("allow", [])

    if rule_str in allow_list:
        return False

    allow_list.append(rule_str)
    _write_settings(project_dir, settings)
    logger.info("Saved persistent rule %s to %s", rule_str, _settings_path(project_dir))
    return True


async def save_persistent_rule(project_dir: str, rule: ApprovalRule) -> bool:
    """Save an approval rule to settings.local.json.

    Appends the rule to permissions.allow if not already present.
    Returns True if the rule was added, False if already present or unsupported.
    """
    return await asyncio.to_thread(_save_persistent_rule_sync, project_dir, rule)
