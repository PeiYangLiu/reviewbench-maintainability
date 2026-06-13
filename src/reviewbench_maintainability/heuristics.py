"""Heuristics for identifying maintainability-focused review comments."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable


AUTO_STATIC_CATEGORIES = {"complexity", "duplication", "dead_code"}

CATEGORY_PATTERNS: dict[str, tuple[str, ...]] = {
    "complexity": (
        "too complex",
        "complexity",
        "cyclomatic",
        "simplify",
        "simpler",
        "hard to follow",
        "split this function",
        "split this method",
        "break this up",
        "extract function",
        "extract method",
        "long function",
        "large function",
        "nested",
    ),
    "duplication": (
        "duplicate",
        "duplicated",
        "duplication",
        "copy paste",
        "copy-paste",
        "repeat",
        "repeated",
        "repetitive",
        "dry",
    ),
    "dead_code": (
        "dead code",
        "unused",
        "not used",
        "unreachable",
        "remove this",
        "delete this",
    ),
    "modularity": (
        "separation of concerns",
        "separate responsibility",
        "responsibility",
        "decouple",
        "coupled",
        "helper",
        "utility",
        "reusable",
        "reuse",
        "extract",
        "move this",
    ),
    "readability": (
        "readability",
        "readable",
        "clarify",
        "clearer",
        "hard to read",
        "hard to understand",
        "confusing",
        "self-documenting",
    ),
    "naming": (
        "rename",
        "naming",
        "better name",
        "descriptive name",
        "ambiguous name",
        "misleading name",
    ),
    "testability": (
        "testable",
        "testability",
        "mock",
        "dependency injection",
        "inject",
    ),
    "api_design": (
        "api",
        "interface",
        "public contract",
        "abstraction",
        "backward compatible",
    ),
}

BOT_AUTHOR_PATTERNS = (
    "[bot]",
    "dependabot",
    "renovate",
    "pre-commit-ci",
    "github-actions",
    "copilot",
    "coderabbit",
    "reviewdog",
    "codecov",
    "sonarcloud",
)

SUGGESTION_BLOCK_RE = re.compile(
    r"```[ \t]*suggestion[^\n]*\n.*?```",
    re.IGNORECASE | re.DOTALL,
)
FENCED_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class Classification:
    category: str
    matched_terms: tuple[str, ...]
    gate_type: str
    confidence: float
    contains_suggestion: bool
    sanitized_body: str


def is_bot_author(author: str | None) -> bool:
    if not author:
        return False
    lowered = author.lower()
    return any(pattern in lowered for pattern in BOT_AUTHOR_PATTERNS)


def strip_suggestion_blocks(body: str) -> tuple[str, bool]:
    sanitized, count = SUGGESTION_BLOCK_RE.subn("", body)
    return sanitized.strip(), count > 0


def normalize_instruction(body: str) -> str:
    """Remove exact replacement suggestions and normalize whitespace."""

    without_suggestions, _ = strip_suggestion_blocks(body)
    # Preserve normal prose while dropping large code-only examples from matching.
    without_fences = FENCED_BLOCK_RE.sub(" ", without_suggestions)
    return WHITESPACE_RE.sub(" ", without_fences).strip()


def _term_matches(text: str, terms: Iterable[str]) -> tuple[str, ...]:
    lowered = text.lower()
    matches = []
    for term in terms:
        term_lower = term.lower()
        if term_lower == "dry":
            if re.search(r"\bDRY\b|\bdry\b", text):
                matches.append(term)
        elif term_lower in lowered:
            matches.append(term)
    return tuple(matches)


def classify_comment(body: str, author: str | None = None) -> list[Classification]:
    """Return maintainability classifications for a review comment.

    The classifier is intentionally conservative: bot-authored comments are
    excluded, suggestion blocks are stripped, and only explicit maintainability
    language is considered a match.
    """

    if not body or is_bot_author(author):
        return []

    stripped, contains_suggestion = strip_suggestion_blocks(body)
    sanitized = normalize_instruction(stripped)
    if len(sanitized) < 8:
        return []

    classifications: list[Classification] = []
    for category, terms in CATEGORY_PATTERNS.items():
        matches = _term_matches(sanitized, terms)
        if not matches:
            continue
        gate_type = (
            "auto_static"
            if category in AUTO_STATIC_CATEGORIES
            else "manual_or_llm_calibrated"
        )
        confidence = min(0.95, 0.48 + 0.12 * len(matches))
        if category in AUTO_STATIC_CATEGORIES:
            confidence += 0.08
        if contains_suggestion:
            confidence -= 0.06
        classifications.append(
            Classification(
                category=category,
                matched_terms=matches,
                gate_type=gate_type,
                confidence=round(max(0.0, min(confidence, 0.99)), 3),
                contains_suggestion=contains_suggestion,
                sanitized_body=sanitized,
            )
        )

    classifications.sort(key=lambda item: item.confidence, reverse=True)
    return classifications
