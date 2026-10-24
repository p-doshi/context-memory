"""Retrieval evaluation. Plan: docs/benchmarks/retrieval-plan.md.

Usage:
    uv run python scripts/eval_retrieval.py --out docs/benchmarks/results
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from context_memory.config import get_settings
from context_memory.server.app import build_service

REPO = Path(__file__).resolve().parents[1]
GOLDEN = REPO / "tests" / "retrieval_eval" / "golden.jsonl"
MODES = ("vector", "bm25", "hybrid")


def is_relevant(result: dict, q: dict) -> bool:
    path = result["provenance"]["source_path"].replace("\\", "/")
    if any(path.endswith("/" + p) for p in q.get("relevant_paths", [])):
        return True
    return (result.get("title") or "") in q.get("relevant_titles", [])


def ranked_docs(svc, q: dict, mode: str, k_docs: int = 10) -> list[dict]:
    """Top k_docs distinct documents (first chunk of each) for a query."""
    res = svc.memory_search(q["query"], k=50, mode=mode, actor="eval", recipient="owner")
    seen, docs = set(), []
    for r in res["results"]:
        if r["provenance"]["doc_id"] in seen:
            continue
        seen.add(r["provenance"]["doc_id"])
        docs.append(r)
        if len(docs) == k_docs:
            break
    return docs


def metrics(docs: list[dict], q: dict) -> dict:
    rel = [is_relevant(d, q) for d in docs]
    n_rel = len(q.get("relevant_paths", [])) + len(q.get("relevant_titles", []))
    first = next((i for i, r in enumerate(rel, 1) if r), None)
    return {"recall@5": sum(rel[:5]) / n_rel, "mrr@10": 1.0 / first if first else 0.0,
            "first_relevant_rank": first}


def bootstrap(values: list[float], n: int, rng: random.Random) -> tuple[float, float, float]:
    means = sorted(statistics.fmean(rng.choices(values, k=len(values))) for _ in range(n))
    return statistics.fmean(values), means[int(0.025 * n)], means[int(0.975 * n) - 1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=20261024)
    ap.add_argument("--resamples", type=int, default=10_000)
    ap.add_argument("--out", type=Path, default=REPO / "docs" / "benchmarks" / "results")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

    queries = [json.loads(l) for l in GOLDEN.read_text(encoding="utf-8").splitlines() if l.strip()]
    svc = build_service(get_settings())
    per_query = {m: [] for m in MODES}
    for q in queries:
        for m in MODES:
            docs = ranked_docs(svc, q, m)
            per_query[m].append({"id": q["id"], "type": q["type"], **metrics(docs, q),
                                 "top3": [d["provenance"]["source_path"][-60:] for d in docs[:3]]})

    rng = random.Random(args.seed)
    summary: dict = {}
    for subset in ("all", "identifier", "paraphrase"):
        idx = [i for i, q in enumerate(queries) if subset == "all" or q["type"] == subset]
        summary[subset] = {"n": len(idx)}
        for m in MODES:
            for metric in ("recall@5", "mrr@10"):
                vals = [per_query[m][i][metric] for i in idx]
                mean, lo, hi = bootstrap(vals, args.resamples, rng)
                summary[subset][f"{m}:{metric}"] = {"mean": mean, "ci95": [lo, hi]}
        for metric in ("recall@5", "mrr@10"):
            diff = [per_query["hybrid"][i][metric] - per_query["vector"][i][metric] for i in idx]
            mean, lo, hi = bootstrap(diff, args.resamples, rng)
            summary[subset][f"hybrid-minus-vector:{metric}"] = {"mean": mean, "ci95": [lo, hi]}

    for subset, s in summary.items():
        print(f"\n[{subset}] n={s['n']}")
        for key, v in s.items():
            if key != "n":
                print(f"  {key:34s} {v['mean']:+.3f}  95% CI [{v['ci95'][0]:+.3f}, {v['ci95'][1]:+.3f}]")

    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True,
                            text=True).stdout.strip()
    out = {"timestamp": datetime.now(timezone.utc).isoformat(), "git_commit": commit,
           "git_dirty": bool(subprocess.run(["git", "status", "--porcelain"], cwd=REPO,
                                            capture_output=True, text=True).stdout.strip()),
           "command": " ".join(sys.argv), "seed": args.seed, "resamples": args.resamples,
           "corpus": {**svc.sqlite.stats(), "vectors": svc.lance.count()},
           "embedding_model": svc.embedder.model_name, "summary": summary,
           "per_query": per_query}
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"retrieval-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(out, indent=2))
    print(f"\nwrote {path}")
    svc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
