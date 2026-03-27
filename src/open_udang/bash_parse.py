"""Bash command parsing via tree-sitter.

Uses tree-sitter with the bash grammar to parse shell commands into an AST,
decompose compound statements (``&&``, ``||``, ``;``), resolve values, and
run security pre-checks.

Features:

- Full AST-based command decomposition with value resolution
- Variable tracking across compound commands
- Structured command objects (argv, env_vars, redirects, text)
- Heredoc safety validation (quoted delimiters required)
- Safe ``cat <<'EOF'`` pattern recognition
- Redirect validation
- Command prefix stripping (time, nohup, timeout, nice, env, stdbuf)
- Brace expansion detection at the AST level
- Pipeline handling as separate segments

tree-sitter-bash is a hard dependency — no fallback to string splitting.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
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
# quotes).
_RE_BRACE_QUOTE = re.compile(r"\{[^}]*['\"]")

# Brace expansion in word/concatenation nodes.
_RE_BRACE_EXPANSION = re.compile(r"\{[^{}\s]*(,|\.\.)[^{}\s]*\}")

# Valid arithmetic literal.
_RE_ARITHMETIC_LITERAL = re.compile(
    r"^(?:[0-9]+|0[xX][0-9a-fA-F]+|[0-9]+#[0-9a-zA-Z]+"
    r"|[-+*/%^&|~!<>=?:(),]+|<<|>>|\*\*|&&|\|\||[<>=!]=|\$\(\(|\)\))$"
)

# Dangerous content in heredocs.
_RE_HEREDOC_DANGEROUS = re.compile(r"/proc/.*/environ")

# Comment after newline (can hide arguments from path validation).
_RE_HERESTRING_COMMENT = re.compile(r"\n[ \t]*#")

# Characters that require quoting in reconstructed command text.
_RE_NEEDS_QUOTING = re.compile(r"""["'\\ \t\n$`;|&<>(){}*?\[\]~#]""")

# Valid variable name.
_RE_VALID_VAR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Characters that indicate a resolved value contains unresolvable parts
# (used for simple_expansion safety in non-double-quote context).
_RE_UNSAFE_VALUE_CHARS = re.compile(r"[ \t\n*?\[]")


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
        if in_single:
            if ch == "'":
                in_single = False
            result.append(" " if ch == "{" else ch)
            i += 1
        elif in_double:
            if ch == "\\" and i + 1 < len(command) and command[i + 1] in (
                '"', "\\"
            ):
                result.append(ch)
                result.append(command[i + 1])
                i += 2
            else:
                if ch == '"':
                    in_double = False
                result.append(" " if ch == "{" else ch)
                i += 1
        elif ch == "\\" and i + 1 < len(command):
            result.append(ch)
            result.append(command[i + 1])
            i += 2
        else:
            if ch == "'":
                in_single = True
            elif ch == '"':
                in_double = True
            result.append(ch)
            i += 1
    return "".join(result)


# ---------------------------------------------------------------------------
# Parse result types
# ---------------------------------------------------------------------------


class TooComplexError(Exception):
    """Raised when a command is too complex to safely auto-approve."""

    def __init__(self, reason: str, node_type: str | None = None) -> None:
        self.reason = reason
        self.node_type = node_type
        super().__init__(reason)


@dataclass
class Redirect:
    """A file redirect parsed from the AST."""
    op: str        # ">", ">>", "<", ">&", "<&", ">|", "&>", "&>>", "<<<"
    target: str    # resolved target path/string
    fd: int | None = None  # file descriptor, if specified


@dataclass
class EnvVar:
    """An environment variable assignment preceding a command."""
    name: str
    value: str


@dataclass
class ParsedCommand:
    """A single command extracted from the AST with resolved arguments."""
    argv: list[str]
    env_vars: list[EnvVar] = field(default_factory=list)
    redirects: list[Redirect] = field(default_factory=list)
    text: str = ""


@dataclass
class ParseResult:
    """Result of parsing a command string."""
    kind: str  # "simple" or "too-complex"
    commands: list[ParsedCommand] = field(default_factory=list)
    reason: str | None = None
    node_type: str | None = None


# Placeholder strings for resolved values.
_PLACEHOLDER_CMD_SUB = "__CMDSUB_OUTPUT__"
_PLACEHOLDER_UNRESOLVED = "__TRACKED_VAR__"


def _has_placeholder(value: str) -> bool:
    """Return True if value contains a placeholder."""
    return _PLACEHOLDER_CMD_SUB in value or _PLACEHOLDER_UNRESOLVED in value


# ---------------------------------------------------------------------------
# Known variable sets
# ---------------------------------------------------------------------------

# Well-known environment variables that are safe to treat as "unresolved but
# not dangerous" in double-quote context.
_KNOWN_ENV_VARS: set[str] = {
    "HOME", "PWD", "OLDPWD", "USER", "LOGNAME", "SHELL", "PATH",
    "HOSTNAME", "UID", "EUID", "PPID", "RANDOM", "SECONDS", "LINENO",
    "TMPDIR", "BASH_VERSION", "BASHPID", "SHLVL", "HISTFILE", "IFS",
}

# Special variables ($?, $$, $!, $#, $0, $-).
_KNOWN_SPECIAL_VARS: set[str] = {"?", "$", "!", "#", "0", "-"}

# Redirect operator mapping.
_REDIRECT_OPS: dict[str, str] = {
    ">": ">", ">>": ">>", "<": "<", ">&": ">&", "<&": "<&",
    ">|": ">|", "&>": "&>", "&>>": "&>>", "<<<": "<<<",
}

# Node types that are inherently too complex.
_TOO_COMPLEX_TYPES: set[str] = {
    "command_substitution", "process_substitution", "expansion",
    "simple_expansion", "brace_expression", "subshell",
    "compound_statement", "for_statement", "while_statement",
    "until_statement", "if_statement", "case_statement",
    "function_definition", "test_command", "ansi_c_string",
    "translated_string", "herestring_redirect", "heredoc_redirect",
}

# Compound/container node types that should be recursed into.
_COMPOUND_TYPES: set[str] = {"program", "list", "pipeline"}

# Operator token types that should be skipped during recursion.
_OPERATOR_TYPES: set[str] = {
    "&&", "||", ";", "|", "|&", "&", "\n",
}

