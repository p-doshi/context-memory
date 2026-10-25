"""Search latency through the hub (MCP over HTTP), with server-side stage times.

Usage:
    python scripts/bench_search.py --url http://127.0.0.1:8765/mcp --token-file <file> \
        --label local --out docs/benchmarks/results

Queries: the public part of tests/retrieval_eval/golden.jsonl plus a few
generic ones (30 total). Each run: 3 warm-up queries (not recorded), then
every query once per mode, in a seeded shuffled order.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

REPO = Path(__file__).resolve().parents[1]
EXTRA = ["how are stale memories retired", "which embedding model is used",
         "what does the disclosure gate withhold", "how are device tokens stored",
         "where are hooks configured"]


def pct(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


async def run(url: str, token: str, modes: list[str], seed: int) -> list[dict]:
    golden = [json.loads(l)["query"] for l in
              (REPO / "tests/retrieval_eval/golden.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    queries = (golden + EXTRA)[:30]
    rng = random.Random(seed)
    jobs = [(q, m) for q in queries for m in modes]
    rng.shuffle(jobs)
    rows = []
    async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http:
        async with streamable_http_client(url, http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as s:
                await s.initialize()
                for q in queries[:3]:  # warm-up: model kernels, caches, GPU clocks
                    await s.call_tool("memory_search", {"query": q, "k": 8, "mode": modes[0]})
                for q, m in jobs:
                    t0 = time.perf_counter()
                    r = await s.call_tool("memory_search", {"query": q, "k": 8, "mode": m, "detail": "full"})
                    wall = (time.perf_counter() - t0) * 1000
                    payload = json.loads(r.content[0].text)
                    rows.append({"mode": m, "client_ms": wall, **{f"server_{k}": v for k, v in
                                                                 payload.get("timings_ms", {}).items()}})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--token-file", required=True, type=Path)
    ap.add_argument("--label", required=True, help="e.g. local, via-server-tunnel")
    ap.add_argument("--modes", default="vector,hybrid")
    ap.add_argument("--seed", type=int, default=20261024)
    ap.add_argument("--out", type=Path, default=REPO / "docs" / "benchmarks" / "results")
    args = ap.parse_args()
    token = args.token_file.read_text().strip()
    rows = asyncio.run(run(args.url, token, args.modes.split(","), args.seed))

    summary = {}
    for m in args.modes.split(","):
        sub = [r for r in rows if r["mode"] == m]
        keys = sorted({k for r in sub for k in r if k != "mode"})
        summary[m] = {k: {"p50": round(statistics.median([r[k] for r in sub if k in r]), 1),
                          "p95": round(pct([r[k] for r in sub if k in r], 0.95), 1)} for k in keys}
        print(f"\n[{args.label}] mode={m} n={len(sub)}")
        for k, v in summary[m].items():
            print(f"  {k:24s} p50 {v['p50']:8.1f} ms   p95 {v['p95']:8.1f} ms")

    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
                            capture_output=True, text=True).stdout.strip()
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"search-{args.label}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), "git_commit": commit,
                                "label": args.label, "seed": args.seed, "summary": summary,
                                "rows": rows}, indent=2))
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
