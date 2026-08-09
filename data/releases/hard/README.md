# Hard releases

Hard releases are dated, immutable subsets focused on tasks whose intrinsic
maintainability difficulty is classified as hard.

| Release | Tasks | Models | Status |
|---|---:|---:|---|
| [`20260808`](20260808/) | 414 | 3 | Current |

The hard release is complementary to `v1/` and `reserves/`:

- 40 tasks are the hard slice of the existing 475-task general release.
- 374 tasks are new, semantic-non-overlapping hard tasks.
- The combined project universe is 849 unique strict tasks: 414 hard and
  435 medium.

Each dated directory must contain a self-contained README, task and Oracle
JSONL, normalized model results, a manifest, and `SHA256SUMS`. Intermediate
worktrees, container layers, dependency caches, and raw logs remain outside
version control.
