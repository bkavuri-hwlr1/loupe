# F6 offline retrieval protocol

F6 compares a product lexical search, a local semantic search and their fusion
on two pinned source trees. Its primary question is whether the fusion improves
file-level recall enough to open F4. This is a curated, difficulty-stratified
benchmark; its absolute rates do not estimate how often users ask hard questions.

## Corpus and question selection

The [manifest](../../scripts/semantic_retrieval_f6.toml) pins Loupe at
`6c7f72b4f31c024b3e618380681b472096acb144` and Django 5.2.8 at
`47fe39af56ecd0ad73b9c7562511015e96b91b80` and lists 38 and 40 behavioral
questions respectively. Each question has one expected file and a named
definition at that revision. File recall measures whether this reference
answer appears in the first K distinct files. Other plausible answer files
are ungraded. Only the question text enters retrieval.

Use F5's regular UTF-8 Python file admission policy and privacy exclusions.
Read Git objects without executing either source tree. Keep the product
line-window chunker, lexical store, BM25 weights and live identity checks for
the lexical baseline. Validate every reference symbol against the syntax tree
before drawing questions. A missing, ambiguous, private or ineligible answer
invalidates the run.

Only product lexical ranks may be seen during question assembly. Per
repository, sort IDs by the SHA-256 digest of the manifest salt, a colon and
the ID (UTF-8), then by ID. Take the first six hits and first six misses at
lexical Recall@10. Fill the remaining 18 slots in that same order, skipping
already selected IDs. An undersized stratum invalidates the draw. This keeps
at least 20 percentage points of headroom and includes known successes that
fusion might lose. Freeze the manifest, selected IDs and all knobs before the
first embedding rank. Do not redraw from a later index.

The [first lexical report](semantic-retrieval-f6-lexical-2026-11-17.json)
records ranks, anchor lines and digests for every candidate, both corpus
digests and all 60 selected IDs. The lexical arm found 18 of the 30 Loupe
reference files and 16 of the 30 Django reference files at rank 10. That
leaves 40.0% and 46.7% headroom, respectively.

## Reproduce the draw

From this checkout, with the pinned Git objects available locally:

```sh
uv run --locked python scripts/semantic_retrieval_eval.py --lexical-only \
  --queries scripts/semantic_retrieval_f6.toml \
  --repo loupe=. --repo django=/path/to/django --out /tmp/f6-lexical.json
```

The command needs no model or optional embedding dependencies. Compare the
new report against the frozen one before scoring. Record the revision
containing this protocol with the final results.

## Frozen primary embedding configuration

Use the same pinned MiniLM-L6-v2 ONNX graph, tokenizer, mean pooling and L2
normalization as F5. Run on one local CPU thread. Keep all source text on the
machine. Process files in path order, tokenize each full file once with the
pinned WordPiece tokenizer, with no padding, truncation or special tokens.
Cut token IDs into windows of 224 content tokens with a stride of 192
(32-token overlap). Emit one last shorter window only if tokens remain
uncovered. Skip empty token sequences. Keep IDs directly; decoding and then
retokenizing the windows can change them. Add the model's ordinary sentence
boundary tokens per window, then pad batches of 32 with the tokenizer pad ID.
Queries use the same tokenizer and boundary tokens without windowing.
Record actual pre-truncation lengths and counts. An actual chunk truncation
rate of 5% or more on either repository, or any truncated question, makes the
experiment inconclusive.

Rank windows by exact cosine similarity with stable ties in file/window order.
Use 80 chunk candidates and at most 20 distinct files per arm. Fuse the two
file lists with reciprocal ranks and a constant of 60, as in F5. Compare to
the original product lexical chunking even though semantic windows are shorter.

## Decision

Use all selected queries, report all three arms at Recall@5 and @10, list
file hits gained and lost by question, and weight repositories equally.
Keep [F5's gate](semantic-retrieval-f5-protocol.md#predeclared-f4-gate):
at least 30 questions per repository, a macro hybrid gain at 10 of at least
0.10, gains of at least 0.05 on both repositories, no per-repository regression
at 5, and a positive lower bound on the paired stratified 95% bootstrap
interval (10,000 samples, seed 20261030). Record CPU time, elapsed time,
vector bytes, token-window counts, truncation rates, tool versions and
verified model hashes. Recalculate independently from per-query file ranks.
A valid run that meets every threshold is a go; a valid run below threshold is
a no-go. A failed input, headroom, truncation or reproducibility check is
inconclusive. Only that completed comparison can change F4's gate.
