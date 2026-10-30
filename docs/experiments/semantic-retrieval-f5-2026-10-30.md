# Offline semantic retrieval comparison (F5), October 30, 2026

**Decision: keep F4 gated.** Hybrid retrieval improved Recall@10 by 3.33
percentage points on each repository, below the fixed thresholds, and lost one
Loupe answer at Recall@5. This is a result for the selected model and questions;
it is not evidence that semantic retrieval is generally ineffective. Product
code, dependencies and retrieval defaults are unchanged.

The [protocol](semantic-retrieval-f5-protocol.md), sixty questions and experiment
script were committed at `3df2c0d50cee07085bd83b0abd5ccbf54647ecbc` before any
embedding ranks were computed. Independent source review checked all sixty
question-to-file mappings before inference. All sixty query records are retained
in the [unmodified result](semantic-retrieval-f5-2026-10-30.json), with
[run controls and timing](semantic-retrieval-f5-2026-10-30-run.json). A separate
read-only recalculation found no mismatch in any recorded Recall@5 or Recall@10
score. No questions, model settings or gate thresholds were changed after ranks
were seen.

## Recall

Each question has one expected answer file. Percentages therefore also give the
fraction of questions whose answer file appeared within the first five or ten
unique files. Repository means have equal weight in the macro result.

| Repository | Arm | Recall@5 | Recall@10 |
| --- | --- | ---: | ---: |
| Loupe | Lexical | 80.00% (24/30) | 90.00% (27/30) |
| Loupe | Semantic | 73.33% (22/30) | 80.00% (24/30) |
| Loupe | Hybrid | 76.67% (23/30) | 93.33% (28/30) |
| Django | Lexical | 90.00% (27/30) | 93.33% (28/30) |
| Django | Semantic | 93.33% (28/30) | 96.67% (29/30) |
| Django | Hybrid | 96.67% (29/30) | 96.67% (29/30) |
| Macro | Lexical | 85.00% | 91.67% |
| Macro | Semantic | 83.33% | 88.33% |
| Macro | Hybrid | 86.67% | 95.00% |

| Predeclared condition | Observed | Result |
| --- | --- | --- |
| At least 30 questions per repository | 30 each | Met |
| Macro Recall@10 gain at least 10 points | +3.33 points | Missed |
| Each repository Recall@10 gain at least 5 points | +3.33 points each | Missed |
| No repository Recall@5 regression | Loupe −3.33 points; Django +6.67 | Missed |
| Paired bootstrap 95% lower bound above zero | [−5.00, +11.67] points | Missed |

The descriptive paired bootstrap resamples queries within each repository,
preserves paired lexical/hybrid scores, and averages repository means equally.
It uses 10,000 samples and seed 20261030. The questions are curated, so this
interval is not a population-wide significance claim.

## Important limits

The lexical baseline was unexpectedly strong. At Recall@10 it already answered
55 of 60 questions, leaving a maximum possible macro improvement of 5/60, or
8.33 points. **Even perfect hybrid retrieval could not meet the predeclared
10-point macro threshold on this frozen question list.** Identifier-free
wording did not make these questions sufficiently difficult for lexical search.
This limits the gate's ability to distinguish a useful embedding backend. We
retain the threshold and report the ceiling rather than selecting harder
questions after seeing ranks.

The fixed sentence model also has a short input window: 1,375 of 1,433 Loupe
chunks (95.95%) and 3,259 of 3,615 Django chunks (90.15%) exceeded 256
wordpieces and were truncated. No query was truncated. The existing product
chunker creates overlapping sixty-line windows, which often exceed this model's
input limit. The experiment deliberately preserves that chunker; it does not
measure a longer-context or code-specific model.

Semantic-only search was weaker than lexical on Loupe and stronger on Django.
Hybrid recovered one additional answer at ten on each repository, while its
Loupe ranking lost an answer at five. These mixed observations and the missed
conditions do not justify opening F4 under this protocol. A different model,
chunking scheme or difficulty-calibrated corpus would require a separately
fixed experiment.

## Inputs, implementation and local cost

| Eligible pinned corpus | Files | Bytes | Lines | Chunks |
| --- | ---: | ---: | ---: | ---: |
| Loupe `69b8bcfcdf5091dc3f76674cd93318ae6fa86213`, `src/llm_cli/` | 135 | 2,762,491 | 69,298 | 1,433 |
| Django `f4c65fd6a517dd32bd2e732ae3543e550e56f206`, `django/` | 907 | 5,946,720 | 166,144 | 3,615 |

Only pinned regular UTF-8 Python blobs were read. No files were excluded by
secret screening in these prefixes. Django was obtained as a depth-one bare
public clone; no Django code or repository hooks ran. Loupe's existing product
retrieval implementation is executed by the prototype. Product lexical search
with a twenty-file limit examines up to eighty chunks before deduplication.
Semantic retrieval uses the same eighty-chunk candidate cap and twenty-file
result cap, then file-level reciprocal-rank fusion with constant sixty. A file
receives only one vote per arm.

The local model is
[`sentence-transformers/all-MiniLM-L6-v2`](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2)
at `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`, with attention-masked mean
pooling, L2 normalization and 384 dimensions. Its tokenizer and ONNX file were
verified against pinned official file identities before loading. Their actual
SHA-256 hashes, sizes, runtime versions and complete corpus manifest hashes are
in the result. There is no remote Python code or hosted embedding request.

| Local cost | Loupe | Django |
| --- | ---: | ---: |
| Lexical store build | 0.271 s | 0.662 s |
| Corpus embedding wall time | 20.067 s | 50.510 s |
| Corpus embedding CPU time | 20.032 s | 50.471 s |
| Query embedding wall time, 30 queries | 0.038 s | 0.036 s |
| All lexical queries | 1.418 s | 1.368 s |
| All exact cosine queries | 0.005 s | 0.008 s |
| Vector bytes | 2,201,088 | 5,552,640 |

Model loading took 1.650 seconds. The complete command took approximately
76.78 seconds, measured from shell-log creation to the final script write.
Embedding API cost and model API token use were zero. The run used macOS arm64,
Python 3.12.13, numpy 2.5.4, onnxruntime 1.31.1 and tokenizers 0.23.3 in a
private experiment environment. Both ONNX thread pools and numerical-library
threads were limited to one, tokenizer parallelism was disabled, sleep was
prevented, and other local benchmarks and live calls were paused. These are
single-run descriptive costs, not latency guarantees. Optional dependencies were
not added to product dependency or lock files.

Only questions, expected/ranked file paths, source anchors, hashes, scores and
aggregate measurements are retained. Source bodies and vectors are absent from
the result; no repository content went to a hosted model.

To reproduce, use the pinned revisions and model files from
[`scripts/semantic_retrieval.toml`](../../scripts/semantic_retrieval.toml),
install the recorded optional versions in a separate environment, and run:

```sh
TOKENIZERS_PARALLELISM=false OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 VECLIB_MAXIMUM_THREADS=1 \
  python scripts/semantic_retrieval_eval.py \
  --repo loupe=/path/to/loupe --repo django=/path/to/django.git \
  --model-dir /path/to/pinned-model --out /new/results.json
```

The script verifies model files and ground-truth anchors, refuses to overwrite
an existing result, and hashes the exact consumed question-file bytes. The
product search and chunker implementations must match the pinned Loupe revision.
