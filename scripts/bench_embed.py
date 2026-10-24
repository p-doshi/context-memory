"""Embedding throughput/latency benchmark. Plan: docs/benchmarks/embedding-plan.md.

Usage:
    uv run --extra embed python scripts/bench_embed.py --out docs/benchmarks/results
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import platform
import random
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import torch
from sentence_transformers import SentenceTransformer

REPO = Path(__file__).resolve().parents[1]
CORPUS = REPO / "docs" / "design-v0.2.md"

QUERIES = [
    "What physics loss is currently used?",
    "Which embedding model runs locally?",
    "How are sub-agent budgets enforced?",
    "What happens when a claim fails verification?",
    "Where are secrets stored?",
    "Which datasets require owner approval for a full scan?",
    "What is the retrieval score formula?",
    "How are stale memories handled?",
    "What does the privacy ingress pipeline block?",
    "Which MCP servers are allowlisted?",
    "How many tokens should a context pack contain?",
    "What is the role of the verification sub-agents?",
    "How is prompt injection mitigated?",
    "What runs on the worker laptop?",
    "Which relations does the temporal graph support?",
    "How are document headers regenerated?",
    "What are the default network classes?",
    "How does the web console handle approvals?",
    "What is the chunk schema?",
    "Which reranker is used?",
    "What is the hard RAM limit per agent?",
    "How are injection canaries used?",
    "What is stored in the audit log?",
    "How are code claims verified?",
    "What are the week three deliverables?",
    "What does a dataset catalog entry contain?",
    "How are experiment claims checked?",
    "Which vector database is recommended?",
    "What are the capability separation rules?",
    "How is the secret broker accessed?",
    "What does the escalation response look like?",
    "What is the GPU priority order?",
]


def run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def environment(args, model_id: str, revision: str | None) -> dict:
    lock = REPO / "uv.lock"
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "command": " ".join([Path(sys.executable).name] + sys.argv),
        "git_commit": run(["git", "rev-parse", "HEAD"]) or None,
        "git_dirty": bool(run(["git", "status", "--porcelain"])),
        "uv_lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest() if lock.exists() else None,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "torch": torch.__version__,
        "cuda_runtime": torch.version.cuda,
        "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "driver": run(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"]) or None,
        "power_source": run(["powershell", "-NoProfile", "-c",
                             "(Get-CimInstance Win32_Battery).BatteryStatus"]) or None,
        "model": model_id,
        "model_revision": revision,
        "seed": args.seed,
        "reps": args.reps,
    }


def gpu_state() -> dict | None:
    out = run(["nvidia-smi", "--query-gpu=temperature.gpu,clocks.sm,power.draw",
               "--format=csv,noheader,nounits"])
    try:
        t, c, p = (float(x) for x in out.split(", "))
        return {"temp_c": int(t), "sm_mhz": int(c), "power_w": p}
    except ValueError:
        return None


def make_inputs(tokenizer, length: int, n: int, rng: random.Random) -> list[str]:
    ids = tokenizer(CORPUS.read_text(encoding="utf-8"), add_special_tokens=False)["input_ids"]
    starts = [rng.randrange(0, len(ids) - length) for _ in range(n)]
    # Decoding then re-encoding can shift counts by a token or two; acceptable here.
    return [tokenizer.decode(ids[s:s + length]) for s in starts]


def load(model_id, revision, device, dtype, cache_dir):
    # Always explicit: with no dtype, transformers loads the checkpoint's
    # bfloat16, which is slow on CPU and not native on Turing GPUs.
    kwargs = {"dtype": getattr(torch, dtype)}
    m = SentenceTransformer(model_id, device=device, revision=revision, cache_folder=cache_dir,
                            model_kwargs=kwargs, processor_kwargs={"padding_side": "left"})
    m.max_seq_length = 1024
    return m


def sync(device):
    if device == "cuda":
        torch.cuda.synchronize()


def time_docs(model, texts, batch, device, reps):
    model.encode(texts[:batch], batch_size=batch, normalize_embeddings=True)  # warm-up
    sync(device)
    if device == "cuda":
        torch.cuda.reset_peak_memory_stats()
    times = []
    for _ in range(reps):
        sync(device)
        t0 = time.perf_counter()
        model.encode(texts, batch_size=batch, normalize_embeddings=True, show_progress_bar=False)
        sync(device)
        times.append(time.perf_counter() - t0)
    peak = torch.cuda.max_memory_allocated() / 2**20 if device == "cuda" else None
    return times, peak


def time_queries(model, device):
    model.encode(QUERIES[:2], prompt_name="query")
    lat = []
    for q in QUERIES:
        sync(device)
        t0 = time.perf_counter()
        model.encode([q], prompt_name="query", normalize_embeddings=True, show_progress_bar=False)
        sync(device)
        lat.append((time.perf_counter() - t0) * 1000)
    lat.sort()
    return {"p50_ms": statistics.median(lat), "p95_ms": lat[int(0.95 * (len(lat) - 1))],
            "all_ms": lat}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="Qwen/Qwen3-Embedding-0.6B")
    ap.add_argument("--revision", default=None)
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--seed", type=int, default=20261024)
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--out", type=Path, default=REPO / "docs" / "benchmarks" / "results")
    ap.add_argument("--skip-cpu", action="store_true")
    ap.add_argument("--grid", choices=["full", "reduced"], default="full",
                    help="reduced = amendment 1 in docs/benchmarks/embedding-plan.md")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # progress visible when piped

    rng = random.Random(args.seed)
    torch.manual_seed(args.seed)

    configs = [("cuda", "float16"), ("cuda", "float32")] if torch.cuda.is_available() else []
    if not args.skip_cpu:
        configs.append(("cpu", "float32"))

    cells = []
    if args.grid == "full":
        for device, dtype in configs:
            lengths = [128, 512] if device == "cpu" else [128, 512, 1024]
            batches = [8] if device == "cpu" else [1, 4, 8, 16, 32, 64]
            cells += [(device, dtype, L, b) for L in lengths for b in batches]
    else:
        cells += [("cuda", "float16", L, b) for L in (128, 512) for b in (1, 4, 8, 16, 32)]
        cells += [("cuda", "float16", 1024, b) for b in (4, 8)]
        cells += [("cuda", "float32", 512, b) for b in (8, 16)]
        cells += [("cpu", "float32", 512, 8)]
        cells = [c for c in cells if (c[0], c[1]) in configs]
    n_gpu, n_cpu = (64, 16) if args.grid == "full" else (32, 8)
    rng.shuffle(cells)

    try:
        from huggingface_hub import HfApi
        revision = HfApi().model_info(args.model, revision=args.revision).sha
    except Exception:
        revision = args.revision

    first = load(args.model, revision, "cpu", "float32", args.cache_dir)
    tok = first.tokenizer
    inputs = {L: make_inputs(tok, L, 64, rng) for L in (128, 512, 1024)}
    del first
    gc.collect()

    env = environment(args, args.model, revision)
    print(json.dumps({k: env[k] for k in ("gpu", "torch", "cuda_runtime", "git_commit", "model_revision")}))

    # Agreement check first, one model resident at a time.
    agreement = None
    if torch.cuda.is_available():
        embs = {}
        for dtype in ("float16", "float32"):
            m = load(args.model, revision, "cuda", dtype, args.cache_dir)
            embs[dtype] = m.encode(inputs[512], batch_size=8, normalize_embeddings=True)
            del m
            gc.collect()
            torch.cuda.empty_cache()
        cos = np.sum(embs["float16"].astype(np.float64) * embs["float32"].astype(np.float64), axis=1)
        agreement = {"seq_len": 512, "n": len(cos), "min_cosine": float(cos.min()),
                     "mean_cosine": float(cos.mean())}
        print(f"fp16 vs fp32 cosine: min {cos.min():.5f} mean {cos.mean():.5f}")

    results, query = [], {}
    for device, dtype in configs:
        models_key = (device, dtype)
        model = load(args.model, revision, device, dtype, args.cache_dir)
        query[f"{device}-{dtype}"] = time_queries(model, device)
        print(f"query {device}-{dtype}: p50 {query[f'{device}-{dtype}']['p50_ms']:.1f} ms")
        for (d, dt, L, b) in [c for c in cells if c[:2] == models_key]:
            texts = inputs[L][: (n_cpu if d == "cpu" else n_gpu)]
            row = {"device": d, "dtype": dt, "seq_len": L, "batch": b, "n_inputs": len(texts),
                   "gpu_before": gpu_state()}
            try:
                times, peak = time_docs(model, texts, b, d, args.reps)
                med = statistics.median(times)
                row.update(status="ok", times_s=times, median_s=med,
                           inputs_per_s=len(texts) / med, tokens_per_s=len(texts) * L / med,
                           min_inputs_per_s=len(texts) / max(times),
                           max_inputs_per_s=len(texts) / min(times), peak_vram_mb=peak)
            except torch.OutOfMemoryError:
                row.update(status="oom")
                torch.cuda.empty_cache()
            row["gpu_after"] = gpu_state()
            results.append(row)
            g = row["gpu_after"] or {}
            print(f"{d}-{dt} L={L:4d} b={b:2d}: "
                  + (f"{row['inputs_per_s']:7.2f} inputs/s ({row['min_inputs_per_s']:.2f}-"
                     f"{row['max_inputs_per_s']:.2f})  peak {row['peak_vram_mb'] or 0:6.0f} MB"
                     if row["status"] == "ok" else "OOM")
                  + (f"  gpu {g.get('temp_c')}C {g.get('sm_mhz')}MHz" if g else ""))
        del model
        gc.collect()
        if device == "cuda":
            torch.cuda.empty_cache()

    args.out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = args.out / f"embed-{stamp}.json"
    path.write_text(json.dumps({"environment": env, "doc_throughput": results,
                                "query_latency": query, "fp16_vs_fp32": agreement}, indent=2))
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
