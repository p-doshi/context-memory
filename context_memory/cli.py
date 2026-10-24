"""Command line for the Context Memory Harness. Run `cmh -h` for commands."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import time
from pathlib import Path


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _summary(results: list[dict]) -> dict:
    out: dict[str, int] = {}
    for r in results:
        out[r.get("status", "?")] = out.get(r.get("status", "?"), 0) + 1
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cmh", description="Context Memory Harness")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("ingest", help="ingest a Markdown file or directory")
    s.add_argument("path", type=Path)
    s.add_argument("--project", required=True)
    s.add_argument("--max-files", type=int, default=200)

    s = sub.add_parser("ingest-code", help="ingest a codebase (git ls-files aware)")
    s.add_argument("path", type=Path)
    s.add_argument("--project", required=True)
    s.add_argument("--max-files", type=int, default=2000)

    s = sub.add_parser("ingest-claude", help="ingest Claude Code session logs")
    s.add_argument("--config-dir", type=Path, default=Path.home() / ".claude")
    s.add_argument("--account", required=True, help="account label from sources.toml")
    s.add_argument("--include-active", action="store_true",
                   help="also index sessions written to in the last few minutes")

    s = sub.add_parser("search", help="semantic search over memory")
    s.add_argument("query")
    s.add_argument("--project")
    s.add_argument("--kind", action="append", dest="kinds")
    s.add_argument("-k", type=int, default=8)
    s.add_argument("--as", dest="recipient", default="owner",
                   help="recipient class for the Disclosure Gate (owner, cloud_model, ...)")
    s.add_argument("--json", action="store_true", help="print the full JSON response")

    s = sub.add_parser("write", help="store a manual memory")
    s.add_argument("text")
    s.add_argument("--project", required=True)
    s.add_argument("--kind", default="note")
    s.add_argument("--title")
    s.add_argument("--tag", action="append", dest="tags")

    sub.add_parser("stats", help="store statistics")
    sub.add_parser("status", help="daemon heartbeat and store statistics")
    s = sub.add_parser("audit", help="show recent audit log entries")
    s.add_argument("-n", type=int, default=20)
    sub.add_parser("serve", help="run the MCP server on stdio")
    s = sub.add_parser("daemon", help="run the always-on indexing daemon")
    s.add_argument("--once", action="store_true", help="run one pass and exit")

    s = sub.add_parser("browser", help="logged-in browser sessions for web accounts")
    s.add_argument("action", choices=["login", "probe", "sync"])
    s.add_argument("account", help="account label from sources.toml")
    s.add_argument("--url", help="page to probe (default: provider home)")
    s.add_argument("--max", type=int, default=500, help="max conversations per sync")

    sub.add_parser("accounts", help="list accounts from sources.toml (no secrets)")
    s = sub.add_parser("secret", help="manage API keys in the OS keychain")
    s.add_argument("action", choices=["set", "list", "delete"])
    s.add_argument("name", nargs="?")

    args = p.parse_args(argv)

    if args.cmd == "serve":
        from context_memory.server.mcp_server import run
        run()
        return 0
    if args.cmd == "daemon":
        from context_memory.server.daemon import main as daemon_main
        return daemon_main(once=args.once)
    if args.cmd == "secret":
        from context_memory.secrets import broker
        if args.action == "list":
            _print(broker.list_secret_names())
        elif not args.name:
            print("error: secret name required", file=sys.stderr)
            return 2
        elif args.action == "set":
            # Read from the terminal, never from argv, so the value stays out of
            # shell history and process listings.
            value = getpass.getpass(f"value for {args.name} (input hidden): ")
            if not value:
                print("error: empty value", file=sys.stderr)
                return 2
            broker.set_secret(args.name, value)
            print(f"stored {args.name} in the OS keychain")
        else:
            print("deleted" if broker.delete_secret(args.name) else "not found")
        return 0

    from context_memory.config import get_settings
    settings = get_settings()

    if args.cmd == "accounts":
        from context_memory.secrets import broker
        from context_memory.sources import load_sources
        src = load_sources(settings.resolved_sources_file)
        names = set(broker.list_secret_names())
        _print({
            "sources_file": str(settings.resolved_sources_file),
            "accounts": [{**a.__dict__, "secret_stored": a.label in names} for a in src.accounts],
        })
        return 0
    if args.cmd == "browser":
        from context_memory.browser import session
        from context_memory.sources import load_sources
        home = settings.data_dir.parent
        acct = next((a for a in load_sources(settings.resolved_sources_file).accounts
                     if a.label == args.account), None)
        if acct is None or acct.provider not in session.LOGIN_URLS:
            print(f"error: no claude/perplexity account labelled {args.account!r}", file=sys.stderr)
            return 2
        if args.action == "login":
            session.login(home, acct.label, acct.provider)
            return 0
        if args.action == "probe":
            out = session.probe(home, acct.label, args.url or session.LOGIN_URLS[acct.provider])
            print(f"wrote endpoint shapes (no values) to {out}")
            return 0
        from context_memory.browser import claude_web, perplexity_web
        from context_memory.server.app import build_service
        svc = build_service(settings)
        try:
            if acct.provider == "claude":
                res = claude_web.sync(svc, home, acct.label, max_conversations=args.max, actor="cli")
            else:
                res = perplexity_web.sync(svc, home, acct.label, max_threads=args.max, actor="cli")
            _print({"summary": _summary(res),
                    "errors": [r for r in res if r["status"] == "error"]})
        except claude_web.NotLoggedIn as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        finally:
            svc.close()
        return 0
    if args.cmd == "status":
        hb = settings.data_dir.parent / "run" / "heartbeat.json"
        if not hb.exists():
            print("no heartbeat: the daemon has not completed a pass")
            return 1
        data = json.loads(hb.read_text())
        data["seconds_since_last_pass"] = round(time.time() - data["last_pass_end"])
        _print(data)
        return 0

    from context_memory.ingestion.claude_code import ingest_config_dir
    from context_memory.ingestion.code import ingest_codebase
    from context_memory.ingestion.markdown import (
        PathNotAllowed, ingest_markdown_dir, ingest_markdown_file,
    )
    from context_memory.memory.service import IngressRejected
    from context_memory.server.app import build_service
    from context_memory.sources import load_sources

    svc = build_service(settings)
    # Paths the owner listed in sources.toml are allowed for manual runs too.
    svc.settings.ingest_roots = list(dict.fromkeys(
        settings.ingest_roots + load_sources(settings.resolved_sources_file).roots()))
    try:
        if args.cmd == "ingest":
            if args.path.is_dir():
                _print(ingest_markdown_dir(svc, args.path, args.project, args.max_files))
            else:
                _print(ingest_markdown_file(svc, args.path, args.project))
        elif args.cmd == "ingest-code":
            res = ingest_codebase(svc, args.path, args.project, args.max_files, actor="cli")
            _print({"summary": _summary(res),
                    "problems": [r for r in res if r["status"] in ("rejected", "stopped")]})
        elif args.cmd == "ingest-claude":
            idle = 0 if args.include_active else settings.session_idle_seconds
            res = ingest_config_dir(svc, args.config_dir, args.account, idle,
                                    load_sources(settings.resolved_sources_file).claude_project_map,
                                    actor="cli")
            _print({"summary": _summary(res), "sessions": res})
        elif args.cmd == "search":
            res = svc.memory_search(args.query, project=args.project, kinds=args.kinds,
                                    k=args.k, actor="cli", recipient=args.recipient)
            if args.json:
                _print(res)
            else:
                for i, r in enumerate(res["results"], 1):
                    prov = r["provenance"]
                    body = r["text"].split("\n", 1)[1].rsplit("\n", 1)[0]
                    print(f"[{i}] {r['score']:.3f}  [{r['project']}/{r['kind']}] "
                          f"{r['title'] or ''} :: {r['section_path'] or ''}")
                    print(f"    {prov['source_path']}  ({prov['verification_status']}, "
                          f"{prov['sensitivity']})")
                    print("    " + body[:300].replace("\n", "\n    "))
                    print()
                for w in res["warnings"]:
                    if w["level"] != "info" or "Disclosure" in w["message"]:
                        print(f"! {w['message']}")
        elif args.cmd == "write":
            _print(svc.memory_write(text=args.text, kind=args.kind, project=args.project,
                                    title=args.title, tags=args.tags, actor="cli"))
        elif args.cmd == "stats":
            _print({**svc.sqlite.stats(), "vectors": svc.lance.count(),
                    "embedding_model": svc.embedder.model_name,
                    "data_dir": str(svc.settings.data_dir)})
        elif args.cmd == "audit":
            _print(svc.sqlite.audit_tail(args.n))
    except (IngressRejected, PathNotAllowed, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        svc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
