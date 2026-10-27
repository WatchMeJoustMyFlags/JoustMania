# Known local flakes

Tests that fail intermittently on a developer machine or under load, not because of the change under review.
Read by the `pr-reviewer` agent and the `review-pr` skill. A failure listed here is reported as a known flake
(name the test), never as a PR defect, and never as proof that a mutation was caught.

A test that is NOT listed and fails once but passes on rerun is a finding (flake or race), not infrastructure.
Add a row only with the issue that tracks the flake.

| Test | Symptom | When | Tracking |
|---|---|---|---|

Infrastructure failures in CI (collector `exited (1)`, stale prebuilt images, `blob not found`) are listed in
`.claude/skills/review-pr/SKILL.md` step 5, not here.
