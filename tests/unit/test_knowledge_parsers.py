"""Static parsing extracts bounded evidence without interpreting source."""

from __future__ import annotations

import pytest

from llm_cli.knowledge.parsers import MAX_ITEMS, parse


def test_python_imports_relative_modules_nested_and_repeated_definitions() -> None:
    result = parse(
        "src/pkg/work.py",
        """import os
from . import helper
from .helpers import thing
class Worker:
    async def run(self): pass
def duplicate(): pass
def duplicate(): pass
""",
    )
    assert result.error is None
    assert {node.kind for node in result.nodes} == {
        "file",
        "module",
        "class",
        "function",
    }
    assert len({node.key for node in result.nodes}) == len(result.nodes)
    assert {edge.target for edge in result.edges if edge.kind == "imports"} == {
        "python:os",
        "python:pkg",
        "python:pkg.helper",
        "python:pkg.helpers",
        "python:pkg.helpers.thing",
    }
    assert any(node.name == "Worker.run" for node in result.nodes)
    assert parse("src/pkg/work.py", "import os\n") == parse(
        "src/pkg/work.py", "import os\n"
    )


def test_test_imports_and_invalid_relative_imports() -> None:
    result = parse(
        "tests/test_work.py",
        "from pkg.work import Worker\nfrom ... import impossible\n",
    )
    assert {edge.kind for edge in result.edges} >= {"tests", "imports"}
    assert not any("impossible" in edge.target for edge in result.edges)


@pytest.mark.parametrize("suffix", ["js", "jsx", "ts", "tsx", "mjs", "cjs"])
def test_javascript_tokenizer_skips_comments_strings_and_handles_static_forms(
    suffix: str,
) -> None:
    result = parse(
        f"web/tests/view.test.{suffix}",
        """// require('commented')
/* import hidden from './hidden' */
const text = "require('not-a-call')";
const template = `import fake from 'template'`;
const expression = /require('regex-is-not-a-call')/g;
import './setup';
import type { Props } from '../types';
export { helper } from '../helper.js';
const work = require('../work');
const lazy = import('../lazy');
const dynamic = require(variable);
""",
    )
    assert result.error is None
    assert {edge.target for edge in result.edges if edge.kind == "imports"} == {
        "javascript:web/tests/setup",
        "javascript:web/types",
        "javascript:web/helper.js",
        "javascript:web/work",
        "javascript:web/lazy",
    }
    assert any(edge.kind == "tests" for edge in result.edges)


@pytest.mark.parametrize(
    "path,text",
    [
        ("bad.py", "def invalid(:\n"),
        ("bad.ts", "/* open"),
        ("bad.ts", "const text = 'open"),
    ],
)
def test_parse_failures_keep_file_and_only_record_exception_categories(
    path: str, text: str
) -> None:
    result = parse(path, text)
    assert result.error == "SyntaxError"
    assert len(result.nodes) == 1 and result.nodes[0].kind == "file"
    assert not result.edges
    assert text not in repr(result)


def test_parser_item_budget_is_nonfatal() -> None:
    result = parse("huge.py", "import os\n" * MAX_ITEMS)
    assert result.error == "ValueError"
    assert len(result.nodes) == 1


@pytest.mark.parametrize("path", ["src.py", "__init__.py", "src/__init__.py"])
def test_source_root_names_are_valid_python_files(path: str) -> None:
    assert parse(path, "def valid(): pass\n").error is None


def test_large_named_import_declarations_do_not_have_a_token_lookahead_cutoff() -> None:
    names = ", ".join(f"a{number}" for number in range(50))
    result = parse("web/view.ts", f"import {{{names}}} from './work';\n")
    assert [(edge.target, edge.line) for edge in result.edges] == [
        ("javascript:web/work", 1)
    ]


