# Repository guidance for coding agents

Keep this file short. It defines repository-wide behavior; task-specific workflows live in skills and scripts.

## Scope discipline

Prefer the simplest implementation that satisfies the current requirement.

Do not add non-functional requirements, safeguards, abstractions, infrastructure, or generalized machinery solely to address hypothetical problems that have not occurred and were not requested.

Every meaningful increase in complexity must be justified by at least one of:

- a current requirement;
- an observed problem;
- a concrete high-impact risk that would make the current implementation unsafe, irreversible, or prohibitively expensive to correct later.

When you notice a possible future concern that does not meet those criteria, do not implement a solution for it. Mention it only when it materially affects a current design decision.

## Frontend work

For frontend changes that require visual review, use the repository skill at `.agents/skills/frontend-review/SKILL.md` and the helper commands in `scripts/dev/`.

Do not create a pull request for a visual change until the user has reviewed the preview and explicitly approved the current result.

## Production data

The preview environment may read production camera and analysis data, but it must not write to those data paths. Keep production-data mounts read-only in preview tooling.
