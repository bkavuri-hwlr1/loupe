# Deterministic code graph

The local graph describes source structure and accepted publications. It runs
under the existing opt-in `[knowledge] index = "lexical"` setting, in
`knowledge.sqlite3`, with no provider calls or new parser dependency. It cannot
acknowledge changes, choose a write base, grant a claim or publish source.

## Build and explore

```sh
loupe config set knowledge.index lexical
loupe daemon restart
loupe repo add .
loupe index build --repo .
loupe graph neighbors workspace/batches.py --repo .
loupe graph neighbors workspace/batches.py --edge tests --depth 2
loupe --json graph neighbors llm_cli.workspace.batches --edge imports
```

`index build` and `index rebuild` construct both lexical and graph projections.
An older lexical index remains searchable after upgrading; run `index rebuild`
once to add the graph. `index drop` removes both projections for that checkout.
With indexing off, graph queries return `off` without creating an optional store.

The command follows incoming and outgoing edges. The default depth is one;
`--depth` accepts 1 through 4. `--edge` restricts every traversal step to one of:

| Type | Meaning |
| --- | --- |
| `imports` | A Python or JavaScript/TypeScript file imports a module or symbol. |
| `tests` | A file named or located like a test imports that source. |
| `defines` | A Python file defines a module, class, function or nested definition. |
| `changed-by` | The current accepted file version came from a published ledger batch. |

A node can be an exact repository-relative file path, a unique file-path suffix,
a Python dotted module or symbol name, or a `node_id` copied from JSON output.
An ambiguous suffix selects nothing. File IDs use `file:PATH`; module and symbol
IDs include the source path. Symbol IDs include their definition line so valid
repeated definitions remain distinct. Unresolved external imports remain module
references. Batch IDs use `batch:BATCH_ID`.

The graph recognizes ordinary Python package layouts and a leading `src/`.
Python imports, relative imports, classes, synchronous/asynchronous functions
and nested definitions are extracted with the standard-library `ast` parser.
Static JavaScript/TypeScript `import`, `import(...)`, `require(...)` and
re-export specifiers use a small tokenizer that skips comments, strings,
templates and regex literals in expression-start positions. Relative imports
resolve extensionless files and directory `index` files, including TypeScript
source imported with its emitted JavaScript extension. Bare JavaScript package
imports remain external references. Dynamic specifiers, template expressions,
custom Python source roots and TypeScript path aliases are not resolved. This is
static evidence; it does not claim that a conditional import executes or that an
imported function is called. Test links use filename/directory conventions.

## Evidence and freshness

Each returned edge contains its source path and line, parser name and version,
content identity, indexed source revision and generation. Python parser versions
include the interpreter minor version that defines its AST grammar; JavaScript
tokenizer versions are independent. After a grammar/parser-version upgrade,
lexical results remain available and graph discovery asks for `index rebuild`. `generation` is the
active complete projection; `indexed_generation` identifies when that edge's
rows were created. Unchanged facts can retain their original rows in a later
active generation. Accepted no-op publications update receipt evidence without
reparsing the file. A changed-by edge names the latest accepted shared batch for
the indexed file version; outside edits and Git baseline files do not invent
batch ownership.

The JSON response includes `nodes`, `edges`, `parse_failures` and `truncated`.
`direction` describes an incoming or outgoing traversal step, and `depth`
records its distance from the requested node. Results are deterministic for a
fixed indexed generation and current source policy. Queries cap output at 100
nodes and 200 edges, report at most 20 parse failures, and have a two-second
budget. Narrow the edge type or depth when results are capped.

Current exclusions and secret screening are reapplied when reading graph facts.
A newly private or ignored file is hidden, and traversal stops at that file.
Safe source edits that have not been accepted by the index mark their older
facts `stale`, matching lexical search. Edge evidence cites indexed bytes, not
unaccepted working edits. Graph facts never change coordination context.

## Failures and recovery

The tokenizer accepts at most 262,144 tokens per file; named import declarations
can span the complete bounded file instead of a fixed token lookahead. A syntax
error, recursion limit or per-file graph item limit records a sanitized
exception category for that file, retaining its file node and the lexical
projection. Its missing imports do not prevent other files from being indexed.
A parser does not execute source, import its modules or include source snippets
in diagnostics. The graph shares lexical source/file/byte/time limits.

Graph and lexical changes commit in one optional-store transaction. A publication
that removes an import removes the old edge in the next generation; unchanged
documents and their graph rows are retained. If optional storage fails, that
projection does not advance its cursor and retries without blocking publication.
`index status`, `doctor`, `db check` and `db backup` retain the existing optional
store health and recovery behavior. A verified database backup includes graph
facts and the independently checksummed knowledge schema version.
