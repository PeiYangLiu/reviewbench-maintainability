"""Data models used by the dataset builder."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import re
from typing import Any


SCHEMA_VERSION = "reviewbench-maintainability/v0.1"


@dataclass(frozen=True)
class PullRequestRef:
    owner: str
    repo: str
    number: int

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    @classmethod
    def parse(cls, value: str) -> "PullRequestRef":
        match = re.fullmatch(r"([^/\s]+)/([^#\s]+)#(\d+)", value.strip())
        if not match:
            raise ValueError(f"Invalid PR ref {value!r}; expected owner/repo#123")
        return cls(match.group(1), match.group(2), int(match.group(3)))

    @classmethod
    def from_html_url(cls, url: str) -> "PullRequestRef":
        match = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", url)
        if not match:
            raise ValueError(f"Could not parse PR URL: {url}")
        return cls(match.group(1), match.group(2), int(match.group(3)))


@dataclass(frozen=True)
class ReviewComment:
    id: int
    body: str
    author: str | None
    path: str | None
    line: int | None
    start_line: int | None
    original_line: int | None
    original_start_line: int | None
    diff_hunk: str | None
    original_commit_id: str | None
    commit_id: str | None
    html_url: str | None
    in_reply_to_id: int | None
    created_at: str | None
    updated_at: str | None

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "ReviewComment":
        user = payload.get("user") or {}
        return cls(
            id=int(payload["id"]),
            body=payload.get("body") or "",
            author=user.get("login"),
            path=payload.get("path"),
            line=payload.get("line"),
            start_line=payload.get("start_line"),
            original_line=payload.get("original_line"),
            original_start_line=payload.get("original_start_line"),
            diff_hunk=payload.get("diff_hunk"),
            original_commit_id=payload.get("original_commit_id"),
            commit_id=payload.get("commit_id"),
            html_url=payload.get("html_url"),
            in_reply_to_id=payload.get("in_reply_to_id"),
            created_at=payload.get("created_at"),
            updated_at=payload.get("updated_at"),
        )

    @property
    def anchor_line(self) -> int | None:
        return self.original_line or self.line

    @property
    def anchor_start_line(self) -> int | None:
        return self.original_start_line or self.start_line or self.anchor_line


@dataclass(frozen=True)
class CandidateTask:
    id: str
    repo: str
    pr_number: int
    pr_url: str
    task_instruction: str
    category: str
    gate_type: str
    confidence: float
    start_sha: str
    target_sha: str
    merge_commit_sha: str | None
    base_sha: str | None
    matched_terms: tuple[str, ...]
    contains_suggestion: bool
    locality: dict[str, Any]
    addressed_evidence: dict[str, Any]
    changed_files: list[str]
    comment: dict[str, Any]
    oracles: dict[str, Any]
    golden_patch: str | None = None
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


def candidate_id(repo_full_name: str, pr_number: int, comment_id: int, category: str) -> str:
    owner, repo = repo_full_name.split("/", 1)
    safe_owner = re.sub(r"[^A-Za-z0-9_.-]+", "_", owner)
    safe_repo = re.sub(r"[^A-Za-z0-9_.-]+", "_", repo)
    safe_category = re.sub(r"[^A-Za-z0-9_.-]+", "_", category)
    return (
        f"github__{safe_owner}__{safe_repo}"
        f"__pull_{pr_number}__comment_{comment_id}__{safe_category}"
    )
