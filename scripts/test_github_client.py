"""Standalone smoke-test for GitHubClient.

Set TEST_GITHUB_PAT in your .env (a temporary GitHub Personal Access Token)
and run::

    python scripts/test_github_client.py

This script does NOT touch the database — it only tests the HTTP client
against the live GitHub API.
"""

import asyncio
import os
import sys

# Ensure the project root is on sys.path so ``app`` is importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

from app.services.github_client import GitHubClient

load_dotenv()  # reads .env from the project root

TOKEN = os.getenv("TEST_GITHUB_PAT")
if not TOKEN:
    print("ERROR: Set TEST_GITHUB_PAT in your .env file first.")
    print("       Generate one at https://github.com/settings/tokens")
    sys.exit(1)


async def main() -> None:
    client = GitHubClient(access_token=TOKEN)

    # ── 1. Fetch repos ───────────────────────────────────────────
    print("Fetching repos...")
    repos = await client.get_repos()
    print(f"\nFound {len(repos)} repo(s):\n")
    for repo in repos:
        visibility = "private" if repo.get("private") else "public"
        print(f"  • {repo['full_name']}  ({visibility})")

    if not repos:
        print("\nNo repos found — nothing else to test.")
        return

    # ── 2. Fetch commits for the first repo ──────────────────────
    first_repo = repos[0]
    owner = first_repo["owner"]["login"]
    repo_name = first_repo["name"]

    print(f"\nFetching commits for {owner}/{repo_name}...")
    commits = await client.get_commits(owner, repo_name)
    print(f"Found {len(commits)} commit(s).")

    for commit in commits[:3]:
        sha = commit["sha"][:7]
        message = commit["commit"]["message"].splitlines()[0]  # first line only
        print(f"  {sha}  {message}")


if __name__ == "__main__":
    asyncio.run(main())
