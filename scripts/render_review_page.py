"""Render a human-friendly HTML review page for verified benchmark cases.

The input is the rendered JSON produced for strict-verified sampling, where each
item carries ``original_pr``, ``original_review_comment``, ``task`` (including
``human_annotation``), and ``why_kept``. The output is a single self-contained
HTML page designed for fast visual inspection:

- GitHub-style colored diff with an old/new line-number gutter and the commented
  line highlighted;
- category / gate / language / quality badges;
- human-label verification badges (keep, maintainability, clear, localized,
  no-leak) colored by value;
- the sanitized task instruction shown prominently;
- collapsible original PR context and "why accepted" rationale;
- a per-case "checked" toggle (persisted in localStorage) with a progress meter
  and keyboard navigation (j/k).
"""

from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path
from typing import Any


CATEGORY_COLORS = {
    "complexity": "#8250df",
    "duplication": "#bc4c00",
    "dead_code": "#cf222e",
    "modularity": "#1b7c83",
    "readability": "#0969da",
    "naming": "#bf3989",
    "testability": "#1a7f37",
    "api_design": "#4338ca",
}

LABEL_COLORS = {
    "yes": ("#1a7f37", "#dafbe1", "#aceebb"),
    "partial": ("#9a6700", "#fff8c5", "#f0d98c"),
    "no": ("#cf222e", "#ffebe9", "#ffcecb"),
    "unclear": ("#59636e", "#f3f4f6", "#d0d7de"),
}

HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
INLINE_CODE_RE = re.compile(r"`([^`]+)`")
STRIKETHROUGH_RE = re.compile(r"~~?([^~]+)~~?")
SUGGESTION_RE = re.compile(r"```[ \t]*suggestion[^\n]*\n(.*?)```", re.IGNORECASE | re.DOTALL)


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def render_inline(text: str | None) -> str:
    """Escape text and apply a light subset of inline Markdown."""

    if not text:
        return ""
    out = esc(text)
    out = INLINE_CODE_RE.sub(lambda m: f"<code>{m.group(1)}</code>", out)
    out = STRIKETHROUGH_RE.sub(lambda m: f"<s>{m.group(1)}</s>", out)
    return out.replace("\n", "<br>")


def render_comment_body(body: str | None) -> str:
    """Render a review comment, visually de-emphasizing suggestion blocks."""

    if not body:
        return '<span class="muted">(empty comment)</span>'

    parts: list[str] = []
    last = 0
    for match in SUGGESTION_RE.finditer(body):
        parts.append(render_inline(body[last:match.start()]))
        inner = match.group(1).strip("\n")
        label = "建议块(空→删除)" if not inner.strip() else "建议块(已在任务中剥离)"
        block = esc(inner) if inner.strip() else "&nbsp;"
        parts.append(
            f'<div class="suggestion"><span class="suggestion-tag">{label}</span>'
            f'<pre>{block}</pre></div>'
        )
        last = match.end()
    parts.append(render_inline(body[last:]))
    return "".join(p for p in parts if p)


def render_diff(hunk: str | None, anchor_line: int | None) -> str:
    """Render a unified diff hunk with a line-number gutter and coloring."""

    if not hunk:
        return '<div class="muted">(no diff hunk)</div>'

    rows: list[str] = []
    old_no = new_no = 0
    for raw in hunk.splitlines():
        cls = "ctx"
        old_cell = new_cell = ""
        header = HUNK_HEADER_RE.match(raw)
        if header:
            old_no = int(header.group(1))
            new_no = int(header.group(2))
            rows.append(
                f'<tr class="hunk"><td class="ln"></td><td class="ln"></td>'
                f'<td class="code">{esc(raw)}</td></tr>'
            )
            continue
        if raw.startswith("…"):
            rows.append(
                f'<tr class="skip"><td class="ln"></td><td class="ln"></td>'
                f'<td class="code">{esc(raw)}</td></tr>'
            )
            continue
        if raw.startswith("+"):
            cls = "add"
            new_cell = str(new_no)
            new_no += 1
        elif raw.startswith("-"):
            cls = "del"
            old_cell = str(old_no)
            old_no += 1
        elif raw.startswith("\\"):
            pass
        else:
            old_cell = str(old_no)
            new_cell = str(new_no)
            old_no += 1
            new_no += 1
        highlight = ""
        if anchor_line is not None and (old_cell == str(anchor_line) or new_cell == str(anchor_line)):
            highlight = " anchor"
        rows.append(
            f'<tr class="{cls}{highlight}"><td class="ln">{old_cell}</td>'
            f'<td class="ln">{new_cell}</td><td class="code">{esc(raw)}</td></tr>'
        )
    return f'<table class="diff">{"".join(rows)}</table>'


