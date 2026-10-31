"""Operator-only administration. No research/admin tool is registered with MCP."""
import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(prog="openinvest-hosted")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8768)
    sub.add_parser("check-config")
    sub.add_parser("status")
    sub.add_parser("backup")
    restore = sub.add_parser("restore")
    restore.add_argument("owner")
    restore.add_argument("backup")
    recover = sub.add_parser("restore-control")
    recover.add_argument("backup")
    research = sub.add_parser("research")
    actions = research.add_subparsers(dest="action", required=True)
    actions.add_parser("list")
    inspect = actions.add_parser("inspect")
    inspect.add_argument("sample")
    inspect.add_argument("destination", type=Path)
    review = actions.add_parser("review")
    review.add_argument("sample")
    review.add_argument("--label", required=True, choices=["accepted", "rejected", "failure_example"])
    review.add_argument("--cleaned", type=Path)
    export = actions.add_parser("export")
    export.add_argument("destination", type=Path)
    used = actions.add_parser("mark-used")
    used.add_argument("dataset")
    args = parser.parse_args()
    from .config import REQUIRED_ENV, Settings
    if args.command == "check-config":
        missing = [name for name in REQUIRED_ENV if not os.environ.get(name, "").strip()]
        valid = False
        if not missing:
            try:
                Settings.from_env()
                valid = True
            except ValueError:
                pass
        # A configuration check must never reveal credentials or create databases.
        print(json.dumps({"valid": valid, "missing": missing,
                          "error": None if valid else "missing_fields" if missing else "invalid_values"}))
        if not valid:
            raise SystemExit(2)
        return
    settings = Settings.from_env()
    if args.command == "serve":
        import uvicorn
        from .safelog import configure
        configure()
        from .app import create_app
        uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port, access_log=False, log_config=None)
        return
    from .research import COSArchive, Research
    archive = COSArchive(settings) if settings.cos_bucket else None
    if args.command == "restore-control":
        from .backup import restore_database
        if archive is None:
            parser.error("Configure COS")
        # The control plane must be stopped and the target directory empty.
        for name in ("control", "research", "receipts"):
            restore_database(settings, archive, name, args.backup)
        print(json.dumps({"restored": True, "connections_revoked": True}))
        return
    if args.command in {"status", "backup", "restore"}:
        import httpx
        with httpx.Client(trust_env=False, timeout=1200, headers={"Authorization": "Bearer "+settings.admin_token}) as client:
            url = "http://127.0.0.1:8768/operator/"+args.command
            response = client.get(url) if args.command == "status" else client.post(url, json={"owner": args.owner, "backup": args.backup} if args.command == "restore" else {})
            if response.is_error:
                raise SystemExit(f"Operator command failed: HTTP {response.status_code}")
            print(json.dumps(response.json(), indent=2))
        return
    from .store import Store
    store = Store(settings.root, settings.encryption_key)
    sink = Research(settings, store)
    if args.action == "list":
        with sink.db() as db:
            rows = [dict(r) for r in db.execute("SELECT id,status,label,created,used FROM samples ORDER BY created DESC LIMIT 100")]
        print(json.dumps(rows, indent=2))
    elif args.action == "inspect":
        with sink.db() as db:
            row = db.execute("SELECT payload,object_key FROM samples WHERE id=? AND status IN ('pending','archived')", (args.sample,)).fetchone()
        if not row:
            raise SystemExit("Unknown available sample")
        blob = row["payload"] or (archive.get(row["object_key"]) if archive else None)
        if not blob:
            raise SystemExit("Configure COS to inspect an archived sample")
        with args.destination.open("x", encoding="utf-8", newline="\n") as output:
            args.destination.chmod(0o600)
            output.write(json.dumps(sink.unseal(blob), ensure_ascii=False, indent=2))
        print(json.dumps({"written": str(args.destination)}))
    elif args.action == "review":
        cleaned = json.loads(args.cleaned.read_text()) if args.cleaned else {}
        sink.review(args.sample, args.label, cleaned)
        print(json.dumps({"reviewed": args.sample}))
    elif args.action == "export":
        print(json.dumps(sink.export(args.destination, archive), indent=2))
    elif args.action == "mark-used":
        sink.mark_used(args.dataset)
        print(json.dumps({"marked_used": args.dataset}))


if __name__ == "__main__":
    main()
