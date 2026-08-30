---
name: frontend-review
description: Iterate on frontend changes in the fixed development worktree using a production-data read-only preview, then create a PR only after explicit visual approval.
---

# Frontend review workflow

Use this workflow for visual frontend work that benefits from repeated human review.

Repository-wide scope and complexity rules in `AGENTS.md` always apply.

## Start

Create a feature branch from the latest `origin/main` by running:

```bash
bash scripts/dev/frontend-start <short-name>
```

Use the existing fixed Codex/development worktree. Do not create a new worktree for each task.

## Implement and verify

Make the requested change on the feature branch. Keep the change scoped to the request; do not add speculative hardening or generalized infrastructure.

Run:

```bash
bash scripts/dev/frontend-check
bash scripts/dev/preview-up
```

The preview stack uses production analysis output and camera videos read-only and is separate from the production Compose project.

Ask the user to inspect the preview. Do not create a PR yet.

## Revise

When the user requests visual adjustments, keep working on the same branch. After each meaningful revision, rerun:

```bash
bash scripts/dev/frontend-check
bash scripts/dev/preview-up
```

Continue until the user explicitly approves the current preview.

## Create the PR

After explicit approval, commit the approved changes, ensure the worktree is clean, then run:

```bash
bash scripts/dev/frontend-pr
```

Do not treat an implicit lack of objections as approval.

## Merge and clean up

Merge only when requested or when the user's instruction clearly includes merging the approved PR. After the PR is merged, run:

```bash
bash scripts/dev/frontend-cleanup
```

This stops the preview, returns the fixed development worktree to detached `origin/main`, and removes the completed local/remote feature branch when safe.

Use `bash scripts/dev/preview-down` when the preview should be stopped without cleaning up the branch.
