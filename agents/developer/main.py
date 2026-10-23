"""Command-line entry point for the Miminet DeveloperAgent prototype."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from developer_agent import AgentError, DeveloperAgent


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch an Issue, prepare a Git worktree and ask Codex to fix it. No commits/push."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--issue", type=int, help="GitHub Issue number in mimi-net/miminet")
    source.add_argument("--task-file", type=Path, help="Local text/markdown task instead of a GitHub Issue")
    parser.add_argument("--repo", type=Path, default=Path.home() / "miminet", help="Existing local Miminet git clone")
    parser.add_argument("--runs-dir", type=Path, default=Path.home() / "miminet-agent-runs", help="Worktrees/reports directory outside the repo")
    parser.add_argument("--base-ref", default="main", help="Starting ref in the local git clone")
    parser.add_argument("--plan-file", type=Path, help="Optional analyst handoff plan (untrusted text)")
    parser.add_argument("--codex-bin", default="codex", help="Path to Codex CLI")
    parser.add_argument("--timeout", type=int, default=1800, help="Max Codex runtime in seconds (default: 1800)")
    parser.add_argument("--dry-run", action="store_true", help="Create worktree/prompt without invoking Codex")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.timeout < 1:
        print("ERROR: --timeout must be positive", file=sys.stderr)
        return 2
    try:
        agent = DeveloperAgent(
            repo=args.repo, runs_dir=args.runs_dir, base_ref=args.base_ref,
            codex_bin=args.codex_bin, timeout_seconds=args.timeout,
        )
        print(f"Preparing isolated Git worktree from {args.repo} ({args.base_ref})", flush=True)
        result = agent.run(
            issue_number=args.issue, task_file=args.task_file,
            plan_file=args.plan_file, dry_run=args.dry_run,
        )
    except (AgentError, OSError, UnicodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"Status: {result.status}")
    print(f"Worktree: {result.worktree}")
    print(f"Branch: {result.branch}")
    print(f"Changed: {', '.join(result.changed_files) if result.changed_files else '(none)'}")
    print(f"Checks: {result.checks}")
    print(f"Report: {result.run_dir}/report.json")
    print(f"Codex log: {result.codex_log_file}")
    if result.codex_exit_code is not None:
        print(f"Codex exit: {result.codex_exit_code}")
    if result.blocked_file_changes:
        print(f"WARNING: protected paths changed: {result.blocked_file_changes}")
    print("Review the worktree before committing, pushing, or creating a PR.")
    return 0 if result.status in ("NEEDS_REVIEW", "DRY_RUN") else 1


if __name__ == "__main__":
    raise SystemExit(main())