def render_before(evidence: dict[str, Any]) -> str:
    """Render the code around the commented line at start_sha."""

    bw = (evidence or {}).get("before_window")
    if not bw:
        err = (evidence or {}).get("before_error") or (evidence or {}).get("fetch_error") or "无法获取改前代码"
        return f'<div class="muted">{esc(err)}</div>'
    anchor = bw.get("anchor")
    refs = set(bw.get("referenced_lines") or [])
    rows = []
    for n, text in bw.get("lines", []):
        cls = "anchor" if n == anchor else ("ref" if n in refs else "")
        rows.append(
            f'<tr class="{cls}"><td class="ln">{n}</td><td class="code">{esc(text)}</td></tr>'
        )
    return f'<table class="diff before">{"".join(rows)}</table>'


def render_split_diff(patch: str | None, anchor_line: int | None = None) -> str:
    """Render a unified diff as a side-by-side table: 左=原代码 / 右=修复后代码."""

    if not patch or not patch.strip():
        return '<div class="muted">没有可显示的修法 diff。</div>'

    added = sum(1 for l in patch.splitlines() if l.startswith("+") and not l.startswith("+++"))
    deleted = sum(1 for l in patch.splitlines() if l.startswith("-") and not l.startswith("---"))
    banner = ""
    if deleted == 0 and added:
        banner = (
            '<div class="splitnote">本例修法为<b>纯新增</b>（+{a}/-0）：原代码里没有对应片段，'
            '左侧「原代码」因此基本为空——重点看<b>右侧绿色新增</b>。</div>'.format(a=added)
        )
    elif added == 0 and deleted:
        banner = (
            '<div class="splitnote">本例修法为<b>纯删除</b>（+0/-{d}）：'
            '右侧「修复后」对应位置为空，重点看<b>左侧红色删除</b>。</div>'.format(d=deleted)
        )

    rows: list[str] = []
    old_no = new_no = 0
    pending_del: list[tuple[int, str]] = []
    pending_add: list[tuple[int, str]] = []

    def flush() -> None:
        for i in range(max(len(pending_del), len(pending_add))):
            if i < len(pending_del):
                ln_o, txt_o = pending_del[i]
                left = f'<td class="ln">{ln_o}</td><td class="code old del">{esc(txt_o)}</td>'
            else:
                left = '<td class="ln"></td><td class="code old empty"></td>'
            if i < len(pending_add):
                ln_n, txt_n = pending_add[i]
                right = f'<td class="ln">{ln_n}</td><td class="code new add">{esc(txt_n)}</td>'
            else:
                right = '<td class="ln"></td><td class="code new empty"></td>'
            rows.append(f"<tr>{left}{right}</tr>")
        pending_del.clear()
        pending_add.clear()

    for raw in patch.splitlines():
        header = HUNK_HEADER_RE.match(raw)
        if header:
            flush()
            old_no = int(header.group(1))
            new_no = int(header.group(2))
            rows.append(f'<tr class="hunk"><td class="ln"></td><td class="code" colspan="3">{esc(raw)}</td></tr>')
            continue
        if raw.startswith("…"):
            flush()
            rows.append(f'<tr class="skip"><td class="ln"></td><td class="code" colspan="3">{esc(raw)}</td></tr>')
            continue
        if raw.startswith("+"):
            pending_add.append((new_no, raw[1:]))
            new_no += 1
        elif raw.startswith("-"):
            pending_del.append((old_no, raw[1:]))
            old_no += 1
        elif raw.startswith("\\"):
            continue
        else:
            flush()
            hl = " anchor" if anchor_line is not None and (old_no == anchor_line or new_no == anchor_line) else ""
            body = esc(raw[1:] if raw.startswith(" ") else raw)
            rows.append(
                f'<tr class="ctx{hl}"><td class="ln">{old_no}</td><td class="code old">{body}</td>'
                f'<td class="ln">{new_no}</td><td class="code new">{body}</td></tr>'
            )
            old_no += 1
            new_no += 1
    flush()
    colgroup = '<colgroup><col class="cln"><col class="ccode"><col class="cln"><col class="ccode"></colgroup>'
    return f'{banner}<table class="diff split">{colgroup}{"".join(rows)}</table>'


def quality_stars(score: Any) -> str:
    try:
        n = int(score)
    except (TypeError, ValueError):
        return ""
    n = max(0, min(5, n))
    return "★" * n + "☆" * (5 - n)


def label_badge(label: str, value: Any) -> str:
    val = "" if value is None else str(value).lower()
    fg, bg, border = LABEL_COLORS.get(val, LABEL_COLORS["unclear"])
    icon = {"yes": "✓", "no": "✗", "partial": "~", "unclear": "?"}.get(val, "?")
    return (
        f'<span class="lbl" style="color:{fg};background:{bg};border-color:{border}">'
        f'{icon} {esc(label)}</span>'
    )


MODEL_LABELS = {
    "golden": "Golden（人类参考）",
    "empty": "Empty（不改，地板）",
    "model:gpt-5.5": "GPT-5.5",
    "model:claude-opus-4.8": "Claude-Opus-4.8",
    "model:gemini-3.1-pro-preview": "Gemini-3.1-Pro",
    "model:gemini-3.5-flash": "Gemini-3.5-Flash",
}
MODEL_ORDER = [
    "golden",
    "model:gemini-3.5-flash",
    "model:claude-opus-4.8",
    "model:gpt-5.5",
    "model:gemini-3.1-pro-preview",
    "empty",
]


