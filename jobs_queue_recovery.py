from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path


DEFAULT_QUEUE_PATH = Path("jobs_article_queue.json")


def load_valid_queue(path=DEFAULT_QUEUE_PATH):
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("articles"), list):
        return None
    return data


def queue_is_valid(path=DEFAULT_QUEUE_PATH):
    return load_valid_queue(path) is not None


def _git_output(args, *, cwd):
    return subprocess.check_output(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        stderr=subprocess.DEVNULL,
    )


def recover_queue_from_git_history(
    path=DEFAULT_QUEUE_PATH,
    *,
    repo_root=".",
):
    """Restore the newest valid non-empty queue version available in local git history."""
    repo_root = Path(repo_root).resolve()
    path = Path(path)
    absolute_path = path if path.is_absolute() else (repo_root / path)
    try:
        relative_path = absolute_path.resolve().relative_to(repo_root).as_posix()
    except ValueError:
        return {
            "recovered": False,
            "reason": "queue_path_outside_repo",
            "commit_sha": "",
            "article_count": 0,
        }

    try:
        commits = _git_output(
            ["log", "--format=%H", "--", relative_path],
            cwd=repo_root,
        ).splitlines()
    except Exception as error:
        return {
            "recovered": False,
            "reason": f"git_log_failed:{error.__class__.__name__}",
            "commit_sha": "",
            "article_count": 0,
        }

    for commit_sha in commits:
        commit_sha = str(commit_sha or "").strip()
        if not commit_sha:
            continue
        try:
            payload = _git_output(
                ["show", f"{commit_sha}:{relative_path}"],
                cwd=repo_root,
            )
            data = json.loads(payload)
        except Exception:
            continue

        articles = data.get("articles") if isinstance(data, dict) else None
        if not isinstance(articles, list) or not articles:
            continue

        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = absolute_path.with_suffix(absolute_path.suffix + ".history-recovery.tmp")
        temp_path.write_text(payload, encoding="utf-8")
        os.replace(temp_path, absolute_path)
        return {
            "recovered": True,
            "reason": "git_history",
            "commit_sha": commit_sha,
            "article_count": len(articles),
        }

    return {
        "recovered": False,
        "reason": "no_valid_nonempty_history",
        "commit_sha": "",
        "article_count": 0,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--recover", action="store_true")
    parser.add_argument("--path", default=str(DEFAULT_QUEUE_PATH))
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args(argv)

    path = Path(args.path)
    if args.check:
        data = load_valid_queue(path)
        if data is None:
            print("Jobs queue is missing/corrupt/zero-byte; history recovery required.")
            return 42
        print(f"Jobs queue is valid before cycle: {len(data.get('articles') or [])} article(s).")
        return 0

    if args.recover:
        result = recover_queue_from_git_history(
            path,
            repo_root=args.repo_root,
        )
        if result["recovered"]:
            print(
                "Recovered Jobs queue from git history commit "
                f"{result['commit_sha']}: {result['article_count']} article(s)."
            )
        else:
            print(
                "::warning::No valid non-empty Jobs queue exists in fetched history; "
                "normal discovery-state recovery will rebuild it."
            )
        return 0

    parser.error("one of --check or --recover is required")


if __name__ == "__main__":
    raise SystemExit(main())
