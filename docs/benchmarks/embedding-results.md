# Embedding benchmark results

Plan: [embedding-plan.md](embedding-plan.md) (amendment 1, reduced grid).
Raw data: [results/embed-20261024-164440.json](results/embed-20261024-164440.json).

| | |
|---|---|
| Command | `python -u scripts/bench_embed.py --grid reduced --reps 3 --cache-dir C:/JARVIS/models` |
| Code | git `37304f5` (clean tree). The raw file records `b991263`, the same commit's ID before the history rewrite of 2026-10-24 that removed private data; `scripts/bench_embed.py`, `embedder.py` and `config.py` are byte-identical between the two. |
| Model | Qwen/Qwen3-Embedding-0.6B @ `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3` |
| Stack | torch 2.14.1+cu126, CUDA runtime 12.6, driver 580.88, Python 3.12.11 |
| Hardware | Dell G3 3590, GTX 1660 Ti Max-Q 6 GB, on AC power |
| Seed | 20261024; 32 inputs per GPU cell, 8 on CPU; 3 timed repetitions after 1 warm-up |

## Document throughput

Median of 3 repetitions; the range across repetitions was within 1% for every cell
except where noted, so it is omitted.

| Device / dtype | Seq len | Batch | Inputs/s | Tokens/s | Peak VRAM (MB) | SM clock after (MHz) |
|---|---|---|---|---|---|---|
| cuda float16 | 128 | 1 | 3.14 | 402 | 1150 | 1770 |
| cuda float16 | 128 | 4 | 4.11 | 526 | 1162 | 1770 |
| cuda float16 | 128 | 8 | 4.34 | 556 | 1173 | 1770 |
| cuda float16 | 128 | 16 | 4.48 | 573 | 1201 | 1770 |
| cuda float16 | 128 | 32 | 4.75 | 608 | 1250 | 1770 |
| cuda float16 | 512 | 1 | 1.01 | 517 | 1162 | 1770 |
| cuda float16 | 512 | 4 | 1.09 | 558 | 1201 | 1770 |
| cuda float16 | 512 | 8 | 1.16 | 594 | 1258 | 1770 |
| cuda float16 | 512 | 16 | 1.17 | 599 | 1370 | 1770 |
| cuda float16 | 512 | 32 | 1.19 | 609 | 1565 | 1770 |
| cuda float16 | 1024 | 4 | 0.57 | 584 | 1258 | 1770 |
| cuda float16 | 1024 | 8 | 0.57 | 584 | 1370 | 1770 |
| **cuda float32** | **512** | **8** | **4.98** | **2550** | **2506** | 1770 |
| cuda float32 | 512 | 16 | 4.27 | 2186 | 2731 | 1620 (throttled) |
| cpu float32 (baseline) | 512 | 8 | 0.23 (0.22–0.24) | 118 | n/a | n/a |

Query latency (single query, p50 over 32): cuda float16 120.6 ms, cuda float32
146.2 ms, cpu float32 551.8 ms.

GPU temperature was 82–83 °C throughout the GPU cells.

## Hypotheses

1. **CUDA float16 is at least 10x faster than CPU float32: refuted.** float16 is
   5.0x faster (1.16 vs 0.23 inputs/s at L=512). float32 on CUDA is 21.7x faster.
2. **Throughput plateaus at or below batch 16 (L=512): supported.** float16 rises
   from 1.01 (batch 1) to 1.17 (batch 16) and 1.19 (batch 32).
3. **float16 matches float32 (min cosine ≥ 0.999): supported.** Over 64 inputs at
   L=512, min cosine 0.99977, mean 1.00005. (The mean is above 1 because
   float16-normalised vectors are not exactly unit length.)

## Why float16 is slow here

A raw 4096×4096 matrix multiply on the same GPU and stack gives a median of
3.83 TFLOPS in float32 and 0.59 TFLOPS in float16 (5 repetitions each, spread
under 1%). The slowdown is in the half-precision GEMM path of this
GPU/driver/cuBLAS combination, below the model code. I have not established why
cuBLAS takes that path on this GPU.

## Decision, and a deviation from the plan

The plan's rule picks float16 whenever its agreement with float32 is at least
0.999. Applied literally, it selects float16, which is 4.3x slower than float32
here. The rule silently assumed float16 is at least as fast; the data show
otherwise. **Post-hoc deviation:** the default is float32, chosen after seeing
the throughput results. Within float32, the plan's batch rule (smallest batch
within 5% of the best at L=512, VRAM ≤ 3 GB) selects batch 8 (4.98 inputs/s,
2.5 GB). Only batches 8 and 16 were measured in float32, and the batch-16 cell
ran throttled, so the batch choice is weakly supported.

Defaults set: `CMH_EMBEDDING_DTYPE=float32`, `CMH_EMBEDDING_BATCH_SIZE=8`.

## What this does not establish

- One session on one laptop. Throughput depends on temperature and power; absolute
  values will move between sessions.
- 3 repetitions per cell on a fixed set of 32 inputs; the narrow ranges reflect
  run-to-run timing noise, not variation over different inputs.
- Chunks indexed before this change were embedded in float16. Given hypothesis 3,
  mixing them with float32 vectors should not affect retrieval, but that was not
  tested on retrieval quality directly.