def render_scoreboard(items: list[dict[str, Any]]) -> str:
    """Render an at-a-glance leaderboard from per-task model results."""

    modes = []
    for mode in MODEL_ORDER:
        if mode in {"golden", "empty"}:
            continue
        if any(mode in (item.get("task", {}).get("model_results") or {}) for item in items):
            modes.append(mode)
    if not modes:
        return ""

    total = len(items)
    hard_total = sum(
        (item.get("difficulty") or {}).get("tier") == "hard"
        for item in items
    )
    rows = []
    for mode in modes:
        values = [
            (item.get("task", {}).get("model_results") or {}).get(mode) or {}
            for item in items
        ]
        hard_values = [
            value
            for item, value in zip(items, values)
            if (item.get("difficulty") or {}).get("tier") == "hard"
        ]
        rows.append(
            {
                "mode": mode,
                "label": MODEL_LABELS.get(mode, mode),
                "resolved": sum(value.get("resolved") is True for value in values),
                "generated": sum(value.get("generation_apply") is True for value in values),
                "correctness": sum(value.get("correctness_ok") is True for value in values),
                "maintainability": sum(value.get("maintainability") == "yes" for value in values),
                "hard": sum(value.get("resolved") is True for value in hard_values),
            }
        )
    rows.sort(key=lambda row: (-row["resolved"], row["label"]))
    best_resolved = max(row["resolved"] for row in rows)
    best_hard = max(row["hard"] for row in rows)

    cards = []
    for rank, row in enumerate(rows, start=1):
        overall_badge = '<span class="score-badge overall">总榜第一</span>' if row["resolved"] == best_resolved else ""
        hard_badge = '<span class="score-badge hard">Hard 第一</span>' if row["hard"] == best_hard else ""
        rate = round(100 * row["resolved"] / total) if total else 0
        cards.append(
            f"""
<article class="score-card{' winner' if row['resolved'] == best_resolved else ''}">
  <div class="score-head">
    <span class="score-rank">#{rank}</span>
    <span class="score-model">{esc(row['label'])}</span>
    <span class="score-badges">{overall_badge}{hard_badge}</span>
  </div>
  <div class="score-main"><strong>{row['resolved']}</strong><span>/{total} Resolved@1</span></div>
  <div class="score-track"><span style="width:{rate}%"></span></div>
  <div class="score-gates">
    <div><b>{row['generated']}</b><span>成功生成补丁</span></div>
    <div><b>{row['correctness']}</b><span>Correctness</span></div>
    <div><b>{row['maintainability']}</b><span>维护性</span></div>
    <div><b>{row['hard']}/{hard_total}</b><span>Hard</span></div>
  </div>
</article>"""
        )
    return f"""
<section class="scoreboard">
  <div class="scoreboard-title">
    <div><span class="score-kicker">最终评测结果</span><h2>{total} 题三模型总榜</h2></div>
    <div class="score-formula">Resolved@1 = 成功生成补丁且范围合规 ∧ P2P 测试通过 ∧ 严格维护性通过</div>
  </div>
  <div class="score-grid">{''.join(cards)}</div>
</section>"""


def render_model_results(task: dict[str, Any]) -> str:
    mr = task.get("model_results") or {}
    if not mr:
        return ""
    p2p = task.get("pass_to_pass")
    rows = []
    for mode in MODEL_ORDER:
        r = mr.get(mode)
        if not r:
            continue
        resolved = r.get("resolved")
        if resolved is True:
            verdict, vc = "✅ Resolved", "#1a7f37"
        else:
            verdict, vc = "❌ Not resolved", "#cf222e"

        def gate(ok, yes, no):
            if ok is True:
                return f'<span class="g ok">{yes}</span>'
            if ok is False:
                return f'<span class="g no">{no}</span>'
            return '<span class="g na">–</span>'

        maint = r.get("maintainability")
        maint_quality = r.get("maintainability_quality_1_to_5")
        quality_suffix = f" q{maint_quality}" if maint_quality is not None else ""
        maint_html = {
            "yes": f'<span class="g ok">维护✓{quality_suffix}</span>',
            "partial": f'<span class="g part">维护~{quality_suffix}</span>',
            "no": f'<span class="g no">维护✗{quality_suffix}</span>',
        }.get(maint, '<span class="g na">维护–</span>')
        gates = (
            gate(r.get("correctness_ok"), "测试✓", "测试✗")
            + gate(r.get("scope_ok"), "范围✓", "范围✗")
            + maint_html
        )
        reason = esc((r.get("judge_reason") or "").strip())
        reason_html = f'<div class="jr">{reason}</div>' if reason and mode != "golden" else ""
        rows.append(
            f'<div class="mr-row"><div class="mr-name">{esc(MODEL_LABELS.get(mode, mode))}</div>'
            f'<div class="mr-verdict" style="--vc:{vc}">{verdict}</div>'
            f'<div class="mr-gates">{gates}</div></div>{reason_html}'
        )
    p2p_html = f'<span class="muted">PASS_TO_PASS={esc(p2p)} 个测试</span>' if p2p else ""
    return (
        '<div class="modelres">'
        f'<div class="mr-label">🤖 模型评测结果（Resolved@1 = 测试通过 ∧ 范围合规 ∧ judge 认为已解决） {p2p_html}</div>'
        + "".join(rows)
        + "</div>"
    )


