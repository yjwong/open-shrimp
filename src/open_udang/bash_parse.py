"""Bash command parsing via tree-sitter.

Uses tree-sitter with the bash grammar to parse shell commands into an AST,
extract individual subcommands from compound statements (``&&``, ``||``,
``;``), and run security pre-checks.

tree-sitter-bash is a hard dependency — no fallback to string splitting.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import tree_sitter_bash as tsbash
from tree_sitter import Language, Parser

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tree-sitter setup (lazy singleton)
# ---------------------------------------------------------------------------

_parser: Parser | None = None


def _get_parser() -> Parser:
    """Return a cached tree-sitter bash parser."""
    global _parser
    if _parser is not None:
        return _parser
    language = Language(tsbash.language())
    _parser = Parser(language)
    return _parser


# ---------------------------------------------------------------------------
# Pre-parse security checks
# ---------------------------------------------------------------------------

# Control characters (excluding TAB \x09 and LF \x0A).
_RE_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0B-\x1F\x7F]")

# Unicode whitespace that can be used to obfuscate commands.
_RE_UNICODE_WHITESPACE = re.compile(
    r"[\u00A0\u1680\u2000-\u200B\u2028\u2029\u202F\u205F\u3000\uFEFF]"
)

# Backslash-escaped whitespace.
_RE_BACKSLASH_WHITESPACE = re.compile(r"\\[ \t]|[^ \t\n\\]\\\n")

# Zsh dynamic directory syntax.
_RE_ZSH_DYNAMIC_DIR = re.compile(r"~\[")

# Brace containing quote character (checked after stripping braces inside
# quotes, but we simplify: reject any brace with quotes outside quotes).
_RE_BRACE_QUOTE = re.compile(r"\{[^}]*['\"]")


def _strip_braces_in_quotes(command: str) -> str:
    """Replace ``{`` with space when inside single or double quotes.

    Ensures the brace-quote check only fires for braces outside quoted
    strings.
    """
    result: list[str] = []
    in_single = False
    in_double = False
    i = 0
    while i < len(command):
        ch = command[i]
        if ch == "\\" and in_double and i + 1 < len(command):
            result.append(ch)
            result.append(command[i + 1])
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "{" and (in_single or in_double):
            result.append(" ")
            i += 1
            continue
        result.append(ch)
        i += 1
    return "".join(result)


class TooComplexError(Exception):
    """Raised when a command is too complex to safely auto-approve."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def check_pre_parse_security(command: str) -> None:
    """Run pre-parse security checks on a raw command string.

    Raises ``TooComplexError`` if the command contains patterns that
    indicate obfuscation or complexity that makes static analysis
    unreliable.
    """
    if _RE_CONTROL_CHARS.search(command):
        raise TooComplexError("Contains control characters")
    if _RE_UNICODE_WHITESPACE.search(command):
        raise TooComplexError("Contains Unicode whitespace")
    if _RE_BACKSLASH_WHITESPACE.search(command):
        raise TooComplexError("Contains backslash-escaped whitespace")
    if _RE_ZSH_DYNAMIC_DIR.search(command):
        raise TooComplexError("Contains zsh ~[ dynamic directory syntax")
    processed = _strip_braces_in_quotes(command)
    if _RE_BRACE_QUOTE.search(processed):
        raise TooComplexError(
            "Contains brace expansion with quote characters"
        )


# ---------------------------------------------------------------------------
# AST-based subcommand extraction
# ---------------------------------------------------------------------------

# Node types that represent compound constructs whose named children
# should be recursed into (operator tokens like &&, ||, ; are unnamed
# and skipped automatically).
_COMPOUND_TYPES = {"program", "list", "redirected_statement"}

# Node types that represent individual executable units.
_COMMAND_TYPES = {"command", "declaration_command", "variable_assignment",
                  "unset_command", "test_command"}

# Node types that indicate the command is too complex to auto-approve
# when encountered in unexpected positions.
_TOO_COMPLEX_TYPES = {
    "command_substitution", "process_substitution", "expansion",
    "simple_expansion", "brace_expression", "compound_statement",
    "function_definition", "ansi_c_string", "translated_string",
    "herestring_redirect", "heredoc_redirect",
}


def _collect_subcommands(node: Any) -> list[str]:
    """Recursively collect individual subcommand strings from an AST node.

    ``command`` and similar types are treated as atomic units.
    ``list``, ``program``, ``pipeline``, ``redirected_statement`` are
    recursed into.  Subshells and control-flow constructs (for, if,
    while) are returned as-is (opaque).

    Raises ``TooComplexError`` for dangerous node types in unexpected
    positions.
    """
    if node.type in _COMMAND_TYPES:
        text = node.text.decode()
        return [text] if text else []

    # Pipelines are a single logical unit — don't decompose.
    if node.type == "pipeline":
        text = node.text.decode()
        return [text] if text else []

    if node.type in _COMPOUND_TYPES:
        results: list[str] = []
        for child in node.children:
            if child.is_named:
                results.extend(_collect_subcommands(child))
        return results

    if node.type == "negated_command":
        # Skip the `!` token, recurse into the actual command.
        for child in node.children:
            if child.is_named:
                return _collect_subcommands(child)
        return []

    if node.type == "subshell":
        # Treat subshell as opaque — return its full text.
        text = node.text.decode()
        return [text] if text else []

    if node.type in ("for_statement", "if_statement", "while_statement",
                      "until_statement", "case_statement"):
        # Control-flow constructs — return as opaque.
        text = node.text.decode()
        return [text] if text else []

    if node.type == "ERROR":
        raise TooComplexError("Parse error in command")

    if node.type in _TOO_COMPLEX_TYPES:
        raise TooComplexError(f"Contains {node.type}")

    # Unknown node type — return as opaque.
    text = node.text.decode()
    return [text] if text else []


def split_subcommands(command: str) -> list[str]:
    """Split a bash command into individual subcommands using tree-sitter.

    Runs pre-parse security checks, then parses the command into an AST
    and extracts subcommands.  Pipe segments are kept together as a
    single pipeline.

    Raises ``TooComplexError`` if the command fails pre-parse security
    checks, contains parse errors, or has dangerous node types.
    """
    cmd = command.strip()
    if not cmd:
        return []

    check_pre_parse_security(cmd)

    parser = _get_parser()
    tree = parser.parse(cmd.encode())
    return _collect_subcommands(tree.root_node)


def _has_pipeline(node: Any) -> bool:
    """Return True if the AST contains a pipeline node."""
    if node.type == "pipeline":
        return True
    for child in node.children:
        if _has_pipeline(child):
            return True
    return False


def is_compound_command(command: str) -> bool:
    """Return True if *command* contains multiple subcommands or pipes.

    Uses tree-sitter for accurate detection that respects quoting.
    Splits on ``&&``, ``||``, ``;``, and ``|`` — so pipelines are also
    considered compound for the purpose of prefix rule matching.

    Returns True on ``TooComplexError`` (safe default).
    """
    cmd = command.strip()
    if not cmd:
        return False
    try:
        check_pre_parse_security(cmd)
        parser = _get_parser()
        tree = parser.parse(cmd.encode())
        root = tree.root_node
        # Check for multiple subcommands (&&, ||, ;)
        subcommands = _collect_subcommands(root)
        if len(subcommands) > 1:
            return True
        # Check for pipelines (|)
        return _has_pipeline(root)
    except TooComplexError:
        return True


# ---------------------------------------------------------------------------
# Compound command safety checks
# ---------------------------------------------------------------------------

# Maximum number of subcommands before requiring manual approval.
MAX_SUBCOMMANDS = 50

# Commands that constitute write operations (for cd + write detection).
_WRITE_COMMANDS = {
    "rm", "rmdir", "mv", "cp", "mkdir", "touch", "sed", "tee", "dd",
    "install", "rsync", "chmod", "chown", "chgrp", "ln",
}


def _get_base_command(subcommand: str) -> str | None:
    """Extract the base command name from a single subcommand string.

    Strips env-var prefixes (``VAR=val cmd``) and leading paths.
    """
    words = subcommand.strip().split()
    for word in words:
        if "=" in word:
            continue  # Skip VAR=val prefixes
        # Strip leading path
        return word.rsplit("/", 1)[-1] or None
    return None


def _is_cd_command(subcommand: str) -> bool:
    """Return True if *subcommand* is a ``cd`` command."""
    return _get_base_command(subcommand) == "cd"


def _is_git_command(subcommand: str) -> bool:
    """Return True if *subcommand* is a ``git`` command."""
    base = _get_base_command(subcommand)
    return base == "git" or base == "xargs" and "git" in subcommand


def _is_write_command(subcommand: str) -> bool:
    """Return True if *subcommand* is a write operation."""
    base = _get_base_command(subcommand)
    return base is not None and base in _WRITE_COMMANDS


def check_compound_safety(subcommands: list[str]) -> str | None:
    """Check compound command safety rules.

    Returns an error reason string if the command should require manual
    approval, or None if it passes all safety checks.
    """
    if len(subcommands) > MAX_SUBCOMMANDS:
        return (
            f"Command splits into {len(subcommands)} subcommands, "
            f"too many to safety-check individually"
        )

    cd_commands = [s for s in subcommands if _is_cd_command(s)]

    if len(cd_commands) > 1:
        return (
            "Multiple directory changes in one command require "
            "approval for clarity"
        )

    has_cd = len(cd_commands) > 0

    if has_cd:
        if any(_is_git_command(s) for s in subcommands):
            return (
                "Compound commands with cd and git require approval "
                "to prevent bare repository attacks"
            )
        if any(_is_write_command(s) for s in subcommands):
            return (
                "Compound command contains cd with write operation — "
                "manual approval required to prevent path resolution bypass"
            )

    return None
