# Offline semantic retrieval comparison (F5)

This protocol and the questions are fixed before the first embedding ranks are
computed. The experiment does not change product configuration or code under
`src/`. F4 remains gated until the recorded decision below is available.

## Fixed inputs

- Loupe: `69b8bcfcdf5091dc3f76674cd93318ae6fa86213`, regular UTF-8 Python
  files under `src/llm_cli/`.
- Django: `f4c65fd6a517dd32bd2e732ae3543e550e56f206`, regular UTF-8 Python
  files under `django/`. It must be larger by eligible files, bytes and lines.
- Thirty natural-language questions per repository, sixty total, in
  [`scripts/semantic_retrieval.toml`](../../scripts/semantic_retrieval.toml).
  Each asks where behavior happens without using a source identifier, and names
  the expected answer file and a source symbol as ground truth. Ground truth is
  inspected against the pinned code before rankings are computed. Questions are
  not rewritten or dropped after results are seen.
- Local model: [`sentence-transformers/all-MiniLM-L6-v2`](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2),
  revision `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`. Its public ONNX graph
  and tokenizer run on the CPU without remote Python code. Attention-masked mean
  pooling and L2 normalization produce 384-dimensional vectors. Inputs are
  truncated at 256 wordpieces, matching the model card; the report records how
  many chunks were truncated. This short-input sentence model may be a poor fit
  for code windows, which is a limitation of this fixed model, not a reason to
  change inputs after measuring.

Pinned corpus blobs are read as data; no Django code or repository hooks run.
The prototype executes Loupe's retrieval implementation. Source admission
keeps the existing 20,000-file, 1 MiB-per-file and 64 MiB-total bounds and excludes
recognized secret material. No source is sent to a hosted embedding provider.
Only public model weights and the public repository are downloaded. Optional
experiment dependencies live in a private environment and are not added to the
product dependency or lock files.

## Retrieval and measures

The script uses the product's existing line-window chunker, real knowledge store
and real lexical search with a limit of twenty files. Product lexical search
examines up to eighty matching chunks before deduplication. Semantic search
scores the same eligible chunks by exact cosine similarity, takes its first
eighty chunk candidates, then deduplicates to at most twenty files. Reciprocal
rank fusion combines those two file rankings with constant sixty. Duplicate
chunks from one file do not create extra votes. Ties are deterministic.

Record per-query and repository-mean file Recall@5 and Recall@10 for lexical,
semantic and hybrid retrieval. Also record eligible corpus sizes, lexical build
time, embedding wall and CPU time, vector bytes, truncation counts, model file
hashes, package versions and the exact query-file hash. There is no model API
billing or token consumption. Results contain questions, expected/ranked file
paths and scores, never source bodies or vectors. CI exercises only fixed-input
scoring and fusion; it downloads no model and runs no embeddings.

## Predeclared F4 gate

All of the following are required:

1. At least thirty questions on each of the two repositories.
2. Repository-macro hybrid Recall@10 improvement over lexical of at least 0.10
   absolute.
3. Hybrid Recall@10 improvement of at least 0.05 on each repository.
4. No hybrid Recall@5 regression on either repository.
5. The descriptive paired-query bootstrap 95% interval for the repository-macro
   Recall@10 improvement has a lower bound greater than zero. Resample queries
   within each repository, preserving paired arm scores, for 10,000 samples with
   random seed 20261030; average the repository means equally.

A failed condition keeps F4 closed. These are decisions about this selected
corpus, question set, model and resource bounds. The bootstrap is descriptive;
these questions are curated rather than a random sample of all developer work,
so it does not establish a universal significance or agent-quality claim. A
positive result would justify a product implementation experiment, not a change
to the default retrieval setting.
