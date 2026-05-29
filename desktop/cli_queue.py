"""CLI for inspecting/resolving/dismissing the pending-questions queue. Plan §9 M8.

Operates directly on the queue SQLite, independent of the orchestrator, so you
can triage from a terminal:

    python cli_queue.py list
    python cli_queue.py show 3
    python cli_queue.py resolve 3 "That's a basil plant."
    python cli_queue.py dismiss 3 "no longer relevant"
    python cli_queue.py summary

Note: resolutions made here are persisted; a *running* robot picks shared
resolutions up from the DB on its next start. For live, in-conversation
learning, resolve through the orchestrator's MCP server instead.
"""

from __future__ import annotations

import argparse
import sys

from config import load_config
from pet_queue import QueueDB


def _db() -> QueueDB:
    cfg = load_config()
    return QueueDB(cfg["queue"]["db_path"], cfg["queue"]["frames_dir"])


def cmd_list(db: QueueDB, args: argparse.Namespace) -> int:
    rows = db.list_pending(args.status, args.limit)
    if not rows:
        print(f"No questions with status '{args.status}'.")
        return 0
    for r in rows:
        utter = (r["utterance"] or "(agent-initiated)")[:60]
        print(f"#{r['id']:>4}  {r['ts']}  [{r['category']}]  {utter}")
    return 0


def cmd_show(db: QueueDB, args: argparse.Namespace) -> int:
    rec = db.get_question(args.id)
    if not rec:
        print(f"No such question #{args.id}", file=sys.stderr)
        return 1
    print(f"#{rec['id']}  [{rec['category']}]  status={rec['status']}  {rec['ts']}")
    print(f"  user said : {rec.get('utterance') or '(agent-initiated)'}")
    print(f"  guess     : {rec.get('agent_guess')}")
    print(f"  why unsure: {rec.get('why_unsure')}")
    print(f"  pose      : {rec.get('pose')}")
    if rec.get("frame_abspath"):
        print(f"  frame     : {rec['frame_abspath']}")
    if rec.get("resolution"):
        print(f"  RESOLVED  : {rec['resolution']}")
    return 0


def cmd_resolve(db: QueueDB, args: argparse.Namespace) -> int:
    fact = db.resolve_question(args.id, args.text, share_with_robot=not args.no_share)
    if fact is None and not args.no_share:
        print(f"No such question #{args.id}", file=sys.stderr)
        return 1
    print(f"Resolved #{args.id}" + ("" if args.no_share else " (shared with robot on next start)"))
    return 0


def cmd_dismiss(db: QueueDB, args: argparse.Namespace) -> int:
    if not db.dismiss_question(args.id, args.reason):
        print(f"No such question #{args.id}", file=sys.stderr)
        return 1
    print(f"Dismissed #{args.id}")
    return 0


def cmd_summary(db: QueueDB, _args: argparse.Namespace) -> int:
    print(db.summarize_queue())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Robot desk pet — pending-questions queue CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="list questions")
    pl.add_argument("--status", default="pending", choices=["pending", "seen", "resolved", "dismissed"])
    pl.add_argument("--limit", type=int, default=20)
    pl.set_defaults(func=cmd_list)

    ps = sub.add_parser("show", help="show one question in full")
    ps.add_argument("id", type=int)
    ps.set_defaults(func=cmd_show)

    pr = sub.add_parser("resolve", help="resolve a question")
    pr.add_argument("id", type=int)
    pr.add_argument("text", help="the answer")
    pr.add_argument("--no-share", action="store_true", help="do not teach the robot")
    pr.set_defaults(func=cmd_resolve)

    pd = sub.add_parser("dismiss", help="dismiss a question")
    pd.add_argument("id", type=int)
    pd.add_argument("reason")
    pd.set_defaults(func=cmd_dismiss)

    psum = sub.add_parser("summary", help="one-line queue summary")
    psum.set_defaults(func=cmd_summary)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db = _db()
    try:
        return args.func(db, args)
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
