"""Small GitHub CLI-backed API client."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
from typing import Any

from .models import PullRequestRef


class GitHubClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class PullRequestMaterials:
    ref: PullRequestRef
    pull: dict[str, Any]
    review_comments: list[dict[str, Any]]
    issue_comments: list[dict[str, Any]]
    files: list[dict[str, Any]]
    commits: list[dict[str, Any]]
    review_threads: list[dict[str, Any]]

    def write_snapshot(self, root: Path) -> None:
        directory = snapshot_pr_dir(root, self.ref)
        directory.mkdir(parents=True, exist_ok=True)
        for name, payload in (
            ("pull.json", self.pull),
            ("review_comments.json", self.review_comments),
            ("issue_comments.json", self.issue_comments),
            ("files.json", self.files),
            ("commits.json", self.commits),
            ("review_threads.json", self.review_threads),
        ):
            (directory / name).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )

    @classmethod
    def read_snapshot(cls, directory: Path) -> "PullRequestMaterials":
        meta = directory.name.split("__")
        if len(meta) < 3:
            raise ValueError(f"Snapshot directory name must be owner__repo__number: {directory}")
        ref = PullRequestRef(meta[0], "__".join(meta[1:-1]), int(meta[-1]))

        def read_json(name: str, default: Any) -> Any:
            path = directory / name
            if not path.exists():
                return default
            return json.loads(path.read_text(encoding="utf-8"))

        return cls(
            ref=ref,
            pull=read_json("pull.json", {}),
            review_comments=read_json("review_comments.json", []),
            issue_comments=read_json("issue_comments.json", []),
            files=read_json("files.json", []),
            commits=read_json("commits.json", []),
            review_threads=read_json("review_threads.json", []),
        )


def snapshot_pr_dir(root: Path, ref: PullRequestRef) -> Path:
    return root / f"{ref.owner}__{ref.repo}__{ref.number}"


class GitHubClient:
    def __init__(self, gh_binary: str = "gh") -> None:
        self.gh_binary = gh_binary

    def _run(self, args: list[str]) -> Any:
        completed = subprocess.run(
            [self.gh_binary, *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if completed.returncode != 0:
            raise GitHubClientError(completed.stderr.strip() or completed.stdout.strip())
        if not completed.stdout.strip():
            return None
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise GitHubClientError(f"gh returned non-JSON output: {completed.stdout[:200]}") from exc

    def api_get(self, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        args = ["api", "-X", "GET", endpoint]
        for key, value in (params or {}).items():
            args.extend(["-f", f"{key}={value}"])
        return self._run(args)

    def graphql(self, query: str, variables: dict[str, Any]) -> Any:
        args = ["api", "graphql", "-f", f"query={query}"]
        for key, value in variables.items():
            if value is None:
                continue
            args.extend(["-F", f"{key}={value}"])
        return self._run(args)

    def list_paginated(self, endpoint: str, params: dict[str, Any] | None = None) -> list[Any]:
        all_items: list[Any] = []
        page = 1
        while True:
            page_params = {"per_page": 100, "page": page}
            page_params.update(params or {})
            payload = self.api_get(endpoint, page_params)
            if not payload:
                break
            if not isinstance(payload, list):
                raise GitHubClientError(f"Expected list from {endpoint}, got {type(payload).__name__}")
            all_items.extend(payload)
            if len(payload) < 100:
                break
            page += 1
        return all_items

    def search_merged_prs(self, query: str, limit: int) -> list[PullRequestRef]:
        refs: list[PullRequestRef] = []
        seen: set[tuple[str, str, int]] = set()
        page = 1
        while len(refs) < limit:
            payload = self.api_get(
                "/search/issues",
                {"q": query, "per_page": min(100, limit - len(refs)), "page": page},
            )
            items = payload.get("items", []) if isinstance(payload, dict) else []
            if not items:
                break
            for item in items:
                html_url = item.get("html_url", "")
                if "/pull/" not in html_url:
                    continue
                ref = PullRequestRef.from_html_url(html_url)
                key = (ref.owner, ref.repo, ref.number)
                if key not in seen:
                    refs.append(ref)
                    seen.add(key)
                    if len(refs) >= limit:
                        break
            if len(items) < 100:
                break
            page += 1
        return refs

    def fetch_pull_request(
        self,
        ref: PullRequestRef,
        include_threads: bool = True,
        include_issue_comments: bool = True,
        include_commits: bool = True,
    ) -> PullRequestMaterials:
        owner_repo = f"{ref.owner}/{ref.repo}"
        pull = self.api_get(f"/repos/{owner_repo}/pulls/{ref.number}")
        review_comments = self.list_paginated(f"/repos/{owner_repo}/pulls/{ref.number}/comments")
        issue_comments = (
            self.list_paginated(f"/repos/{owner_repo}/issues/{ref.number}/comments")
            if include_issue_comments
            else []
        )
        files = self.list_paginated(f"/repos/{owner_repo}/pulls/{ref.number}/files")
        commits = (
            self.list_paginated(f"/repos/{owner_repo}/pulls/{ref.number}/commits")
            if include_commits
            else []
        )
        review_threads = []
        if include_threads:
            try:
                review_threads = self.fetch_review_threads(ref)
            except GitHubClientError:
                review_threads = []
        return PullRequestMaterials(
            ref=ref,
            pull=pull,
            review_comments=review_comments,
            issue_comments=issue_comments,
            files=files,
            commits=commits,
            review_threads=review_threads,
        )

    def compare(self, ref: PullRequestRef, base: str, head: str) -> dict[str, Any]:
        return self.api_get(f"/repos/{ref.full_name}/compare/{base}...{head}")

    def fetch_review_threads(self, ref: PullRequestRef) -> list[dict[str, Any]]:
        query = """
        query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
          repository(owner: $owner, name: $repo) {
            pullRequest(number: $number) {
              reviewThreads(first: 100, after: $cursor) {
                nodes {
                  id
                  isResolved
                  isOutdated
                  comments(first: 100) {
                    nodes {
                      databaseId
                      url
                      body
                      path
                      line
                      originalLine
                      originalCommit {
                        oid
                      }
                    }
                  }
                }
                pageInfo {
                  hasNextPage
                  endCursor
                }
              }
            }
          }
        }
        """
        threads: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            payload = self.graphql(
                query,
                {
                    "owner": ref.owner,
                    "repo": ref.repo,
                    "number": ref.number,
                    "cursor": cursor,
                },
            )
            thread_conn = (
                payload.get("data", {})
                .get("repository", {})
                .get("pullRequest", {})
                .get("reviewThreads", {})
            )
            threads.extend(thread_conn.get("nodes", []))
            page_info = thread_conn.get("pageInfo", {})
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
        return threads


def resolution_by_comment_id(review_threads: list[dict[str, Any]]) -> dict[int, bool]:
    mapping: dict[int, bool] = {}
    for thread in review_threads:
        is_resolved = bool(thread.get("isResolved"))
        comments = (thread.get("comments") or {}).get("nodes") or []
        for comment in comments:
            database_id = comment.get("databaseId")
            if database_id is not None:
                mapping[int(database_id)] = is_resolved
    return mapping
