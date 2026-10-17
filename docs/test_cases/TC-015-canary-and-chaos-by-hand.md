# Test Case: Canary a Backend and Inject Chaos by Hand

## Overview

**ID:** TC-015  
**Goal:** An operator routes half of the controllers to the rust adapter, injects a fault, watches both in controller-overview and Grafana Explore queries, and returns everyone to the default backend.  
**Priority:** Medium  
**Status:** Draft  
**Use cases:** [UC-052](../use_cases/UC-052.md) main, A1; [UC-035](../use_cases/UC-035.md) main, A2

## Roles

- Operator (calls the connect-proxy and watches Grafana)

## Preconditions

- The full stack runs with real controllers (the chaos adapter wraps only real adapters), the rust-hid service up, and controllers `A` … `F` connected on the python backend.
- `bluetooth_backend` default variant `python` and `chaos_fault_type` = `none` in `controller.json`.

## Flow

| Step | Name                     | Description                                                                                         | Test Data                                    | Use Case                            |
|------|--------------------------|-----------------------------------------------------------------------------------------------------|----------------------------------------------|-------------------------------------|
| 1    | Start the canary         | Operator requests a rust canary for half of the controllers from inside the network                 | `/canary/rust?fraction=50`                   | [UC-052](../use_cases/UC-052.md)    |
| 2    | Verify routing           | `controller_routing_decisions_total` shows about half of the controllers on rust after the next cycle | -                                          | -                                   |
| 3    | Watch the comparison     | Operator opens controller-overview and queries `controller_backend_info == 1` by `backend` in Grafana Explore | `/grafana/`                        | [UC-035](../use_cases/UC-035.md)    |
| 4    | Inject a fault           | Operator injects a fault for the targeted controllers                                               | `/chaos/poll-drop?fraction=50` (`poll_drop`) | [UC-052](../use_cases/UC-052.md)    |
| 5    | Verify the fault         | `controller_chaos_faults_injected_total` grows (Grafana Explore query); `/chaos/status` shows the fault; the controller panels in Grafana show the faulted controllers' poll drops | `/grafana/`                          | [UC-035](../use_cases/UC-035.md)    |
| 6    | Reset chaos              | Operator resets chaos; `/chaos/status` shows `none`                                                 | `/chaos/reset`                               | [UC-052](../use_cases/UC-052.md)    |
| 7    | Roll the canary back     | Operator requests the rollback; every controller returns to python                                  | `/canary/rollback`                           | [UC-052](../use_cases/UC-052.md)    |

## Validation

1. **Back to baseline**: `controller.json` holds `bluetooth_backend` default `python` without targeting, and `chaos_fault_type` = `none`.

## Postconditions

- `controller.json` is back to its precondition values; no other flag file changes.

## Automation

- **Manual** (needs real controllers for the chaos adapter). The `/canary/*` handlers are tested in `services/connect-proxy/canary_test.go` (#1388); the `/chaos/*` handlers are untested (#1364).
- Contract-level coverage (no live stack, no controllers): `tests/contract/test_envoy_routing.py` checks the envoy routes for `/grafana/` and `/chaos/` (steps 3, 5, 6; `/canary/` has no envoy route, so steps 1 and 7 call the connect-proxy from inside the network, UC-052 A2).
