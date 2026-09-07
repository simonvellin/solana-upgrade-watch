"""CLI: one-shot report or continuous watch with webhook alerts."""
import argparse
import json
import sys
import time
import urllib.request

from upgrade_watch.core import DEFAULT_RPC, resolve

# Fields whose change between polls is worth alerting on.
_WATCHED = ("upgrade_authority", "last_deploy_slot", "upgradable")


def _fmt(report: dict) -> str:
    lines = [f"program           {report['program']}"]
    for k in ("loader", "upgradable", "upgrade_authority", "authority_kind",
              "last_deploy_slot", "last_deploy_time"):
        if report.get(k) is not None:
            lines.append(f"{k:<18}{report[k]}")
    ms = report.get("multisig")
    if ms:
        lines.append(f"{'multisig':<18}{ms.get('settings')}")
        lines.append(f"{'threshold':<18}{ms.get('threshold_str')} "
                     f"({ms.get('total_members')} members total)")
        tl = ms.get("timelock_s") or 0
        lines.append(f"{'timelock':<18}{str(tl // 3600) + 'h' if tl else 'none'}")
    for q in report.get("queued_proposals") or []:
        lines.append(f"{'queued':<18}#{q['index']} {q['status']} "
                     f"{q['approvals']}/{q.get('approvals_required')} approvals"
                     + (" [UPGRADE]" if q.get("is_upgrade") else ""))
    if report.get("error"):
        lines.append(f"{'error':<18}{report['error']}")
    if report.get("verdict"):
        lines.append("")
        lines.append(report["verdict"])
    return "\n".join(lines)


def _diff(old: dict, new: dict) -> list:
    changes = []
    for k in _WATCHED:
        if old.get(k) != new.get(k):
            changes.append(f"{k}: {old.get(k)} -> {new.get(k)}")
    oq = {q["index"]: q["status"] for q in old.get("queued_proposals") or []}
    for q in new.get("queued_proposals") or []:
        if q["index"] not in oq:
            changes.append(f"new proposal #{q['index']} ({q['status']})"
                           + (" [UPGRADE]" if q.get("is_upgrade") else ""))
        elif oq[q["index"]] != q["status"]:
            changes.append(f"proposal #{q['index']}: {oq[q['index']]} -> {q['status']}")
    return changes


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="solana-upgrade-watch",
        description="Who can upgrade your money? Resolve a Solana program's "
                    "upgrade authority, multisig, timelock and queued upgrades.")
    ap.add_argument("program", help="program id (base58)")
    ap.add_argument("--rpc", default=DEFAULT_RPC, help="JSON-RPC endpoint")
    ap.add_argument("--json", action="store_true", help="raw JSON output")
    ap.add_argument("--shallow", action="store_true",
                    help="skip the Squads reverse-lookup and queue scan")
    ap.add_argument("--watch", type=int, metavar="SECONDS",
                    help="poll continuously and report changes")
    ap.add_argument("--webhook", metavar="URL",
                    help="POST changes as JSON to this URL (watch mode)")
    args = ap.parse_args()

    report = resolve(args.program, args.rpc, deep=not args.shallow)
    print(json.dumps(report, indent=2) if args.json else _fmt(report))

    if not args.watch:
        return 1 if report.get("error") else 0

    prev = report
    while True:
        time.sleep(args.watch)
        try:
            cur = resolve(args.program, args.rpc, deep=not args.shallow)
        except Exception as e:
            print(f"[watch] poll failed: {e}", file=sys.stderr)
            continue
        changes = _diff(prev, cur)
        if changes:
            stamp = cur.get("checked_at")
            for c in changes:
                print(f"[{stamp}] {c}")
            if args.webhook:
                body = json.dumps({"program": args.program,
                                   "changes": changes, "report": cur}).encode()
                try:
                    urllib.request.urlopen(urllib.request.Request(
                        args.webhook, data=body,
                        headers={"Content-Type": "application/json"}), timeout=15)
                except Exception as e:
                    print(f"[watch] webhook failed: {e}", file=sys.stderr)
        prev = cur


if __name__ == "__main__":
    raise SystemExit(main())
