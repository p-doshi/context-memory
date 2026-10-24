# Retrieval evaluation results (2026-10-24)

Plan: [retrieval-plan.md](retrieval-plan.md), written before this run.
Raw data: [results/retrieval-20261024-201319.json](results/retrieval-20261024-201319.json)
(query ids and metrics only).

Command: `python scripts/eval_retrieval.py` with `CMH_EMBEDDING_DEVICE=cpu` (the
daemon held the GPU; same float32 weights). 30 queries: the 25 in
`tests/retrieval_eval/golden.jsonl` plus 5 kept outside the repository because they
name private documents. Bootstrap: 10,000 resamples over queries, seed 20261024.
Corpus: about 12,000 current chunks (code, docs, Claude Code / Claude web /
Perplexity sessions).

## Results (mean, 95% bootstrap CI)

| Subset | Metric | vector | bm25 | hybrid | hybrid − vector |
|---|---|---|---|---|---|
| all (30) | recall@5 | 0.750 [0.583, 0.900] | 0.833 [0.700, 0.950] | 0.900 [0.800, 0.983] | +0.150 [+0.033, +0.283] |
| all (30) | MRR@10 | 0.672 [0.517, 0.817] | 0.717 [0.578, 0.844] | 0.738 [0.614, 0.856] | +0.066 [−0.056, +0.191] |
| identifier (15) | recall@5 | 0.633 [0.400, 0.867] | 1.000 [1.000, 1.000] | 0.967 [0.900, 1.000] | +0.333 [+0.133, +0.567] |
| identifier (15) | MRR@10 | 0.589 [0.356, 0.811] | 0.856 [0.722, 0.967] | 0.780 [0.633, 0.913] | +0.191 [+0.013, +0.371] |
| paraphrase (15) | recall@5 | 0.867 [0.667, 1.000] | 0.667 [0.433, 0.867] | 0.833 [0.633, 1.000] | −0.033 [−0.100, +0.000] |
| paraphrase (15) | MRR@10 | 0.756 [0.556, 0.933] | 0.578 [0.356, 0.789] | 0.697 [0.496, 0.883] | −0.059 [−0.209, +0.072] |

## Decision (rule from the plan)

Hybrid becomes the default only if the paired recall@5 gain on all queries excludes
zero (met: +0.150, CI [+0.033, +0.283]) **and** the MRR drop on paraphrase queries is
at most 0.05 (not met: −0.059). **The default stays `vector`.**

Usage guidance derived from the subsets (not a change to the default): for queries
containing exact identifiers, hybrid finds the right document in the top 5 in 97% of
cases against 63% for vector; Claude Code is told to pass `mode="hybrid"` for such
queries (`client/claude_memory.md`).

## Limits

- 30 queries written by me, relevance judged at document level by me; small, not
  independent. Confidence intervals are wide (about ±0.15).
- BM25 was changed (stopwords, top-N before join) after the plan was written and
  before this run; the plan's systems are otherwise as specified.
- One corpus snapshot; the daemon keeps indexing, so later runs see a different corpus.
