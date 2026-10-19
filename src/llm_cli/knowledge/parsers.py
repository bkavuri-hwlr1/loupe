"""Small deterministic source parsers. No execution, imports, or dependencies."""

from __future__ import annotations

import ast
import posixpath
import re
import sys
from dataclasses import dataclass
from pathlib import PurePosixPath

VERSION = "code-graph-v1"
PYTHON_VERSION = f"{VERSION}/python-{sys.version_info.major}.{sys.version_info.minor}"
PROJECTION_VERSION = PYTHON_VERSION
MAX_ITEMS = 10_000
MAX_TOKENS = 262_144
EDGE_TYPES = ("imports", "tests", "defines", "changed-by")


@dataclass(frozen=True, slots=True)
class Node:
    key: str
    kind: str
    name: str
    line: int


@dataclass(frozen=True, slots=True)
class Edge:
    source: str
    target: str
    kind: str
    line: int


@dataclass(frozen=True, slots=True)
class Parsed:
    parser: str
    nodes: tuple[Node, ...]
    aliases: tuple[tuple[str, str], ...]
    edges: tuple[Edge, ...]
    error: str | None = None


def python_module(path: str) -> str:
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if len(parts) > 1 and parts[0] == "src":
        parts.pop(0)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def is_test(path: str) -> bool:
    item = PurePosixPath(path)
    return (
        any(part in {"test", "tests", "__tests__"} for part in item.parts[:-1])
        or item.stem.startswith("test_")
        or item.stem.endswith(("_test", ".test", ".spec"))
    )


def parse(path: str, text: str) -> Parsed:
    file_node = Node("file:" + path, "file", path, 1)
    suffix = PurePosixPath(path).suffix.lower()
    parser = "python-ast" if suffix in {".py", ".pyi"} else "file"
    if suffix in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}:
        parser = "javascript-tokens"
    try:
        if parser == "python-ast":
            return _python(path, text, file_node)
        if parser == "javascript-tokens":
            return _javascript(path, text, file_node)
        return Parsed(parser, (file_node,), (), ())
    except (SyntaxError, ValueError, RecursionError) as exc:
        # Exception text can quote source. Only a bounded category is persisted.
        return Parsed(parser, (file_node,), (), (), type(exc).__name__)


_PYTHON_STATEMENTS = (
    ast.ClassDef,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Import,
    ast.ImportFrom,
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    ast.TryStar,
    ast.Match,
)


def _python_children(item: ast.AST) -> tuple[ast.stmt, ...]:
    """Visit statement containers, never argument/annotation/expression trees.

    Python expressions cannot contain a definition or import statement. Handler
    and match-case bodies are flattened in AST field order, preserving the
    original fact order and the lexical scope carried by the traversal stack.
    """
    bodies: list[list[ast.stmt]]
    if isinstance(item, (ast.Try, ast.TryStar)):
        bodies = [
            item.body,
            *(handler.body for handler in item.handlers),
            item.orelse,
            item.finalbody,
        ]
    elif isinstance(item, ast.Match):
        bodies = [case.body for case in item.cases]
    elif isinstance(item, (ast.If, ast.For, ast.AsyncFor, ast.While)):
        bodies = [item.body, item.orelse]
    elif isinstance(
        item,
        (
            ast.Module,
            ast.ClassDef,
            ast.FunctionDef,
            ast.AsyncFunctionDef,
            ast.With,
            ast.AsyncWith,
        ),
    ):
        bodies = [item.body]
    else:
        return ()
    return tuple(
        child
        for body in bodies
        for child in body
        if isinstance(child, _PYTHON_STATEMENTS)
    )


