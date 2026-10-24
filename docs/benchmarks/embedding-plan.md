# Embedding benchmark plan (written before any run)

Date: 2026-10-24

## Question

Which configuration of Qwen3-Embedding-0.6B gives the highest document-embedding
throughput and lowest query latency on the development laptop (GTX 1660 Ti Max-Q,
6 GB VRAM, 16 GB RAM), while leaving VRAM for a reranker and a local LLM?

## Hypotheses

1. CUDA float16 embeds documents at least 10x faster than the CPU float32 baseline.
2. Throughput rises with batch size and plateaus once the GPU saturates; at 512 tokens
   the plateau starts at or below batch 16.
3. float16 embeddings match float32 closely enough for retrieval: per-text cosine
   similarity of at least 0.999.

## Workload

- Inputs: text from `docs/design-v0.2.md`, split into windows of exactly
  L tokens with the model tokenizer, L in {128, 512, 1024}. 64 distinct inputs per
  cell on GPU and 16 on CPU, drawn with a fixed seed.
- Queries: 32 short questions (8 to 20 tokens), embedded one at a time with the
  model's `query` prompt.

## Grid

| Factor | Levels |
|---|---|
| device / dtype | cpu-float32 (baseline), cuda-float32, cuda-float16 |
| batch size | 1, 4, 8, 16, 32, 64 (CPU: 8 only) |
| sequence length | 128, 512, 1024 (CPU: 128, 512) |

Cells run in an order shuffled with the seed, so thermal throttling over the
session does not line up with any one factor.

## Metrics

- Document throughput: inputs per second and tokens per second.
- Query latency: p50 and p95 in milliseconds over the 32 queries.
- Peak VRAM: `torch.cuda.max_memory_allocated` per cell.
- Agreement: cosine(float16, float32) per input at L=512, reported as min and mean.

## Repetition and reporting

One untimed warm-up, then 5 timed repetitions per cell. Report the median and the
min to max range across repetitions; the raw per-repetition times go into the JSON
results. Out-of-memory cells are recorded as OOM, not dropped.

## Decision rule

- Default dtype: float16 if the min cosine against float32 is at least 0.999, else float32.
- Default batch size: the smallest batch whose median throughput at L=512 is within 5%
  of the best, with peak VRAM at or below 3 GB.

## Recorded with each run

Git commit, `uv.lock` SHA-256, Python, torch, CUDA, driver, GPU name, model id and
Hugging Face revision, seed, and the exact command line.

## Amendment 1 (2026-10-24, before any result was seen)

The full grid ran for about 3 hours without finishing (output was buffered, so no
cell result was seen) and was stopped. The GPU sat at 83 to 86 °C with the SM clock
falling from 1755 to 1620 MHz, so long runs mix throttled and unthrottled cells.
Run instead with `--grid reduced`:

| device / dtype | seq len | batch |
|---|---|---|
| cuda-float16 | 128, 512 | 1, 4, 8, 16, 32 |
| cuda-float16 | 1024 | 4, 8 |
| cuda-float32 | 512 | 8, 16 |
| cpu-float32 (baseline) | 512 | 8 |

32 inputs per GPU cell, 8 on CPU, 3 timed repetitions. GPU temperature, SM clock and
power are recorded before and after every cell. Hypotheses, metrics and the decision
rule are unchanged; hypothesis 2's plateau is judged over batches 1 to 32.

## Limits

Single machine, single session. A laptop GPU's clocks depend on power source and
temperature, so absolute numbers move between sessions; relative comparisons within
one run are more trustworthy than the absolute values.
