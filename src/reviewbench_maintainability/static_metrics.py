"""Static maintainability metrics for before/after evidence."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any


COMMENT_PREFIXES = ("#", "//", "/*", "*")


@dataclass(frozen=True)
class FileMetrics:
    path: str
    exists: bool
    loc: int = 0
    non_comment_loc: int = 0
    max_line_length: int = 0
    avg_line_length: float = 0.0
    max_indent: int = 0
    duplicate_block_ratio: float = 0.0
    python_function_count: int | None = None
    python_max_function_length: int | None = None
    python_max_cyclomatic_complexity: int | None = None
    python_parse_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "exists": self.exists,
            "loc": self.loc,
            "non_comment_loc": self.non_comment_loc,
            "max_line_length": self.max_line_length,
            "avg_line_length": self.avg_line_length,
            "max_indent": self.max_indent,
            "duplicate_block_ratio": self.duplicate_block_ratio,
            "python_function_count": self.python_function_count,
            "python_max_function_length": self.python_max_function_length,
            "python_max_cyclomatic_complexity": self.python_max_cyclomatic_complexity,
            "python_parse_error": self.python_parse_error,
        }


class _FunctionComplexityVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.functions: list[tuple[int, int]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._record_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._record_function(node)

    def _record_function(self, node: ast.AST) -> None:
        counter = _ComplexityCounter()
        counter.visit(node)
        start = getattr(node, "lineno", 0)
        end = getattr(node, "end_lineno", start)
        self.functions.append((max(0, end - start + 1), counter.complexity))
        self.generic_visit(node)


class _ComplexityCounter(ast.NodeVisitor):
    def __init__(self) -> None:
        self.complexity = 1

    def visit_If(self, node: ast.If) -> None:
        self.complexity += 1
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        self.complexity += 1
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.complexity += 1
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        self.complexity += 1
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self.complexity += 1
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        self.complexity += max(0, len(node.values) - 1)
        self.generic_visit(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self.complexity += 1
        self.generic_visit(node)

    def visit_comprehension(self, node: ast.comprehension) -> None:
        self.complexity += 1 + len(node.ifs)
        self.generic_visit(node)


def _normalized_code_lines(lines: list[str]) -> list[str]:
    normalized = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith(COMMENT_PREFIXES):
            continue
        stripped = re.sub(r"\s+", " ", stripped)
        normalized.append(stripped)
    return normalized


def duplicate_block_ratio(lines: list[str], window: int = 6) -> float:
    normalized = _normalized_code_lines(lines)
    if len(normalized) < window:
        return 0.0
    windows = [tuple(normalized[index : index + window]) for index in range(len(normalized) - window + 1)]
    counts: dict[tuple[str, ...], int] = {}
    for block in windows:
        counts[block] = counts.get(block, 0) + 1
    duplicate_windows = sum(count for count in counts.values() if count > 1)
    return round(duplicate_windows / len(windows), 4)


def compute_file_metrics(path: Path, display_path: str | None = None) -> FileMetrics:
    display = display_path or str(path)
    if not path.exists() or not path.is_file():
        return FileMetrics(path=display, exists=False)

    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    non_comment = _normalized_code_lines(lines)
    lengths = [len(line) for line in lines]
    indents = [len(line) - len(line.lstrip(" ")) for line in lines if line.strip()]

    base = {
        "path": display,
        "exists": True,
        "loc": len(lines),
        "non_comment_loc": len(non_comment),
        "max_line_length": max(lengths, default=0),
        "avg_line_length": round(sum(lengths) / len(lengths), 2) if lengths else 0.0,
        "max_indent": max(indents, default=0),
        "duplicate_block_ratio": duplicate_block_ratio(lines),
    }

    if path.suffix != ".py":
        return FileMetrics(**base)

    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return FileMetrics(**base, python_parse_error=str(exc))

    visitor = _FunctionComplexityVisitor()
    visitor.visit(tree)
    function_lengths = [length for length, _ in visitor.functions]
    complexities = [complexity for _, complexity in visitor.functions]
    return FileMetrics(
        **base,
        python_function_count=len(visitor.functions),
        python_max_function_length=max(function_lengths, default=0),
        python_max_cyclomatic_complexity=max(complexities, default=0),
    )


def compare_paths(before_dir: Path, after_dir: Path, paths: list[str], category: str | None = None) -> dict[str, Any]:
    files = []
    for rel_path in paths:
        before = compute_file_metrics(before_dir / rel_path, rel_path)
        after = compute_file_metrics(after_dir / rel_path, rel_path)
        files.append(
            {
                "path": rel_path,
                "before": before.to_dict(),
                "after": after.to_dict(),
                "delta": metric_delta(before, after),
                "directional_evidence": directional_evidence(before, after, category),
            }
        )
    return {
        "category": category,
        "files": files,
        "summary": summarize_file_evidence(files),
    }


def metric_delta(before: FileMetrics, after: FileMetrics) -> dict[str, Any]:
    fields = (
        "loc",
        "non_comment_loc",
        "max_line_length",
        "avg_line_length",
        "max_indent",
        "duplicate_block_ratio",
        "python_function_count",
        "python_max_function_length",
        "python_max_cyclomatic_complexity",
    )
    result: dict[str, Any] = {}
    before_dict = before.to_dict()
    after_dict = after.to_dict()
    for field in fields:
        before_value = before_dict[field]
        after_value = after_dict[field]
        if before_value is None or after_value is None:
            result[field] = None
        else:
            result[field] = round(after_value - before_value, 4)
    return result


def directional_evidence(before: FileMetrics, after: FileMetrics, category: str | None) -> dict[str, Any]:
    delta = metric_delta(before, after)
    if category == "complexity":
        signals = {
            "max_function_length_decreased": _is_negative(delta["python_max_function_length"]),
            "cyclomatic_complexity_decreased": _is_negative(delta["python_max_cyclomatic_complexity"]),
            "max_indent_decreased": _is_negative(delta["max_indent"]),
        }
    elif category == "duplication":
        signals = {
            "duplicate_block_ratio_decreased": _is_negative(delta["duplicate_block_ratio"]),
            "non_comment_loc_decreased": _is_negative(delta["non_comment_loc"]),
        }
    elif category == "dead_code":
        signals = {
            "non_comment_loc_decreased": _is_negative(delta["non_comment_loc"]),
            "loc_decreased": _is_negative(delta["loc"]),
        }
    else:
        signals = {"automatic_gate_available": False}
    return {
        "signals": signals,
        "has_positive_static_evidence": any(value is True for value in signals.values()),
    }


def summarize_file_evidence(files: list[dict[str, Any]]) -> dict[str, Any]:
    positive = sum(1 for item in files if item["directional_evidence"]["has_positive_static_evidence"])
    return {
        "file_count": len(files),
        "files_with_positive_static_evidence": positive,
        "has_any_positive_static_evidence": positive > 0,
    }


def _is_negative(value: Any) -> bool | None:
    if value is None:
        return None
    return value < 0