def _python(path: str, text: str, file_node: Node) -> Parsed:
    tree = ast.parse(text, filename="<indexed source>")
    module = python_module(path)
    module_node = Node("module:" + path, "module", module, 1)
    nodes = [file_node, module_node]
    aliases = [("python:" + module, file_node.key)]
    edges = [Edge(file_node.key, module_node.key, "defines", 1)]
    package = (
        module if PurePosixPath(path).stem == "__init__" else module.rpartition(".")[0]
    )
    stack: list[tuple[ast.AST, str, str]] = [(tree, "", module_node.key)]
    while stack:
        item, qualified, parent = stack.pop()
        if isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            qualified = qualified + "." + item.name if qualified else item.name
            kind = "class" if isinstance(item, ast.ClassDef) else "function"
            key = f"{kind}:{path}::{qualified}@{item.lineno}"
            nodes.append(Node(key, kind, qualified, item.lineno))
            aliases.append((f"python-symbol:{module}.{qualified}", key))
            aliases.append((f"python:{module}.{qualified}", key))
            edges.append(Edge(parent, key, "defines", item.lineno))
            parent = key
        if isinstance(item, ast.Import):
            targets = [alias.name for alias in item.names]
        elif isinstance(item, ast.ImportFrom):
            if item.level:
                parts = package.split(".") if package else []
                if item.level > len(parts):
                    targets = []
                else:
                    prefix = ".".join(parts[: len(parts) - item.level + 1])
                    target = ".".join(part for part in (prefix, item.module) if part)
                    targets = [target] if target else []
            else:
                targets = [item.module] if item.module else []
            # `from package import module` may refer to a submodule; retain both
            # facts. Alias resolution only connects a submodule that exists.
            targets += [
                target + "." + alias.name
                for target in targets[:]
                for alias in item.names
                if alias.name != "*"
            ]
        else:
            targets = []
        for target in targets:
            edges.append(
                Edge(
                    file_node.key,
                    "python:" + target,
                    "imports",
                    getattr(item, "lineno", 1),
                )
            )
            if is_test(path):
                edges.append(
                    Edge(
                        file_node.key,
                        "python:" + target,
                        "tests",
                        getattr(item, "lineno", 1),
                    )
                )
        if len(nodes) + len(edges) > MAX_ITEMS:
            raise ValueError("source graph exceeds its item budget")
        stack.extend(
            (child, qualified, parent) for child in reversed(_python_children(item))
        )
    return Parsed("python-ast", tuple(nodes), tuple(aliases), tuple(edges))


_TOKEN = re.compile(
    r"(?P<space>\s+)|(?P<comment>//[^\n]*|/\*[\s\S]*?\*/)"
    r"|(?P<string>'(?:\\[^\r\n]|[^'\\\r\n])*'|\"(?:\\[^\r\n]|[^\"\\\r\n])*\")"
    r"|(?P<template>`(?:\\[\s\S]|[^`\\])*`)"
    r"|(?P<identifier>[$\w]+)"
    r"|(?P<operator>\?\?=|&&=|\|\|=|\*\*=|>>>=|===|!==|>>>|<<=|>>=|=>"
    r"|\+\+|--|\?\?|\|\||&&|\?\.|\*\*|==|!=|<=|>=|<<|>>|[+\-*/%&|^]=)"
    r"|(?P<punct>[^\s])",
    re.UNICODE,
)


def _regex_end(text: str, position: int) -> int | None:
    """Skip a regex literal in expression-start positions, including classes."""
    cursor, in_class = position + 1, False
    while cursor < len(text) and text[cursor] not in "\r\n":
        value = text[cursor]
        if value == "\\":
            if cursor + 1 == len(text) or text[cursor + 1] in "\r\n":
                return None
            cursor += 2
            continue
        if value == "[":
            in_class = True
        elif value == "]":
            in_class = False
        elif value == "/" and not in_class:
            cursor += 1
            while cursor < len(text) and text[cursor].isalpha():
                cursor += 1
            return cursor
        cursor += 1
    return None


def _tokens(text: str) -> list[tuple[str, str, int]]:
    tokens: list[tuple[str, str, int]] = []
    line, position = 1, 0
    expression_start = True
    parens: list[bool] = []
    prefix_words = {
        "return",
        "throw",
        "case",
        "yield",
        "await",
        "delete",
        "void",
        "typeof",
        "new",
        "in",
        "instanceof",
        "of",
        "else",
        "do",
    }
    while position < len(text):
        if text.startswith("/*", position) and text.find("*/", position + 2) == -1:
            raise SyntaxError("unterminated comment")
        previous = tokens[-1][1] if tokens else ""
        if (
            text[position] == "/"
            and expression_start
            and not text.startswith(("//", "/*"), position)
        ):
            end = _regex_end(text, position)
            if end is None:
                raise SyntaxError("unterminated regex literal")
            # Keep a literal placeholder: a following slash is division, and
            # must not inherit the context before this regex.
            tokens.append(("literal", "", line))
            expression_start = False
            position = end
        else:
            match = _TOKEN.match(text, position)
            assert match is not None
            kind, value = str(match.lastgroup), match.group()
            if kind == "punct" and value in {"'", '"', "`"}:
                raise SyntaxError("unterminated literal")
            if kind not in {"space", "comment"}:
                if kind == "template":
                    kind, value = "literal", ""
                if value == "(":
                    property_access = len(tokens) > 1 and tokens[-2][1] in {".", "?."}
                    parens.append(
                        previous in {"if", "while", "for", "with", "switch", "catch"}
                        and not property_access
                    )
                    expression_start = True
                elif value == ")":
                    expression_start = parens.pop() if parens else False
                elif kind == "identifier":
                    expression_start = value in prefix_words and previous not in {
                        ".",
                        "?.",
                    }
                elif kind in {"string", "literal"} or value in {
                    "]",
                    "}",
                    "++",
                    "--",
                    ".",
                    "?.",
                }:
                    expression_start = False
                else:
                    expression_start = True
                tokens.append((kind, value, line))
            line += match.group().count("\n")
            position = match.end()
        if len(tokens) > MAX_TOKENS:
            raise ValueError("source graph exceeds its token budget")
    return tokens