def test_semicolonless_exports_do_not_claim_the_next_imports_specifier() -> None:
    result = parse(
        "web/view.ts",
        "export const value = 1\nimport {work} from './work'\n",
    )
    assert [(edge.target, edge.line) for edge in result.edges] == [
        ("javascript:web/work", 2)
    ]


@pytest.mark.parametrize(
    "expression",
    [
        "condition || /require('fake')/",
        "condition && /require('fake')/",
        "condition ?? /require('fake')/",
        "throw /require('fake')/",
        "return /require('fake')/",
        "value += /require('fake')/",
        "if (condition) /require('fake')/.test(value)",
    ],
)
def test_regex_literals_after_operators_and_keywords_never_create_imports(
    expression: str,
) -> None:
    result = parse("web/view.js", expression + ";\n")
    assert result.error is None
    assert not result.edges


@pytest.mark.parametrize(
    "expression",
    [
        "numerator / require('real')",
        "count++ / require('real')",
        "call() / require('real')",
        "values[0] / require('real')",
        "return /require('fake')/ / require('real')",
    ],
)
def test_division_keeps_real_require_calls(expression: str) -> None:
    result = parse("web/view.js", expression + ";\n")
    assert result.error is None
    assert [edge.target for edge in result.edges] == ["javascript-package:real"]


@pytest.mark.parametrize(
    "declaration",
    [
        "import Work from './work'",
        "import Work, {helper as local} from './work'",
        "import * as Work from './work'",
        "import Work, * as Helpers from './work'",
        "import type Work from './work'",
        "import type from './work'",
        "export * from './work'",
        "export * as Work from './work'",
        "export type {Work} from './work'",
        "export type * from './work'",
    ],
)
def test_static_import_and_reexport_clause_forms(declaration: str) -> None:
    result = parse("web/view.ts", declaration + "\n")
    assert result.error is None
    assert [(edge.target, edge.line) for edge in result.edges] == [
        ("javascript:web/work", 1)
    ]


def test_token_budget_failure_remains_local_to_one_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm_cli.knowledge import parsers

    monkeypatch.setattr(parsers, "MAX_TOKENS", 8)
    result = parse("large.ts", "const first = 1; const second = 2;\n")
    assert result.error == "ValueError"
    assert [node.kind for node in result.nodes] == ["file"]
    assert not result.edges


def test_python_walk_preserves_nested_scopes_and_statement_containers() -> None:
    result = parse(
        "pkg/work.py",
        """if condition:
    import if_body
else:
    import if_else
for item in items:
    import for_body
else:
    import for_else
while condition:
    import while_body
else:
    import while_else
with manager:
    import with_body
try:
    import try_body
except Exception:
    class Handler:
        def method(self):
            import handler_method
else:
    import try_else
finally:
    import try_finally
try:
    import star_body
except* Exception:
    import star_handler
async def async_worker():
    async for item in items:
        import async_for_body
    else:
        import async_for_else
    async with manager:
        import async_with_body
def outer():
    match subject:
        case 1:
            def case_inner():
                import match_one
        case _:
            import match_other
    return sum(value * value for value in range(2_000_000))
""",
    )
    assert result.error is None
    assert {edge.target for edge in result.edges if edge.kind == "imports"} == {
        "python:" + name
        for name in (
            "if_body",
            "if_else",
            "for_body",
            "for_else",
            "while_body",
            "while_else",
            "with_body",
            "try_body",
            "handler_method",
            "try_else",
            "try_finally",
            "star_body",
            "star_handler",
            "async_for_body",
            "async_for_else",
            "async_with_body",
            "match_one",
            "match_other",
        )
    }
    assert {
        node.name for node in result.nodes if node.kind in {"function", "class"}
    } == {
        "Handler",
        "Handler.method",
        "async_worker",
        "outer",
        "outer.case_inner",
    }
    inner = next(node.key for node in result.nodes if node.name == "outer.case_inner")
    outer = next(node.key for node in result.nodes if node.name == "outer")
    assert any(edge.source == outer and edge.target == inner for edge in result.edges)
