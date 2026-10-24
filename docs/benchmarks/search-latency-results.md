# Search latency: diagnosis and fixes (2026-10-24)

Goal stated before measuring: warm hybrid search under 300 ms median end to end,
from a lab server through the SSH tunnel.

Method: `scripts/bench_search.py`, 30 distinct queries (public golden set plus five
generic ones), 3 warm-up queries, each query once per mode in a seeded shuffled
order (seed 20261024), MCP over HTTP against the running hub. Server stage times
come from `timings_ms` in each response. Corpus: about 12,000 current chunks,
1024-dimensional vectors (Qwen3-Embedding-0.6B, float32, GTX 1660 Ti).

## Results (median / 95th percentile, ms)

| Step | Path | Hybrid client | Vector client | Raw file |
|---|---|---|---|---|
| Baseline | local | 538 / 3709 | 221 / 394 | `search-local-baseline-*.json` |
| + BM25 stopwords, FTS top-N before join; LanceDB compaction | local | 235 / 308 | 211 / 319 | `search-local-optimized-*.json` |
| + exact in-memory vector search | local | **152 / 194** | 126 / 181 | `search-local-memsearch-*.json` |
| same | via tunnel (two VPN legs) | 352 / 462 | 337 / 427 | `search-via-server-tunnel-2*.json` |
| + JSON responses instead of SSE | via tunnel (two VPN legs) | **219 / 283** | 191 / 303 | `search-via-server-tunnel-json-*.json` |

Server-side stages after all changes (hybrid, local run): query embedding 70 / 89,
vector 26 / 34, BM25 18 / 46, disclosure gate 15 / 36, hydrate + audit about 2.5.

## What each fix addressed

- **BM25** (293 / 3433 ms before): common words such as "how" and "the" matched
  most chunks (7,521 vs 697 for one query without them), and every match was scored
  and joined to `chunks` before the limit. Stopwords are now dropped and the top
  matches are taken inside FTS5 first.
- **Vector search** (98 ms before): compaction (461 small fragments to 1) did not
  help; LanceDB's unindexed search took about 70 ms regardless of options, while a
  NumPy brute force over the same matrix took 3.9 ms. Search now uses an in-memory
  copy that reloads when the table version changes. A test checks ranks and
  distances match LanceDB exactly across filters.
- **Tunnel overhead**: the MCP server answered through a server-sent event stream;
  plain JSON responses removed about 130 ms per call over two VPN legs.

## Correctness bugs found on the way

- The hub's LanceDB connection never saw documents indexed after it started
  (reproduced with two connections). Connections now re-check the version every
  second.
- An abruptly ended daemon left its ssh tunnel running, blocking every later tunnel.
  The daemon now kills stale tunnel processes at start (verified by a simulated
  crash).

## What this does not establish

- The tunnel measurement crosses the VPN twice (laptop to server and back), so it
  overstates what a Claude Code session on the server sees; TCP connect time to the
  server was about 29 ms. A run from the server itself has not been done.
- One session, one corpus size. In-memory search cost grows linearly with chunks;
  above 250k rows it falls back to LanceDB, which would need an ANN index to stay fast.
- Query embedding (about 70 ms) is now the largest stage and was not optimised.