# Shell keywords that should not appear as command names.
_SHELL_KEYWORDS: set[str] = {
    "for", "do", "done", "while", "until", "if", "then", "elif",
    "else", "fi", "case", "esac", "select", "function", "in",
}

# Dangerous builtins (eval, source, etc.).
_DANGEROUS_BUILTINS: set[str] = {
    "eval", "source", ".", "exec", "command", "builtin", "fc",
    "coproc", "noglob", "nocorrect", "trap", "enable", "mapfile",
    "readarray", "hash", "bind", "complete", "compgen", "alias", "let",
}

# Zsh-specific builtins that can bypass security checks.
_ZSH_DANGEROUS_BUILTINS: set[str] = {
    "zmodload", "emulate", "sysopen", "sysread", "syswrite", "sysseek",
    "zpty", "ztcp", "zsocket", "zf_rm", "zf_mv", "zf_ln", "zf_chmod",
    "zf_chown", "zf_mkdir", "zf_rmdir", "zf_chgrp",
}

# jq flags that can execute code or read arbitrary files.
_RE_JQ_DANGEROUS_FLAGS = re.compile(
    r"^(?:-[fL](?:$|[^A-Za-z])"
    r"|--(?:from-file|rawfile|slurpfile|library-path)(?:$|=))"
)

# Tools/builtins with flags that take variable names which may contain
# array subscripts (bash evaluates $(cmd) in subscripts).
_SUBSCRIPT_DANGER_FLAGS: dict[str, set[str]] = {
    "test": {"-v", "-R"},
    "[": {"-v", "-R"},
    "[[": {"-v", "-R"},
    "printf": {"-v"},
    "read": {"-a"},
    "unset": {"-v"},
}

# Commands where positional arguments may be variable names with subscripts.
_SUBSCRIPT_DANGER_COMMANDS: set[str] = {"read", "unset"}

# read flags that take an argument (skip next token).
_READ_ARG_FLAGS: set[str] = {"-p", "-d", "-n", "-N", "-t", "-u", "-i"}


# ---------------------------------------------------------------------------
# Pre-parse security checks
# ---------------------------------------------------------------------------


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
        raise TooComplexError(
            "Contains zsh ~[ dynamic directory syntax"
        )
    processed = _strip_braces_in_quotes(command)
    if _RE_BRACE_QUOTE.search(processed):
        raise TooComplexError(
            "Contains brace with quote character (expansion obfuscation)"
        )


# ---------------------------------------------------------------------------
# AST value resolution
# ---------------------------------------------------------------------------


def _too_complex(node: Any) -> ParseResult:
    """Return a too-complex result for the given node.

    """
    node_type = _get_text(node.type) if hasattr(node, "type") else None
    if node_type == "ERROR":
        reason = "Parse error"
    elif node_type in _TOO_COMPLEX_TYPES:
        reason = f"Contains {node_type}"
    else:
        reason = f"Unhandled node type: {node_type}"
    return ParseResult(kind="too-complex", reason=reason, node_type=node_type)


