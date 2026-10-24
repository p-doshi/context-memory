# Retrieval evaluation plan (written before any comparison)

Date: 2026-10-24

## Question

Does adding SQLite FTS5 keyword search to vector search improve which chunks are
returned for real questions about this corpus?

## Hypothesis

Hybrid retrieval (reciprocal rank fusion of vector and BM25 rankings) beats
vector-only retrieval on recall@5, mainly on queries that name exact identifiers
(function names, file names, error strings), and does not lose more than 0.05 MRR
on paraphrased natural-language queries.

## Systems compared

| System | Description |
|---|---|
| vector (baseline) | Qwen3-Embedding-0.6B cosine, top k |
| bm25 | FTS5 BM25 over chunk text, title and section path, top k |
| hybrid | RRF over the top 50 of each: score = sum 1 / (60 + rank) |

The RRF constant 60 is the value fixed in the original RRF paper, used here as is
and not tuned on the evaluation set:

> Gordon V. Cormack, Charles L. A. Clarke, Stefan Büttcher. 2009. Reciprocal rank
> fusion outperforms Condorcet and individual rank learning methods. In Proceedings
> of SIGIR 2009, Boston, MA, 758–759.
> https://doi.org/10.1145/1571941.1572114

Record checked 2026-10-24 against the Webis IR anthology, OpenAlex, and the
University of Waterloo Data Systems Group page.

## Evaluation set

`tests/retrieval_eval/golden.jsonl`, written before running any system. Each line
has a query, a query type (`identifier` or `paraphrase`), and the source paths that
count as relevant. Queries target documents whose answer location is unambiguous
(code files in this repo, the design documents, and named Claude sessions).
Relevance is judged at document level: a returned chunk is relevant if its
source path is in the relevant set.

## Metrics

recall@5, MRR@10, per query type. 95% confidence intervals by bootstrap over
queries (10,000 resamples, seed 20261024). Paired differences (hybrid minus vector)
use the same resamples.

## Decision rule

Make hybrid the default if the 95% CI of the paired recall@5 difference
(hybrid minus vector) excludes zero on the full set and the MRR drop on paraphrase
queries is no larger than 0.05. Otherwise keep vector as default and report.

## Limits

One small, self-authored query set (about 30 queries) on one corpus; relevance
labels are mine, not independent. This tests whether hybrid helps here, not in
general. Corpus changes between runs (the daemon keeps indexing), so each run
records the chunk count and the git commit.