def render_case(item: dict[str, Any]) -> str:
    task = item.get("task", {})
    pr = item.get("original_pr", {})
    rc = item.get("original_review_comment", {})
    ann = task.get("human_annotation", {})
    rank = item.get("rank")

    category = ann.get("label_best_category") or task.get("human_category") or task.get("category") or "?"
    orig_category = task.get("category")
    gate = task.get("human_gate_type") or task.get("gate_type") or "?"
    language = task.get("inferred_language") or ann.get("language") or "?"
    quality = ann.get("label_quality_score_1_to_5")
    repo = task.get("repo", "?")
    pr_number = task.get("pr_number")
    pr_url = task.get("pr_url") or pr.get("url") or "#"
    comment_url = rc.get("url") or (task.get("comment") or {}).get("url") or pr_url
    locality = task.get("locality", {})
    path = locality.get("path") or rc.get("path") or "?"
    line = locality.get("line") or rc.get("line")
    anchor = rc.get("original_line") or rc.get("line") or line
    cat_color = CATEGORY_COLORS.get(category, "#59636e")

    secondary_cat = ""
    if orig_category and orig_category != category:
        secondary_cat = f'<span class="pill ghost">原判:{esc(orig_category)}</span>'

    verification = "".join(
        label_badge(name, ann.get(key))
        for name, key in (
            ("keep", "label_keep_for_benchmark"),
            ("maintainability", "label_is_maintainability_comment"),
            ("clear", "label_task_is_clear"),
            ("localized", "label_task_localized_to_comment"),
            ("no-leak", "label_no_answer_leakage_after_sanitization"),
            ("addressed", "label_comment_was_addressed"),
            ("oracle", "label_correctness_oracle_usable"),
        )
    )

    why = "".join(f"<li>{render_inline(reason)}</li>" for reason in item.get("why_kept", []))
    oracle = task.get("correctness_oracle_proxy", {}) or {}
    test_paths = oracle.get("test_change_paths") or []
    oracle_clues = "".join(f"<li><code>{esc(p)}</code></li>" for p in test_paths)

    addressed = task.get("addressed_evidence", {})
    addressed_str = " · ".join(
        f"{k}={esc(addressed.get(k))}"
        for k in ("thread_resolved", "file_touched_after_comment", "region_touched_after_comment")
    )

    ev = item.get("evidence", {}) or {}
    fix = ev.get("fix_patch", {}) or {}
    difficulty = item.get("difficulty", {}) or {}
    tier = difficulty.get("tier")
    tier_color = {"hard": "#cf222e", "medium": "#9a6700", "easy": "#1a7f37"}.get(tier, "#59636e")
    difficulty_badge = (
        f'<span class="pill" style="--c:{tier_color}" title="+{difficulty.get("added")}/-{difficulty.get("deleted")} '
        f'hunks={difficulty.get("hunks")} new_defs={difficulty.get("new_defs")}">难度 {esc(tier)} ·{esc(difficulty.get("score"))}</span>'
        if tier
        else ""
    )
    summary_zh = item.get("summary_zh") or ""
    summary_html = (
        f'<div class="summary"><div class="summary-label">🧭 这个 case 在干啥（AI 总结，供快速理解，请以下方原始证据为准）</div>'
        f'<div class="summary-text">{render_inline(summary_zh)}</div></div>'
        if summary_zh.strip()
        else ""
    )
    fix_src = fix.get("source")
    if fix_src and fix_src.startswith("start..target"):
        fix_src_label = '<span class="srcbadge ok">真实修法 start→target（两点 diff）</span>'
    elif fix_src and fix_src.startswith("comment..head"):
        fix_src_label = '<span class="srcbadge ok">评论后增量 start→head</span>'
    elif fix_src and fix_src.startswith("base..head"):
        fix_src_label = '<span class="srcbadge warn">PR 整体改动 base→head（评论后该文件未再改）</span>'
    else:
        fix_src_label = ""
    fix_note = fix.get("note")
    fix_note_html = f'<div class="splitnote">⚠ {esc(fix_note)}</div>' if (fix_note and not (fix.get("text") or "").strip()) else ""
    fix_text = fix.get("text") or ""
    fix_html = render_diff(fix_text, None) if fix_text.strip() else f'<div class="muted">{esc(fix_note) if fix_note else "没有可显示的修法 diff。"}</div>'
    fix_split_html = fix_note_html + render_split_diff(fix_text)
    before_html = render_before(ev)

    pr_body = pr.get("body") or ""
    pr_body_html = render_inline(pr_body[:1600]) + ("…" if len(pr_body) > 1600 else "")

    return f"""
<section class="card" id="case-{rank}">
  <div class="card-head">
    <div class="head-left">
      <span class="rank">#{esc(rank)}</span>
      <div>
        <div class="title-row">
          <a class="repo" href="{esc(pr_url)}" target="_blank">{esc(repo)} <span class="prnum">#{esc(pr_number)}</span></a>
          {difficulty_badge}
          <span class="pill" style="--c:{cat_color}">{esc(category)}</span>
          {secondary_cat}
          <span class="pill ghost">{esc(gate)}</span>
          <span class="pill ghost">{esc(language)}</span>
          <span class="stars" title="quality {esc(quality)}/5">{quality_stars(quality)}</span>
        </div>
        <div class="loc"><code>{esc(path)}{':' + str(line) if line else ''}</code>
          &nbsp;·&nbsp; <a href="{esc(comment_url)}" target="_blank">查看 review comment ↗</a></div>
      </div>
    </div>
    <button class="check-btn" data-case="{esc(rank)}">○ 待检查</button>
  </div>

  {summary_html}

  <div class="task-box">
    <div class="task-label">Task instruction</div>
    <div class="task-text">{render_inline(task.get('task_instruction'))}</div>
  </div>

  {render_model_results(task)}

  <div class="verify">{verification}</div>

  <div class="step">
    <div class="step-h"><span class="num">①</span> Reviewer 在抱怨什么 <span class="muted">@{esc(rc.get('author'))} · {esc(path)}{':' + str(line) if line else ''} · {esc(category)}</span></div>
    <div class="comment">{render_comment_body(rc.get('body'))}</div>
  </div>

  <div class="step">
    <div class="step-h"><span class="num">②</span> 改前代码（上下文）<span class="muted">start_sha 处，黄=评论锚定行 {esc(anchor)}{('，蓝=评论提到的行' if (ev.get('before_window') or {}).get('referenced_lines') else '')}</span></div>
    <div class="diff-wrap">{before_html}</div>
  </div>

  <div class="step">
    <div class="step-h"><span class="num">③</span> 修复对比 <span class="splitlabel"><span class="dot-old"></span>左 = 原代码（低质量）　<span class="dot-new"></span>右 = 按评论修复后</span> {fix_src_label}</div>
    <div class="diff-wrap">{fix_split_html}</div>
  </div>

  <details>
    <summary>为什么验收 · addressed evidence · 统一 diff · 原始 diff_hunk</summary>
    <ul class="why">{why}</ul>
    <div class="muted">addressed: {addressed_str}</div>
    {f'<div class="muted">correctness 测试线索:</div><ul class="why">{oracle_clues}</ul>' if oracle_clues else ''}
    <div class="muted" style="margin-top:8px">修法（统一 diff 视图）:</div>
    <div class="diff-wrap">{fix_html}</div>
    <div class="muted" style="margin-top:8px">评论时刻的 diff_hunk（base→评论 commit，仅作参考）:</div>
    <div class="diff-wrap">{render_diff(rc.get('diff_hunk') or locality.get('diff_hunk'), anchor)}</div>
  </details>

  <details>
    <summary>Original PR · {esc(pr.get('title'))}</summary>
    <div class="muted">作者 {esc(pr.get('author'))} · 合并 {esc(pr.get('merged_at'))}</div>
    <pre class="pr-body">{pr_body_html}</pre>
  </details>
</section>"""


