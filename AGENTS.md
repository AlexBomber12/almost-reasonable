# Almost Reasonable contributor instructions

This repository uses a manual, test-first workflow. It has no Pipeline Orchestrator, coding daemon, task queue, autonomous merge bot, or related run-record and escalation machinery.

## PLANNED PR

1. Treat the complete, self-contained Codex prompt as the task. Do not search for `tasks/QUEUE.md` or `tasks/PR-*.md`.
2. Verify the repository, requested base and target branches, and a clean working tree. Create one task-specific branch from `origin/main`.
3. Implement only the approved coherent outcome. Keep production changes minimal and independently testable.
4. Write meaningful tests, then run the complete local gate with `scripts/ci.sh`. Do not reduce coverage or disable a failing test.
5. Commit and push the task branch. Open a ready GitHub pull request with real validation evidence, and verify that the remote PR HEAD matches the local HEAD.
6. When the operator authorizes complete task execution, continue on the same PR through the `FIX FEEDBACK` loop until the current HEAD is validated and ready for human merge authorization. Otherwise stop after publication. Never merge automatically.

## FIX FEEDBACK

1. Work on the existing PR branch. Obtain the latest relevant GitHub Actions results and Codex Review comments for its current HEAD; stale results and reviews never qualify.
2. Ignore feedback that applies only to obsolete commits and has already been addressed. Fix every applicable P1/P2 finding unless the human operator explicitly waives it.
3. Make only necessary, coherent regression fixes and avoid unrelated refactors. Add or update tests that demonstrate each correction.
4. Run `scripts/ci.sh` again, commit, and push to the same PR branch.
5. Verify the new remote HEAD and its GitHub Actions results. If a Codex Review is not already running for that HEAD, request one; never send a duplicate request for a running current-HEAD review.
6. Repeat without an artificial iteration limit until current-HEAD CI passes and the latest review has no unresolved actionable P1/P2 findings, or report a genuine blocker with a defensible rationale. Every pushed correction requires fresh current-HEAD validation. Do not weaken CI or tests, and do not merge automatically.

The normal authorized sequence is: implement, run local CI, commit, push, open the PR, wait for GitHub Actions, request and evaluate Codex Review, fix actionable feedback with regression tests, rerun local CI, commit and push, request a current-HEAD review when needed, and repeat before reporting final validation status.

## Merge eligibility

A PR may be merged only when all of the following are true:

- Every applicable GitHub Actions check succeeded on the current PR HEAD.
- The latest Codex Review for the current HEAD completed without unresolved actionable P1/P2 findings. An old review from before the latest push never qualifies.
- When a Codex bot `+1` reaction is available, it is non-stale positive review evidence. A generic `Completed` summary alone is not approval.
- The human operator authorizes the merge.

## Security, testing, and repository rules

- Never commit secrets, access tokens, private keys, real participant data, or such values in fixtures, logs, or PR descriptions. Ordinary tests must not use production API credentials.
- Never commit directly to `main`, force-push `main`, bypass branch protection, weaken CI to obtain green status, or fabricate tests or reviewer approval.
- Do not modify deployment services without an explicitly authorized task. Report genuine blockers instead of looping indefinitely.
- All first-party production Python code must reach 100% line and branch coverage. Coverage does not replace security review, integration testing, behavioral evaluation, or production validation.