def _braces(tokens: list[tuple[str, str, int]]) -> dict[int, int]:
    """Match named-clause braces once, avoiding repeated scans of partial code."""
    stack: list[int] = []
    pairs: dict[int, int] = {}
    for number, (_, value, _) in enumerate(tokens):
        if value == "{":
            stack.append(number)
        elif value == "}" and stack:
            pairs[stack.pop()] = number
    return pairs


def _from_specifier(
    tokens: list[tuple[str, str, int]],
    number: int,
    keyword: str,
    braces: dict[int, int],
) -> str | None:
    """Recognize only an import clause or an actual re-export clause.

    Named declarations may span the entire bounded source file. A matching brace
    lookup bounds that work without guessing a fixed lookahead or scanning into
    an unrelated semicolonless statement.
    """
    cursor = number + 1
    size = len(tokens)
    if cursor >= size:
        return None
    if tokens[cursor][1] == "type" and cursor + 1 < size:
        following = tokens[cursor + 1]
        if following[1] in {"{", "*"} or (
            keyword == "import"
            and following[0] == "identifier"
            and following[1] != "from"
        ):
            cursor += 1
    if keyword == "import" and tokens[cursor][0] == "identifier":
        cursor += 1  # Default binding, optionally followed by a named/namespace clause.
        if cursor < size and tokens[cursor][1] == ",":
            cursor += 1
        else:
            return (
                tokens[cursor + 1][1]
                if cursor + 1 < size
                and tokens[cursor][1] == "from"
                and tokens[cursor + 1][0] == "string"
                else None
            )
    if cursor >= size:
        return None
    if tokens[cursor][1] == "{":
        end = braces.get(cursor)
        if end is None:
            return None
        cursor = end + 1
    elif tokens[cursor][1] == "*":
        cursor += 1
        if (
            cursor + 1 < size
            and tokens[cursor][1] == "as"
            and tokens[cursor + 1][0] == "identifier"
        ):
            cursor += 2
        elif keyword == "import":
            return None
    else:
        return None  # export const/default/class/function is not a re-export.
    return (
        tokens[cursor + 1][1]
        if cursor + 1 < size
        and tokens[cursor][1] == "from"
        and tokens[cursor + 1][0] == "string"
        else None
    )


def _javascript(path: str, text: str, file_node: Node) -> Parsed:
    tokens = _tokens(text)
    braces = _braces(tokens)
    aliases = [("javascript:" + path, file_node.key)]
    base = str(PurePosixPath(path).with_suffix(""))
    aliases.append(("javascript:" + base, file_node.key))
    if PurePosixPath(path).suffix in {".ts", ".tsx", ".mts", ".cts"}:
        extension = {".mts": ".mjs", ".cts": ".cjs"}.get(
            PurePosixPath(path).suffix, ".js"
        )
        aliases.append(("javascript:" + base + extension, file_node.key))
    if PurePosixPath(path).stem == "index":
        aliases.append(("javascript:" + str(PurePosixPath(path).parent), file_node.key))
    edges: list[Edge] = []
    for number, (kind, value, line) in enumerate(tokens):
        if kind != "identifier" or value not in {"import", "require", "export"}:
            continue
        if number and tokens[number - 1][1] in {".", "?."}:
            continue
        literal: str | None = None
        cursor = number + 1
        if cursor < len(tokens) and tokens[cursor][0] == "string" and value == "import":
            literal = tokens[cursor][1]
        elif (
            cursor + 2 < len(tokens)
            and tokens[cursor][1] == "("
            and tokens[cursor + 1][0] == "string"
            and tokens[cursor + 2][1] == ")"
        ):
            if value in {"require", "import"}:
                literal = tokens[cursor + 1][1]
        elif value in {"import", "export"}:
            literal = _from_specifier(tokens, number, value, braces)
        if literal is None:
            continue
        specifier = literal[1:-1]
        # Escaped specifiers are deliberately left unresolved rather than
        # interpreting language escapes or executing arbitrary source.
        if not specifier or "\\" in specifier or len(specifier) > 500:
            continue
        if specifier.startswith("."):
            target = posixpath.normpath(
                posixpath.join(posixpath.dirname(path), specifier)
            )
            if target == ".." or target.startswith("../") or target.startswith("/"):
                continue
            reference = "javascript:" + target
        else:
            reference = "javascript-package:" + specifier
        edges.append(Edge(file_node.key, reference, "imports", line))
        if is_test(path):
            edges.append(Edge(file_node.key, reference, "tests", line))
        if len(edges) > MAX_ITEMS:
            raise ValueError("source graph exceeds its item budget")
    return Parsed("javascript-tokens", (file_node,), tuple(aliases), tuple(edges))
