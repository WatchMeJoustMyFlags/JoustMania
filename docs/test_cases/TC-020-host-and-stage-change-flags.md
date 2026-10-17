# Test Case: Host and Stage Change Flags at Runtime

## Overview

**ID:** TC-020  
**Goal:** The host changes a setting from a controller and the presenter changes the decision backend with a stage script, both take effect at once through the flag service, and the stage reset clears only the stage's changes.  
**Priority:** High  
**Status:** Automated  
**Use cases:** [UC-012](../use_cases/UC-012.md) A1; [UC-053](../use_cases/UC-053.md) A1, A2; [UC-044](../use_cases/UC-044.md) main

## Roles

- Host (uses the admin combo on one controller)
- Presenter (runs `scripts/agent-backend.sh` and `scripts/talk-stage.sh`)

## Preconditions

- The integration stack is up with the menu running; mock controllers connected; `FLAG_SERVICE_URL` points at the flag service for the scripts.

## Flow

| Step | Name                        | Description                                                                                         | Test Data                                  | Use Case                            |
|------|-----------------------------|-----------------------------------------------------------------------------------------------------|--------------------------------------------|-------------------------------------|
| 1    | Host changes sensitivity    | Host enters admin mode and presses Circle; the menu persists the next `sensitivity` level            | `MOCK0001`, Circle                         | [UC-012](../use_cases/UC-012.md)    |
| 2    | Presenter flips the backend | Presenter runs `agent-backend.sh jev`; a stack overlay with writer `stage`                           | `decision.backend` = `jev`                 | [UC-044](../use_cases/UC-044.md)    |
| 3    | Verify both served          | flagd serves the host's level and `jev`                                                               | -                                          | [UC-053](../use_cases/UC-053.md)    |
| 4    | Stage reset                 | Presenter runs `talk-stage.sh reset`: the stage overlays are cleared and the stage baseline applied; the host's setting stays | `decision.backend` = `rules` | [UC-053](../use_cases/UC-053.md)    |

## Validation

1. **Persisted vs in memory**: the host's change carries an admin value (`persist`), the presenter's does not.
2. **No file changes**: the flag files are unchanged (C-033).

## Postconditions

- The `admin_flags` fixture deletes the persisted admin values; the test clears every remaining `stage` overlay.

## Automation

- `tests/integration/test_admin_flow.py::test_host_and_stage_change_flags_at_runtime` implements steps 1 to 4.
- The script logic itself is covered without a stack by `scripts/tests/test-agent-backend.sh` and `test-talk-stage.sh` (fake flag service with the real registry).