def _get_text(value: Any) -> str:
    """Get text from a node or bytes value."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _node_text(node: Any) -> str:
    """Get the text content of a tree-sitter node."""
    return _get_text(node.text)


def _strip_raw_string(text: str) -> str:
    """Strip surrounding single quotes from a raw_string.

    """
    return text[1:-1]


def _resolve_simple_expansion(
    node: Any, env: dict[str, str], in_double_quote: bool,
) -> str | ParseResult:
    """Resolve a simple_expansion ($VAR) node.

    """
    var_name: str | None = None
    is_special = False
    for child in node.children:
        if child is None:
            continue
        if _get_text(child.type) == "variable_name":
            var_name = _node_text(child)
            break
        if _get_text(child.type) == "special_variable_name":
            var_name = _node_text(child)
            is_special = True
            break
    if var_name is None:
        return _too_complex(node)

    # Check if the variable has a tracked value.
    tracked = env.get(var_name)
    if tracked is not None:
        if _has_placeholder(tracked):
            if not in_double_quote:
                return _too_complex(node)
            return _PLACEHOLDER_UNRESOLVED
        if not in_double_quote:
            # Outside double quotes, empty or whitespace-containing values
            # are dangerous (word splitting).
            if tracked == "":
                return _too_complex(node)
            if _RE_UNSAFE_VALUE_CHARS.search(tracked):
                return _too_complex(node)
        return tracked

    # Variable not tracked — check if it's a known safe variable.
    if in_double_quote:
        if var_name in _KNOWN_ENV_VARS:
            return _PLACEHOLDER_UNRESOLVED
        if is_special and (
            var_name in _KNOWN_SPECIAL_VARS
            or re.match(r"^[0-9]+$", var_name)
        ):
            return _PLACEHOLDER_UNRESOLVED

    return _too_complex(node)


def _check_arithmetic(node: Any) -> ParseResult | None:
    """Validate arithmetic expansion contains only literals.

    """
    for child in node.children:
        if child is None:
            continue
        if not child.children:
            if not _RE_ARITHMETIC_LITERAL.match(_node_text(child)):
                return ParseResult(
                    kind="too-complex",
                    reason=(
                        f"Arithmetic expansion references variable or "
                        f"non-literal: {_node_text(child)}"
                    ),
                    node_type="arithmetic_expansion",
                )
            continue
        child_type = _get_text(child.type)
        if child_type in (
            "binary_expression", "unary_expression",
            "ternary_expression", "parenthesized_expression",
        ):
            result = _check_arithmetic(child)
            if result is not None:
                return result
        else:
            return _too_complex(child)
    return None


def _check_safe_cat_heredoc(node: Any) -> str | None:
    """Detect safe ``$(cat <<'EOF' ... EOF)`` pattern.

    Returns the heredoc content string if safe, "DANGEROUS" if the content
    contains dangerous patterns, or None if the pattern doesn't match.
    """
    redirected = None
    for child in node.children:
        if child is None:
            continue
        ctype = _get_text(child.type)
        if ctype in ("$(", "`", ")"):
            continue
        if ctype == "redirected_statement" and redirected is None:
            redirected = child
        else:
            return None
    if redirected is None:
        return None

    has_cat = False
    heredoc_body: str | None = None
    for child in redirected.children:
        if child is None:
            continue
        ctype = _get_text(child.type)
        if ctype == "command":
            named_children = [c for c in child.children if c]
            if len(named_children) != 1:
                return None
            first = named_children[0]
            if (
                _get_text(first.type) != "command_name"
                or _node_text(first) != "cat"
            ):
                return None
            has_cat = True
        elif ctype == "heredoc_redirect":
            # Must have a safe (quoted) delimiter.
            if _check_heredoc_redirect(child) is not None:
                return None
            for hchild in child.children:
                if hchild and _get_text(hchild.type) == "heredoc_body":
                    heredoc_body = _node_text(hchild)
        else:
            return None

    if not has_cat or heredoc_body is None:
        return None
    if _RE_HEREDOC_DANGEROUS.search(heredoc_body):
        return "DANGEROUS"
    if re.search(r"\bsystem\s*\(", heredoc_body):
        return "DANGEROUS"
    return heredoc_body


def _resolve_double_quoted_string(
    node: Any, commands: list[ParsedCommand], env: dict[str, str],
) -> str | ParseResult:
    """Resolve a double-quoted string node.

    """
    result = ""
    prev_end = -1
    has_placeholder = False
    has_literal = False

    for child in node.children:
        if child is None:
            continue
        # Handle gaps (newlines between children).
        if prev_end != -1 and child.start_byte > prev_end:
            result += "\n" * (child.start_byte - prev_end)
            has_literal = True

        ctype = _get_text(child.type)
        prev_end = child.end_byte

        if ctype == '"':
            prev_end = child.end_byte
            continue

        if ctype == "string_content":
            # Unescape only $, `, ", \.
            text = re.sub(r"\\([$`\"\\])", r"\1", _node_text(child))
            result += text
            has_literal = True
            continue

        # Dollar sign character ($).
        if ctype == "$":
            result += "$"
            has_literal = True
            continue

        if ctype == "command_substitution":
            cat_result = _check_safe_cat_heredoc(child)
            if cat_result == "DANGEROUS":
                return _too_complex(child)
            if cat_result is not None:
                # Strip trailing newlines (shell behavior).
                stripped = cat_result.rstrip("\n")
                if "\n" in stripped:
                    # Multi-line — skip adding to result but mark as literal.
                    has_literal = True
                    continue
                result += stripped
                has_literal = True
                continue

            # Validate command substitution contents.
            err = _check_command_substitution(child, commands, env)
            if err is not None:
                return err
            result += _PLACEHOLDER_CMD_SUB
            has_placeholder = True
            continue

        if ctype == "simple_expansion":
            resolved = _resolve_simple_expansion(child, env, True)
            if not isinstance(resolved, str):
                return resolved
            if resolved == _PLACEHOLDER_UNRESOLVED:
                has_placeholder = True
            else:
                has_literal = True
            result += resolved
            continue

        if ctype == "arithmetic_expansion":
            err = _check_arithmetic(child)
            if err is not None:
                return err
            result += _node_text(child)
            has_literal = True
            continue

        return _too_complex(child)

    # If we only have placeholders and no literal content, it's too complex.
    if has_placeholder and not has_literal:
        return _too_complex(node)
    # Empty string with length > 2 (more than just "") is suspicious.
    if not has_literal and not has_placeholder and len(_node_text(node)) > 2:
        return _too_complex(node)
    return result


def _resolve_value(
    node: Any, commands: list[ParsedCommand], env: dict[str, str],
) -> str | ParseResult:
    """Resolve a value from an AST node.

    Returns the resolved string value
    or a ParseResult if the value is too complex.
    """
    if node is None:
        return ParseResult(kind="too-complex", reason="Null argument node")

    ntype = _get_text(node.type)

    if ntype == "word":
        text = _node_text(node)
        if _RE_BRACE_EXPANSION.search(text):
            return ParseResult(
                kind="too-complex",
                reason="Word contains brace expansion syntax",
                node_type="word",
            )
        # Unescape backslash sequences.
        return re.sub(r"\\(.)", r"\1", text)

    if ntype == "number":
        if node.children:
            return ParseResult(
                kind="too-complex",
                reason=(
                    "Number node contains expansion "
                    "(NN# arithmetic base syntax)"
                ),
                node_type=(
                    _get_text(node.children[0].type)
                    if node.children[0] else None
                ),
            )
        return _node_text(node)

    if ntype == "raw_string":
        return _strip_raw_string(_node_text(node))

    if ntype == "string":
        return _resolve_double_quoted_string(node, commands, env)

    if ntype == "concatenation":
        text = _node_text(node)
        if _RE_BRACE_EXPANSION.search(text):
            return ParseResult(
                kind="too-complex",
                reason="Brace expansion",
                node_type="concatenation",
            )
        result = ""
        for child in node.children:
            if child is None:
                continue
            resolved = _resolve_value(child, commands, env)
            if not isinstance(resolved, str):
                return resolved
            result += resolved
        return result

    if ntype == "arithmetic_expansion":
        err = _check_arithmetic(node)
        if err is not None:
            return err
        return _node_text(node)

    if ntype == "simple_expansion":
        return _resolve_simple_expansion(node, env, False)

    return _too_complex(node)


# ---------------------------------------------------------------------------
# Variable assignment resolution
# ---------------------------------------------------------------------------


@dataclass
class _VarAssignment:
    """Internal result of resolving a variable assignment."""
    name: str
    value: str
    is_append: bool


def _resolve_var_assignment(
    node: Any, commands: list[ParsedCommand], env: dict[str, str],
) -> _VarAssignment | ParseResult:
    """Resolve a variable_assignment node.

    """
    var_name: str | None = None
    value = ""
    is_append = False

    for child in node.children:
        if child is None:
            continue
        ctype = _get_text(child.type)

        if ctype == "variable_name":
            var_name = _node_text(child)
        elif ctype in ("=", "+="):
            is_append = ctype == "+="
        elif ctype == "command_substitution":
            err = _check_command_substitution(child, commands, env)
            if err is not None:
                return err
            value = _PLACEHOLDER_CMD_SUB
        elif ctype == "simple_expansion":
            resolved = _resolve_simple_expansion(child, env, True)
            if not isinstance(resolved, str):
                return resolved
            value = resolved
        else:
            resolved = _resolve_value(child, commands, env)
            if not isinstance(resolved, str):
                return resolved
            value = resolved

    if var_name is None:
        return ParseResult(
            kind="too-complex",
            reason="Variable assignment without name",
            node_type="variable_assignment",
        )
    if not _RE_VALID_VAR_NAME.match(var_name):
        return ParseResult(
            kind="too-complex",
            reason=(
                f"Invalid variable name (bash treats as command): {var_name}"
            ),
            node_type="variable_assignment",
        )
    if var_name == "IFS":
        return ParseResult(
            kind="too-complex",
            reason=(
                "IFS assignment changes word-splitting "
                "\u2014 cannot model statically"
            ),
            node_type="variable_assignment",
        )
    if "~" in value:
        return ParseResult(
            kind="too-complex",
            reason=(
                "Tilde in assignment value "
                "\u2014 bash may expand at assignment time"
            ),
            node_type="variable_assignment",
        )

    return _VarAssignment(name=var_name, value=value, is_append=is_append)


def _apply_var_assignment(
    env: dict[str, str], assignment: _VarAssignment,
) -> None:
    """Apply a variable assignment to the environment.

    """
    existing = env.get(assignment.name, "")
    new_val = (
        existing + assignment.value if assignment.is_append
        else assignment.value
    )
    env[assignment.name] = (
        _PLACEHOLDER_UNRESOLVED if _has_placeholder(new_val) else new_val
    )


# ---------------------------------------------------------------------------
# Command substitution validation
# ---------------------------------------------------------------------------


def _check_command_substitution(
    node: Any, commands: list[ParsedCommand], env: dict[str, str],
) -> ParseResult | None:
    """Validate command substitution contents.

    Recursively walks the substitution body to ensure all commands within
    are safe.  Returns a ParseResult on failure, None on success.
    """
    child_env = dict(env)
    for child in node.children:
        if child is None:
            continue
        ctype = _get_text(child.type)
        if ctype in ("$(", "`", ")"):
            continue
        err = _walk_node(child, commands, child_env)
        if err is not None:
            return err
    return None


# ---------------------------------------------------------------------------
# Redirect resolution
# ---------------------------------------------------------------------------


def _resolve_redirect(
    node: Any, commands: list[ParsedCommand], env: dict[str, str],
) -> Redirect | ParseResult:
    """Resolve a file_redirect node.

    """
    op: str | None = None
    target: str | None = None
    fd: int | None = None

    for child in node.children:
        if child is None:
            continue
        ctype = _get_text(child.type)

        if ctype == "file_descriptor":
            fd = int(_node_text(child))
        elif ctype in _REDIRECT_OPS:
            op = _REDIRECT_OPS[ctype]
        elif ctype in ("word", "number"):
            if child.children:
                return _too_complex(child)
            text = _node_text(child)
            if _RE_BRACE_EXPANSION.search(text):
                return _too_complex(child)
            target = re.sub(r"\\(.)", r"\1", text)
        elif ctype == "raw_string":
            target = _strip_raw_string(_node_text(child))
        elif ctype == "string":
            resolved = _resolve_double_quoted_string(child, commands, env)
            if not isinstance(resolved, str):
                return resolved
            target = resolved
        elif ctype == "concatenation":
            resolved = _resolve_value(child, commands, env)
            if not isinstance(resolved, str):
                return resolved
            target = resolved
        else:
            return _too_complex(child)

    if op is None or target is None:
        return ParseResult(
            kind="too-complex",
            reason="Unrecognized redirect shape",
            node_type=_get_text(node.type),
        )
    return Redirect(op=op, target=target, fd=fd)


# ---------------------------------------------------------------------------
# Heredoc validation
# ---------------------------------------------------------------------------


def _check_heredoc_redirect(node: Any) -> ParseResult | None:
    """Validate a heredoc_redirect node.

    Returns None if safe, ParseResult if too complex.
    Requires quoted or escaped delimiter.
    """
    delimiter: str | None = None
    body_node: Any = None

    for child in node.children:
        if child is None:
            continue
        ctype = _get_text(child.type)
        if ctype == "heredoc_start":
            delimiter = _node_text(child)
        elif ctype == "heredoc_body":
            body_node = child
        elif ctype in ("<<", "<<-", "heredoc_end", "file_descriptor"):
            continue
        else:
            return _too_complex(child)

    # Delimiter must be quoted or backslash-escaped.
    if delimiter is None or not (
        (delimiter.startswith("'") and delimiter.endswith("'"))
        or (delimiter.startswith('"') and delimiter.endswith('"'))
        or delimiter.startswith("\\")
    ):
        return ParseResult(
            kind="too-complex",
            reason=(
                "Heredoc with unquoted delimiter undergoes shell expansion"
            ),
            node_type="heredoc_redirect",
        )

    # Validate body contains only heredoc_content nodes.
    if body_node is not None:
        for child in body_node.children:
            if child is None:
                continue
            if _get_text(child.type) != "heredoc_content":
                return _too_complex(child)

    return None


# ---------------------------------------------------------------------------
# Herestring validation
# ---------------------------------------------------------------------------


def _check_herestring_redirect(
    node: Any, commands: list[ParsedCommand], env: dict[str, str],
) -> ParseResult | None:
    """Validate a herestring_redirect (<<<) node.

    Returns None if safe, ParseResult if too complex.
    """
    for child in node.children:
        if child is None:
            continue
        if _get_text(child.type) == "<<<":
            continue
        resolved = _resolve_value(child, commands, env)
        if not isinstance(resolved, str):
            return resolved
        if _RE_HERESTRING_COMMENT.search(resolved):
            return _too_complex(child)
    return None


# ---------------------------------------------------------------------------
# Command node resolution
# ---------------------------------------------------------------------------


def _resolve_command_node(
    node: Any,
    parent_redirects: list[Redirect],
    commands: list[ParsedCommand],
    env: dict[str, str],
) -> ParseResult:
    """Resolve a ``command`` AST node into a ParsedCommand.

    """
    argv: list[str] = []
    env_vars: list[EnvVar] = []
    redirects = list(parent_redirects)

    for child in node.children:
        if child is None:
            continue
        ctype = _get_text(child.type)

        if ctype == "variable_assignment":
            result = _resolve_var_assignment(child, commands, env)
            if isinstance(result, ParseResult):
                return result
            env_vars.append(EnvVar(name=result.name, value=result.value))

        elif ctype == "command_name":
            first_child = child.children[0] if child.children else child
            resolved = _resolve_value(first_child, commands, env)
            if not isinstance(resolved, str):
                return resolved
            argv.append(resolved)

        elif ctype in (
            "word", "number", "raw_string", "string",
            "concatenation", "arithmetic_expansion",
        ):
            resolved = _resolve_value(child, commands, env)
            if not isinstance(resolved, str):
                return resolved
            argv.append(resolved)

        elif ctype == "simple_expansion":
            resolved = _resolve_simple_expansion(child, env, False)
            if not isinstance(resolved, str):
                return resolved
            argv.append(resolved)

        elif ctype == "file_redirect":
            result = _resolve_redirect(child, commands, env)
            if isinstance(result, ParseResult):
                return result
            redirects.append(result)

        elif ctype == "herestring_redirect":
            err = _check_herestring_redirect(child, commands, env)
            if err is not None:
                return err

        else:
            return _too_complex(child)

    # Reconstruct text for the command.
    node_text = _node_text(node)
    if re.search(r"\$[A-Za-z_]", node_text) or "\n" in node_text:
        # Re-quote arguments to avoid shell expansion in reconstructed text.
        parts: list[str] = []
        for arg in argv:
            if arg == "" or _RE_NEEDS_QUOTING.search(arg):
                parts.append("'" + arg.replace("'", "'\\''") + "'")
            else:
                parts.append(arg)
        text = " ".join(parts)
    else:
        text = node_text

    return ParseResult(
        kind="simple",
        commands=[ParsedCommand(
            argv=argv,
            env_vars=env_vars,
            redirects=redirects,
            text=text,
        )],
    )


# ---------------------------------------------------------------------------
# Test command resolution
# ---------------------------------------------------------------------------


def _resolve_test_expression(
    node: Any,
    argv: list[str],
    commands: list[ParsedCommand],
    env: dict[str, str],
) -> ParseResult | None:
    """Resolve a test expression node.

    """
    ntype = _get_text(node.type)

    if ntype in (
        "unary_expression", "binary_expression",
        "negated_expression", "parenthesized_expression",
    ):
        for child in node.children:
            if child is None:
                continue
            err = _resolve_test_expression(child, argv, commands, env)
            if err is not None:
                return err
        return None

    if ntype in (
        "test_operator", "!", "(", ")", "&&", "||",
        "==", "=", "!=", "<", ">", "=~",
    ):
        argv.append(_node_text(node))
        return None

    # Default: resolve as a value.
    resolved = _resolve_value(node, commands, env)
    if not isinstance(resolved, str):
        return resolved
    argv.append(resolved)
    return None


# ---------------------------------------------------------------------------
# Redirected statement handling
# ---------------------------------------------------------------------------


def _walk_redirected_statement(
    node: Any, commands: list[ParsedCommand], env: dict[str, str],
) -> ParseResult | None:
    """Walk a redirected_statement node.

    """
    redirects: list[Redirect] = []
    inner_node: Any = None

    for child in node.children:
        if child is None:
            continue
        ctype = _get_text(child.type)

        if ctype == "file_redirect":
            result = _resolve_redirect(child, commands, env)
            if isinstance(result, ParseResult):
                return result
            redirects.append(result)
        elif ctype == "heredoc_redirect":
            err = _check_heredoc_redirect(child)
            if err is not None:
                return err
        elif ctype in (
            "command", "pipeline", "list", "negated_command",
            "declaration_command", "unset_command",
        ):
            inner_node = child
        else:
            return _too_complex(child)

    if inner_node is None:
        # Redirect-only statement (no command).
        commands.append(ParsedCommand(
            argv=[],
            redirects=redirects,
            text=_node_text(node),
        ))
        return None

    prev_len = len(commands)
    err = _walk_node(inner_node, commands, env)
    if err is not None:
        return err
    # Attach redirects to the last command added.
    if len(commands) > prev_len and redirects:
        commands[-1].redirects.extend(redirects)
    return None


# ---------------------------------------------------------------------------
# Main AST walker
# ---------------------------------------------------------------------------


def _walk_node(
    node: Any, commands: list[ParsedCommand], env: dict[str, str],
) -> ParseResult | None:
    """Recursively walk the AST and collect ParsedCommand objects.

    Returns a ParseResult on failure (too-complex), or None on success
    (commands are appended to the ``commands`` list).

    """
    ntype = _get_text(node.type)

    # --- command ---
    if ntype == "command":
        result = _resolve_command_node(node, [], commands, env)
        if result.kind != "simple":
            return result
        commands.extend(result.commands)
        return None

    # --- redirected_statement ---
    if ntype == "redirected_statement":
        return _walk_redirected_statement(node, commands, env)

    # --- comment ---
    if ntype == "comment":
        return None

    # --- compound/container types (program, list, pipeline) ---
    if ntype in _COMPOUND_TYPES:
        is_pipeline = ntype == "pipeline"

        # Check for || or & operators which reset the env context.
        has_fallible_op = False
        if not is_pipeline:
            for child in node.children:
                if child and _get_text(child.type) in ("||", "&"):
                    has_fallible_op = True
                    break

        snapshot = dict(env) if has_fallible_op else None
        current_env = dict(env) if is_pipeline else env

        for child in node.children:
            if child is None:
                continue
            ctype = _get_text(child.type)

            if ctype in _OPERATOR_TYPES:
                # After ||, |, |&, or &, reset env to pre-operator snapshot.
                if ctype in ("||", "|", "|&", "&"):
                    current_env = dict(snapshot if snapshot else env)
                continue

            err = _walk_node(child, commands, current_env)
            if err is not None:
                return err

        return None

    # --- negated_command ---
    if ntype == "negated_command":
        for child in node.children:
            if child is None:
                continue
            if _get_text(child.type) == "!":
                continue
            return _walk_node(child, commands, env)
        return None

    # --- declaration_command (export, declare, etc.) ---
    if ntype == "declaration_command":
        argv: list[str] = []
        for child in node.children:
            if child is None:
                continue
            ctype = _get_text(child.type)

            if ctype in (
                "export", "local", "readonly", "declare", "typeset",
            ):
                argv.append(_node_text(child))
            elif ctype in (
                "word", "number", "raw_string", "string", "concatenation",
            ):
                resolved = _resolve_value(child, commands, env)
                if not isinstance(resolved, str):
                    return resolved
                # Check for dangerous declare flags.
                if argv and argv[0] in ("declare", "typeset", "local"):
                    if re.match(r"^-[a-zA-Z]*[niaA]", resolved):
                        return ParseResult(
                            kind="too-complex",
                            reason=(
                                f"declare flag {resolved} changes assignment "
                                f"semantics (nameref/integer/array)"
                            ),
                            node_type="declaration_command",
                        )
                    if (
                        not resolved.startswith("-")
                        and re.search(r"^[^=]*\[", resolved)
                    ):
                        return ParseResult(
                            kind="too-complex",
                            reason=(
                                f"declare positional '{resolved}' contains "
                                f"array subscript \u2014 bash evaluates "
                                f"$(cmd) in subscripts"
                            ),
                            node_type="declaration_command",
                        )
                argv.append(resolved)
            elif ctype == "variable_assignment":
                result = _resolve_var_assignment(child, commands, env)
                if isinstance(result, ParseResult):
                    return result
                _apply_var_assignment(env, result)
                argv.append(f"{result.name}={result.value}")
            elif ctype == "variable_name":
                argv.append(_node_text(child))
            else:
                return _too_complex(child)

        commands.append(ParsedCommand(
            argv=argv,
            redirects=[],
            text=_node_text(node),
        ))
        return None

    # --- variable_assignment ---
    if ntype == "variable_assignment":
        result = _resolve_var_assignment(node, commands, env)
        if isinstance(result, ParseResult):
            return result
        _apply_var_assignment(env, result)
        return None

    # --- for_statement ---
    if ntype == "for_statement":
        var_name: str | None = None
        do_group: Any = None
        for child in node.children:
            if child is None:
                continue
            ctype = _get_text(child.type)
            if ctype == "variable_name":
                var_name = _node_text(child)
            elif ctype == "do_group":
                do_group = child
            elif ctype in ("for", "in", "select", ";"):
                continue
            elif ctype == "command_substitution":
                err = _check_command_substitution(child, commands, env)
                if err is not None:
                    return err
            else:
                resolved = _resolve_value(child, commands, env)
                if not isinstance(resolved, str):
                    return resolved

        if var_name is None or do_group is None:
            return _too_complex(node)

        # The loop variable gets an unresolvable value.
        env[var_name] = _PLACEHOLDER_UNRESOLVED

        # Walk the do_group body with a fresh env snapshot.
        body_env = dict(env)
        for child in do_group.children:
            if child is None:
                continue
            ctype = _get_text(child.type)
            if ctype in ("do", "done", ";"):
                continue
            err = _walk_node(child, commands, body_env)
            if err is not None:
                return err
        return None

    # --- if_statement / while_statement ---
    if ntype in ("if_statement", "while_statement"):
        in_then = False
        for child in node.children:
            if child is None:
                continue
            ctype = _get_text(child.type)

            if ctype in (
                "if", "fi", "else", "elif", "while", "until", ";",
            ):
                continue

            if ctype == "then":
                in_then = True
                continue

            if ctype == "do_group":
                body_env = dict(env)
                for bchild in child.children:
                    if bchild is None:
                        continue
                    if _get_text(bchild.type) in ("do", "done", ";"):
                        continue
                    err = _walk_node(bchild, commands, body_env)
                    if err is not None:
                        return err
                continue

            if ctype in ("elif_clause", "else_clause"):
                clause_env = dict(env)
                for cchild in child.children:
                    if cchild is None:
                        continue
                    if _get_text(cchild.type) in (
                        "elif", "else", "then", ";",
                    ):
                        continue
                    err = _walk_node(cchild, commands, clause_env)
                    if err is not None:
                        return err
                continue

            # Condition or body.
            cond_env = dict(env) if in_then else env
            prev_len = len(commands)
            err = _walk_node(child, commands, cond_env)
            if err is not None:
                return err

            # Track read commands in conditions — 'read VAR' may not
            # execute, so we can't prove it overwrites tracked values.
            if not in_then:
                for cmd in commands[prev_len:]:
                    if cmd.argv and cmd.argv[0] == "read":
                        for arg in cmd.argv[1:]:
                            if (
                                not arg.startswith("-")
                                and _RE_VALID_VAR_NAME.match(arg)
                            ):
                                existing = env.get(arg)
                                if (
                                    existing is not None
                                    and not _has_placeholder(existing)
                                ):
                                    return ParseResult(
                                        kind="too-complex",
                                        reason=(
                                            f"'read {arg}' in condition may "
                                            f"not execute (||/pipeline/"
                                            f"subshell); cannot prove it "
                                            f"overwrites tracked literal "
                                            f"'{existing}'"
                                        ),
                                        node_type="if_statement",
                                    )
                                env[arg] = _PLACEHOLDER_UNRESOLVED
        return None

    # --- subshell ---
    if ntype == "subshell":
        subshell_env = dict(env)
        for child in node.children:
            if child is None:
                continue
            if _get_text(child.type) in ("(", ")"):
                continue
            err = _walk_node(child, commands, subshell_env)
            if err is not None:
                return err
        return None

    # --- test_command ---
    if ntype == "test_command":
        test_argv: list[str] = ["[["]
        for child in node.children:
            if child is None:
                continue
            ctype = _get_text(child.type)
            if ctype in ("[[", "]]", "[", "]"):
                continue
            err = _resolve_test_expression(child, test_argv, commands, env)
            if err is not None:
                return err
        commands.append(ParsedCommand(
            argv=test_argv,
            redirects=[],
            text=_node_text(node),
        ))
        return None

    # --- unset_command ---
    if ntype == "unset_command":
        unset_argv: list[str] = []
        for child in node.children:
            if child is None:
                continue
            ctype = _get_text(child.type)
            if ctype == "unset":
                unset_argv.append(_node_text(child))
            elif ctype == "variable_name":
                vname = _node_text(child)
                unset_argv.append(vname)
                env.pop(vname, None)
            elif ctype == "word":
                resolved = _resolve_value(child, commands, env)
                if not isinstance(resolved, str):
                    return resolved
                unset_argv.append(resolved)
            else:
                return _too_complex(child)
        commands.append(ParsedCommand(
            argv=unset_argv,
            redirects=[],
            text=_node_text(node),
        ))
        return None

    return _too_complex(node)


# ---------------------------------------------------------------------------
# Post-parse validation
# ---------------------------------------------------------------------------


def _validate_commands(commands: list[ParsedCommand]) -> tuple[bool, str]:
    """Validate parsed commands for safety.

    Strips wrapper prefixes (time, nohup, timeout, nice, env, stdbuf)
    and checks for dangerous patterns.

    Returns (ok, reason).
    """
    for cmd in commands:
        argv = list(cmd.argv)

        # Strip wrapper command prefixes.
        while True:
            if not argv:
                break
            if argv[0] in ("time", "nohup"):
                argv = argv[1:]
            elif argv[0] == "timeout":
                i = 1
                while i < len(argv):
                    arg = argv[i]
                    if arg in (
                        "--foreground", "--preserve-status", "--verbose",
                    ):
                        i += 1
                    elif re.match(
                        r"^--(?:kill-after|signal)=[A-Za-z0-9_.+-]+$", arg,
                    ):
                        i += 1
                    elif (
                        arg in ("--kill-after", "--signal")
                        and i + 1 < len(argv)
                        and re.match(
                            r"^[A-Za-z0-9_.+-]+$", argv[i + 1],
                        )
                    ):
                        i += 2
                    elif arg.startswith("--"):
                        return (
                            False,
                            f"timeout with {arg} flag cannot be "
                            f"statically analyzed",
                        )
                    elif arg == "-v":
                        i += 1
                    elif (
                        arg in ("-k", "-s")
                        and i + 1 < len(argv)
                        and re.match(
                            r"^[A-Za-z0-9_.+-]+$", argv[i + 1],
                        )
                    ):
                        i += 2
                    elif re.match(r"^-[ks][A-Za-z0-9_.+-]+$", arg):
                        i += 1
                    elif arg.startswith("-"):
                        return (
                            False,
                            f"timeout with {arg} flag cannot be "
                            f"statically analyzed",
                        )
                    else:
                        break
                if (
                    i < len(argv)
                    and re.match(r"^\d+(?:\.\d+)?[smhd]?$", argv[i])
                ):
                    argv = argv[i + 1:]
                elif i < len(argv):
                    return (
                        False,
                        f"timeout duration '{argv[i]}' cannot be "
                        f"statically analyzed",
                    )
                else:
                    break
            elif argv[0] == "nice":
                if (
                    len(argv) > 2
                    and argv[1] == "-n"
                    and re.match(r"^-?\d+$", argv[2])
                ):
                    argv = argv[3:]
                elif (
                    len(argv) > 1
                    and re.match(r"^-\d+$", argv[1])
                ):
                    argv = argv[2:]
                elif (
                    len(argv) > 1
                    and re.search(r"[$(`]", argv[1])
                ):
                    return (
                        False,
                        f"nice argument '{argv[1]}' contains expansion "
                        f"\u2014 cannot statically determine wrapped command",
                    )
                else:
                    argv = argv[1:]
            elif argv[0] == "env":
                i = 1
                while i < len(argv):
                    arg = argv[i]
                    if "=" in arg and not arg.startswith("-"):
                        i += 1
                    elif arg in ("-i", "-0", "-v"):
                        i += 1
                    elif arg == "-u" and i + 1 < len(argv):
                        i += 2
                    elif arg.startswith("-"):
                        return (
                            False,
                            f"env with {arg} flag cannot be "
                            f"statically analyzed",
                        )
                    else:
                        break
                if i < len(argv):
                    argv = argv[i:]
                else:
                    break
            elif argv[0] == "stdbuf":
                i = 1
                while i < len(argv):
                    arg = argv[i]
                    if (
                        re.match(r"^-[ioe]$", arg)
                        and i + 1 < len(argv)
                    ):
                        i += 2
                    elif re.match(r"^-[ioe].", arg):
                        i += 1
                    elif re.match(r"^--(input|output|error)=", arg):
                        i += 1
                    elif arg.startswith("-"):
                        return (
                            False,
                            f"stdbuf with {arg} flag cannot be "
                            f"statically analyzed",
                        )
                    else:
                        break
                if i > 1 and i < len(argv):
                    argv = argv[i:]
                else:
                    break
            else:
                break

        # Validate the unwrapped command.
        cmd_name = argv[0] if argv else None
        if cmd_name is None:
            continue
        if cmd_name == "":
            return (
                False,
                "Empty command name \u2014 argv[0] may not reflect "
                "what bash runs",
            )
        if _has_placeholder(cmd_name):
            return (
                False,
                "Command name is runtime-determined (placeholder argv[0])",
            )
        if (
            cmd_name.startswith("-")
            or cmd_name.startswith("|")
            or cmd_name.startswith("&")
        ):
            return (
                False,
                "Command appears to be an incomplete fragment",
            )

        # Check for array subscript attacks in flag arguments.
        danger_flags = _SUBSCRIPT_DANGER_FLAGS.get(cmd_name)
        if danger_flags is not None:
            for i in range(1, len(argv)):
                arg = argv[i]
                # Direct flag match.
                if arg in danger_flags and (
                    i + 1 < len(argv)
                    and "[" in argv[i + 1]
                ):
                    return (
                        False,
                        f"'{cmd_name} {arg}' operand contains array "
                        f"subscript \u2014 bash evaluates $(cmd) in "
                        f"subscripts",
                    )
                # Combined short flags (e.g. -vR).
                if (
                    len(arg) > 2
                    and arg[0] == "-"
                    and arg[1] != "-"
                    and "[" not in arg
                ):
                    for flag in danger_flags:
                        if (
                            len(flag) == 2
                            and flag[1] in arg
                            and i + 1 < len(argv)
                            and "[" in argv[i + 1]
                        ):
                            return (
                                False,
                                f"'{cmd_name} {flag}' (combined in '{arg}') "
                                f"operand contains array subscript \u2014 "
                                f"bash evaluates $(cmd) in subscripts",
                            )
                # Fused flag+value (e.g. -vfoo[0]).
                for flag in danger_flags:
                    if (
                        len(flag) == 2
                        and arg.startswith(flag)
                        and len(arg) > 2
                        and "[" in arg
                    ):
                        return (
                            False,
                            f"'{cmd_name} {flag}' (fused) operand contains "
                            f"array subscript \u2014 bash evaluates $(cmd) "
                            f"in subscripts",
                        )

        # Check positional arguments for subscript attacks (read, unset).
        if cmd_name in _SUBSCRIPT_DANGER_COMMANDS:
            skip_next = False
            for i in range(1, len(argv)):
                arg = argv[i]
                if skip_next:
                    skip_next = False
                    continue
                if arg.startswith("-"):
                    if cmd_name == "read":
                        if arg in _READ_ARG_FLAGS:
                            skip_next = True
                        elif len(arg) > 2 and arg[1] != "-":
                            for j in range(1, len(arg)):
                                if f"-{arg[j]}" in _READ_ARG_FLAGS:
                                    if j == len(arg) - 1:
                                        skip_next = True
                                    break
                    continue
                if "[" in arg:
                    return (
                        False,
                        f"'{cmd_name}' positional NAME '{arg}' contains "
                        f"array subscript \u2014 bash evaluates $(cmd) "
                        f"in subscripts",
                    )

        # Shell keywords as command name = tree-sitter mis-parse.
        if cmd_name in _SHELL_KEYWORDS:
            return (
                False,
                f"Shell keyword '{cmd_name}' as command name "
                f"\u2014 tree-sitter mis-parse",
            )

        # jq-specific checks.
        if cmd_name == "jq":
            for arg in argv:
                if re.search(r"\bsystem\s*\(", arg):
                    return (
                        False,
                        "jq command contains system() function which "
                        "executes arbitrary commands",
                    )
            if any(
                _RE_JQ_DANGEROUS_FLAGS.match(arg) for arg in argv
            ):
                return (
                    False,
                    "jq command contains dangerous flags that could "
                    "execute code or read arbitrary files",
                )

        # Zsh-specific dangerous builtins.
        if cmd_name in _ZSH_DANGEROUS_BUILTINS:
            return (
                False,
                f"Zsh builtin '{cmd_name}' can bypass security checks",
            )

        # Dangerous builtins (eval, source, exec, etc.) with exceptions.
        if cmd_name in _DANGEROUS_BUILTINS:
            # command -v / -V is safe (lookup only).
            if cmd_name == "command" and len(argv) > 1 and argv[1] in (
                "-v", "-V",
            ):
                pass
            # fc without -e or -s flags is safe (history listing).
            elif cmd_name == "fc" and not any(
                re.match(r"^-[^-]*[es]", a) for a in argv[1:]
            ):
                pass
            # compgen without -C, -F, -W is safe.
            elif cmd_name == "compgen" and not any(
                re.match(r"^-[^-]*[CFW]", a) for a in argv[1:]
            ):
                pass
            else:
                return (
                    False,
                    f"'{cmd_name}' evaluates arguments as shell code",
                )

        # Newline+# pattern in argv (can hide arguments from validation).
        for arg in cmd.argv:
            if "\n" in arg and _RE_HERESTRING_COMMENT.search(arg):
                return (
                    False,
                    "Newline followed by # inside an argument can hide "
                    "arguments from path validation",
                )

        # Newline+# pattern in env var values.
        for ev in cmd.env_vars:
            if "\n" in ev.value and _RE_HERESTRING_COMMENT.search(ev.value):
                return (
                    False,
                    "Newline followed by # inside an env var value can "
                    "hide arguments from path validation",
                )

        # Newline+# pattern in redirect targets.
        for redir in cmd.redirects:
            if (
                "\n" in redir.target
                and _RE_HERESTRING_COMMENT.search(redir.target)
            ):
                return (
                    False,
                    "Newline followed by # inside a redirect target can "
                    "hide arguments from path validation",
                )

        # /proc/*/environ access in argv.
        for arg in cmd.argv:
            if "/proc/" in arg and _RE_HEREDOC_DANGEROUS.search(arg):
                return (
                    False,
                    "Accesses /proc/*/environ which may expose secrets",
                )

        # /proc/*/environ access in redirect targets.
        for redir in cmd.redirects:
            if (
                "/proc/" in redir.target
                and _RE_HEREDOC_DANGEROUS.search(redir.target)
            ):
                return (
                    False,
                    "Accesses /proc/*/environ which may expose secrets",
                )

    return (True, "")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_command(command: str) -> ParseResult:
    """Parse a bash command string into structured ParsedCommand objects.

    This is the main entry point for command parsing.

    Returns a ParseResult with kind="simple" containing structured
    ParsedCommand objects, or kind="too-complex" if the command cannot
    be safely analyzed.
    """
    cmd = command.strip()
    if not cmd:
        return ParseResult(kind="simple", commands=[])

    # Pre-parse security checks.
    try:
        check_pre_parse_security(cmd)
    except TooComplexError as e:
        return ParseResult(
            kind="too-complex", reason=e.reason, node_type=e.node_type,
        )

    # Parse with tree-sitter.
    parser = _get_parser()
    tree = parser.parse(cmd.encode())
    root = tree.root_node

    # Walk the AST and collect commands.
    commands: list[ParsedCommand] = []
    err = _walk_node(root, commands, {})
    if err is not None:
        return err

    # Post-parse validation.
    ok, reason = _validate_commands(commands)
    if not ok:
        return ParseResult(kind="too-complex", reason=reason)

    return ParseResult(kind="simple", commands=commands)


def split_subcommands(command: str) -> list[str]:
    """Split a bash command into individual subcommands using tree-sitter.

    This is the legacy API used by hooks.py.  When parse_command returns
    "simple", subcommand text strings are extracted from the structured
    commands.  When it returns "too-complex", TooComplexError is raised.
    """
    result = parse_command(command)
    if result.kind == "too-complex":
        raise TooComplexError(
            result.reason or "Command is too complex",
            result.node_type,
        )
    return [cmd.text for cmd in result.commands]


def is_compound_command(command: str) -> bool:
    """Return True if *command* contains multiple subcommands or pipes.

    Uses tree-sitter for accurate detection that respects quoting.
    Splits on ``&&``, ``||``, ``;``, and ``|`` — so pipelines are also
    considered compound for the purpose of prefix rule matching.

    Returns True on parse failure (safe default).
    """
    cmd = command.strip()
    if not cmd:
        return False
    try:
        check_pre_parse_security(cmd)
        parser = _get_parser()
        tree = parser.parse(cmd.encode())
        root = tree.root_node
        # Use the new structured parser to count commands.
        commands: list[ParsedCommand] = []
        err = _walk_node(root, commands, {})
        if err is not None:
            return True
        if len(commands) > 1:
            return True
        return _has_pipeline(root)
    except TooComplexError:
        return True


def _has_pipeline(node: Any) -> bool:
    """Return True if the AST contains a pipeline node."""
    if _get_text(node.type) == "pipeline":
        return True
    for child in node.children:
        if _has_pipeline(child):
            return True
    return False


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