def build_html(items: list[dict[str, Any]], title: str, subtitle: str) -> str:
    nav = "".join(
        f'<a href="#case-{it.get("rank")}" data-nav="{it.get("rank")}">'
        f'<span class="dot" style="background:{CATEGORY_COLORS.get((it.get("task", {}).get("human_annotation", {}).get("label_best_category") or it.get("task", {}).get("category")), "#59636e")}"></span>'
        f'<span class="nav-txt">#{it.get("rank")} {esc(it.get("task", {}).get("repo"))}</span>'
        f'<span class="nav-chk" data-navchk="{it.get("rank")}"></span></a>'
        for it in items
    )
    cases = "".join(render_case(it) for it in items)
    scoreboard = render_scoreboard(items)
    legend = "".join(
        f'<span class="leg"><span class="dot" style="background:{color}"></span>{esc(name)}</span>'
        for name, color in CATEGORY_COLORS.items()
    )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>
:root {{ --bg:#f6f8fa; --card:#fff; --text:#1f2328; --muted:#59636e; --border:#d0d7de; --accent:#0969da; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif; background:var(--bg); color:var(--text); }}
header {{ position:sticky; top:0; z-index:5; background:rgba(255,255,255,.95); border-bottom:1px solid var(--border); backdrop-filter:blur(8px); padding:14px 24px; }}
header h1 {{ margin:0 0 4px; font-size:20px; }}
header .sub {{ color:var(--muted); font-size:13px; }}
.bar {{ display:flex; align-items:center; gap:18px; flex-wrap:wrap; margin-top:8px; }}
.legend {{ display:flex; gap:12px; flex-wrap:wrap; font-size:12px; color:var(--muted); }}
.leg {{ display:inline-flex; align-items:center; gap:5px; }}
.dot {{ width:10px; height:10px; border-radius:50%; display:inline-block; }}
.progress {{ margin-left:auto; font-weight:600; font-size:13px; }}
.progress b {{ color:var(--accent); }}
.scoreboard {{ max-width:1480px; margin:20px auto 0; padding:20px; background:linear-gradient(135deg,#f0f7ff,#fff); border:1px solid #b6e3ff; border-radius:16px; box-shadow:0 4px 16px rgba(9,105,218,.08); }}
.scoreboard-title {{ display:flex; justify-content:space-between; align-items:end; gap:20px; margin-bottom:14px; }}
.scoreboard-title h2 {{ margin:2px 0 0; font-size:24px; }}
.score-kicker {{ color:#0969da; font-size:12px; font-weight:800; letter-spacing:.08em; text-transform:uppercase; }}
.score-formula {{ max-width:620px; color:#57606a; font-size:13px; line-height:1.5; text-align:right; }}
.score-grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:14px; }}
.score-card {{ background:#fff; border:1px solid #d0d7de; border-radius:12px; padding:15px; }}
.score-card.winner {{ border-color:#54aeff; box-shadow:0 0 0 2px #ddf4ff; }}
.score-head {{ display:flex; align-items:center; gap:8px; min-height:28px; }}
.score-rank {{ color:#8c959f; font-weight:800; }}
.score-model {{ font-weight:750; font-size:15px; }}
.score-badges {{ margin-left:auto; display:flex; gap:5px; flex-wrap:wrap; justify-content:flex-end; }}
.score-badge {{ border-radius:999px; padding:2px 7px; font-size:10px; font-weight:750; }}
.score-badge.overall {{ color:#0550ae; background:#ddf4ff; border:1px solid #54aeff; }}
.score-badge.hard {{ color:#a40e26; background:#ffebe9; border:1px solid #ff8182; }}
.score-main {{ margin:10px 0 7px; display:flex; align-items:baseline; gap:5px; }}
.score-main strong {{ font-size:32px; line-height:1; }}
.score-main span {{ color:#57606a; font-size:13px; }}
.score-track {{ height:7px; border-radius:999px; background:#eaeef2; overflow:hidden; }}
.score-track span {{ display:block; height:100%; background:linear-gradient(90deg,#0969da,#54aeff); border-radius:999px; }}
.score-gates {{ display:grid; grid-template-columns:repeat(4,1fr); gap:8px; margin-top:12px; }}
.score-gates div {{ min-width:0; }}
.score-gates b {{ display:block; font-size:15px; }}
.score-gates span {{ display:block; color:#6e7781; font-size:10px; white-space:nowrap; }}
.layout {{ display:grid; grid-template-columns:240px minmax(0,1fr); gap:20px; max-width:1480px; margin:0 auto; padding:20px; }}
nav {{ position:sticky; top:120px; align-self:start; background:var(--card); border:1px solid var(--border); border-radius:12px; padding:8px; max-height:calc(100vh - 140px); overflow:auto; }}
nav a {{ display:flex; align-items:center; gap:8px; color:var(--text); text-decoration:none; padding:7px 9px; border-radius:8px; font-size:13px; }}
nav a:hover {{ background:#eef4ff; }}
nav a.active {{ background:#ddf4ff; font-weight:600; }}
.nav-txt {{ flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.nav-chk {{ color:#1a7f37; font-weight:700; }}
.card {{ background:var(--card); border:1px solid var(--border); border-radius:14px; padding:20px; margin-bottom:20px; box-shadow:0 1px 2px rgba(0,0,0,.04); scroll-margin-top:130px; }}
.card.checked {{ border-color:#aceebb; box-shadow:0 0 0 2px #dafbe1; }}
.card-head {{ display:flex; justify-content:space-between; gap:16px; align-items:flex-start; }}
.head-left {{ display:flex; gap:14px; align-items:flex-start; }}
.rank {{ font-size:22px; font-weight:800; color:var(--muted); }}
.title-row {{ display:flex; align-items:center; gap:8px; flex-wrap:wrap; }}
.repo {{ font-weight:700; font-size:16px; color:var(--text); text-decoration:none; }}
.repo:hover {{ color:var(--accent); }}
.prnum {{ color:var(--accent); }}
.pill {{ background:var(--c,#59636e); color:#fff; border-radius:999px; padding:3px 10px; font-size:12px; font-weight:600; }}
.pill.ghost {{ background:#eef1f4; color:var(--muted); border:1px solid var(--border); }}
.stars {{ color:#e3b341; font-size:15px; letter-spacing:1px; }}
.loc {{ margin-top:6px; font-size:13px; color:var(--muted); }}
.loc code {{ background:#eef1f4; padding:1px 6px; border-radius:5px; }}
.check-btn {{ flex:none; cursor:pointer; border:1px solid var(--border); background:#fff; color:var(--muted); border-radius:8px; padding:7px 12px; font-size:13px; font-weight:600; }}
.check-btn.on {{ background:#1a7f37; border-color:#1a7f37; color:#fff; }}
.task-box {{ margin:16px 0; background:#fff8c5; border:1px solid #f0d98c; border-radius:10px; padding:12px 14px; }}
.summary {{ margin:14px 0; background:#eef6ff; border:1px solid #b6e3ff; border-left:4px solid #0969da; border-radius:10px; padding:12px 14px; }}
.summary-label {{ font-size:12px; font-weight:700; color:#0550ae; margin-bottom:6px; }}
.summary-text {{ font-size:14.5px; line-height:1.6; }}
.task-label {{ font-size:11px; text-transform:uppercase; letter-spacing:.08em; color:#9a6700; font-weight:700; margin-bottom:4px; }}
.task-text {{ font-size:15px; line-height:1.55; }}
.verify {{ display:flex; flex-wrap:wrap; gap:6px; margin-bottom:14px; }}
.modelres {{ margin:14px 0; background:#f6f8fa; border:1px solid #d0d7de; border-left:4px solid #6e40c9; border-radius:10px; padding:12px 14px; }}
.mr-label {{ font-size:12px; font-weight:700; color:#6e40c9; margin-bottom:8px; }}
.mr-row {{ display:flex; align-items:center; gap:12px; padding:5px 0; border-top:1px solid #eaeef2; }}
.mr-row:first-of-type {{ border-top:none; }}
.mr-name {{ flex:0 0 168px; font-weight:600; font-size:14px; }}
.mr-verdict {{ flex:0 0 130px; font-weight:700; font-size:13px; color:var(--vc); }}
.mr-gates {{ display:flex; gap:5px; flex-wrap:wrap; }}
.mr-gates .g {{ font-size:11px; padding:1px 7px; border-radius:999px; border:1px solid; }}
.g.ok {{ color:#1a7f37; border-color:#aceebb; background:#dafbe1; }}
.g.no {{ color:#cf222e; border-color:#ffcecb; background:#ffebe9; }}
.g.part {{ color:#9a6700; border-color:#f0d98c; background:#fff8c5; }}
.g.na {{ color:#8c959f; border-color:#d0d7de; background:#f6f8fa; }}
.jr {{ font-size:12.5px; color:#57606a; line-height:1.5; margin:0 0 4px 180px; padding:4px 8px; background:#fff; border-radius:6px; border:1px solid #eaeef2; }}
.lbl {{ border:1px solid; border-radius:999px; padding:2px 9px; font-size:12px; font-weight:600; }}
.cols {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
.col-h {{ font-size:13px; font-weight:700; margin-bottom:6px; }}
.step {{ margin-bottom:14px; }}
.step-h {{ font-size:13px; font-weight:700; margin-bottom:6px; display:flex; align-items:center; gap:6px; flex-wrap:wrap; }}
.num {{ display:inline-flex; align-items:center; justify-content:center; width:20px; height:20px; border-radius:50%; background:var(--accent); color:#fff; font-size:12px; }}
.srcbadge {{ font-size:11px; font-weight:600; border-radius:999px; padding:2px 8px; }}
.srcbadge.ok {{ color:#1a7f37; background:#dafbe1; border:1px solid #aceebb; }}
.srcbadge.warn {{ color:#9a6700; background:#fff8c5; border:1px solid #f0d98c; }}
.comment {{ background:#fbfbfc; border:1px solid var(--border); border-left:4px solid #bf3989; border-radius:8px; padding:12px; font-size:14px; line-height:1.5; overflow-wrap:anywhere; }}
.suggestion {{ margin:8px 0; border:1px dashed #bc4c00; border-radius:8px; background:#fff4ec; padding:6px 8px; }}
.suggestion-tag {{ font-size:11px; color:#bc4c00; font-weight:700; }}
.suggestion pre {{ margin:4px 0 0; background:transparent; border:0; padding:0; white-space:pre-wrap; }}
.diff-wrap {{ border:1px solid var(--border); border-radius:8px; overflow:auto; max-height:460px; }}
table.diff {{ border-collapse:collapse; width:100%; font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:12.5px; }}
table.diff td {{ padding:0 8px; vertical-align:top; white-space:pre; }}
table.diff td.ln {{ width:1%; text-align:right; color:#8c959f; background:#f6f8fa; border-right:1px solid var(--border); user-select:none; }}
table.diff td.code {{ width:100%; }}
tr.add td.code {{ background:#e6ffec; }}
tr.add td.code::before {{ content:""; }}
tr.del td.code {{ background:#ffebe9; }}
tr.hunk td {{ background:#ddf4ff; color:#0550ae; }}
tr.skip td.code {{ color:#8c959f; background:#f6f8fa; font-style:italic; }}
tr.anchor td.code {{ box-shadow:inset 3px 0 0 #e3b341; background:#fff8c5; }}
tr.ref td.code {{ box-shadow:inset 3px 0 0 #54aeff; background:#ddf4ff; }}
table.before td.code {{ background:#fff; }}
table.split {{ table-layout:fixed; width:100%; }}
table.split col.cln {{ width:3.2em; }}
table.split td.code {{ overflow-wrap:anywhere; white-space:pre-wrap; }}
table.split td.code.old {{ border-right:2px solid var(--border); }}
table.split td.code.old.del {{ background:#ffebe9; }}
table.split td.code.new.add {{ background:#e6ffec; }}
table.split td.code.empty {{ background:#f6f8fa; }}
table.split tr.ctx.anchor td.code {{ background:#fff8c5; }}
.splitlabel {{ font-size:12px; color:var(--muted); font-weight:400; }}
.splitnote {{ font-size:12.5px; color:#9a6700; background:#fff8c5; border:1px solid #f0d98c; border-radius:8px; padding:6px 10px; margin-bottom:6px; }}
.dot-old, .dot-new {{ display:inline-block; width:9px; height:9px; border-radius:2px; margin-right:3px; vertical-align:middle; }}
.dot-old {{ background:#ffcecb; border:1px solid #ff8c87; }}
.dot-new {{ background:#aceebb; border:1px solid #4ac26b; }}
details {{ margin-top:14px; }}
summary {{ cursor:pointer; font-weight:600; font-size:14px; }}
.why {{ margin:8px 0; padding-left:18px; }}
.why li {{ margin:3px 0; }}
.pr-body {{ white-space:pre-wrap; overflow-wrap:anywhere; background:#f6f8fa; border:1px solid var(--border); border-radius:8px; padding:10px; max-height:240px; overflow:auto; font-size:13px; }}
.muted {{ color:var(--muted); }}
code {{ background:#eef1f4; border-radius:5px; padding:1px 4px; }}
@media (max-width:980px) {{ .scoreboard-title {{ align-items:flex-start; flex-direction:column; }} .score-formula {{ text-align:left; }} .score-grid {{ grid-template-columns:1fr; }} .layout {{ grid-template-columns:1fr; }} nav {{ position:static; max-height:none; }} .cols {{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>
<header>
  <h1>{esc(title)}</h1>
  <div class="sub">{esc(subtitle)}</div>
  <div class="bar">
    <div class="legend">{legend}</div>
    <div class="progress">已检查 <b id="done">0</b> / {len(items)} <span class="muted">(快捷键 j/k 跳转，点右上角按钮标记)</span></div>
  </div>
</header>
{scoreboard}
<div class="layout">
  <nav>{nav}</nav>
  <main>{cases}</main>
</div>
<script>
const KEY = "reviewbench-checked-" + location.pathname;
let checked = {{}};
try {{ checked = JSON.parse(localStorage.getItem(KEY) || "{{}}"); }} catch(e) {{}}

function paint() {{
  let n = 0;
  document.querySelectorAll(".check-btn").forEach(btn => {{
    const id = btn.dataset.case, on = !!checked[id];
    btn.classList.toggle("on", on);
    btn.textContent = on ? "✓ 已检查" : "○ 待检查";
    btn.closest(".card").classList.toggle("checked", on);
    const nv = document.querySelector('[data-navchk="' + id + '"]');
    if (nv) nv.textContent = on ? "✓" : "";
    if (on) n++;
  }});
  document.getElementById("done").textContent = n;
}}
document.querySelectorAll(".check-btn").forEach(btn => {{
  btn.addEventListener("click", () => {{
    const id = btn.dataset.case;
    checked[id] = !checked[id];
    localStorage.setItem(KEY, JSON.stringify(checked));
    paint();
  }});
}});
paint();

const cards = [...document.querySelectorAll(".card")];
let cur = 0;
document.addEventListener("keydown", e => {{
  if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
  if (e.key === "j") {{ cur = Math.min(cards.length - 1, cur + 1); cards[cur].scrollIntoView({{behavior:"smooth", block:"start"}}); }}
  if (e.key === "k") {{ cur = Math.max(0, cur - 1); cards[cur].scrollIntoView({{behavior:"smooth", block:"start"}}); }}
}});
const navlinks = [...document.querySelectorAll("nav a")];
const obs = new IntersectionObserver(entries => {{
  entries.forEach(en => {{
    if (en.isIntersecting) {{
      const id = en.target.id.replace("case-", "");
      navlinks.forEach(a => a.classList.toggle("active", a.dataset.nav === id));
      cur = cards.indexOf(en.target);
    }}
  }});
}}, {{ rootMargin: "-120px 0px -60% 0px" }});
cards.forEach(c => obs.observe(c));
</script>
</body>
</html>"""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="input", type=Path, required=True, help="Rendered cases JSON (a list of items).")
    parser.add_argument("--out", type=Path, required=True, help="Output HTML path.")
    parser.add_argument("--title", default="ReviewBench 人工验收 strict 案例检查")
    parser.add_argument("--subtitle", default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    items = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(items, list):
        raise SystemExit("Input JSON must be a list of case objects.")
    subtitle = args.subtitle or f"来源 {args.input.name} · 共 {len(items)} 条人工验收通过案例"
    html_text = build_html(items, args.title, subtitle)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html_text, encoding="utf-8")
    print(f"Wrote {len(items)} cases to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
