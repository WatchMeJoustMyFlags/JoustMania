# Test Case: Canary a Backend and Inject Chaos by Hand

## Overview

**ID:** TC-015  
**Goal:** An operator routes half of the controllers to the rust adapter, injects a fault, watches both on the Controllers & Bluetooth dashboard (uid `controller-overview`) and in Grafana Explore queries, and returns everyone to the default backend.  
**Priority:** Medium  
**Status:** Draft  
**Use cases:** [UC-052](../use_cases/UC-052.md) main, A1; [UC-035](../use_cases/UC-035.md) main, A2

## Roles

- Operator (calls the connect-proxy and watches Grafana)

## Preconditions

- The full stack runs with real controllers (the chaos adapter wraps only real adapters), the rust-hid service up, and controllers `A` … `F` connected on the python backend.
- `bluetooth_backend` serves `python` and `chaos_fault_type` serves `none` (`GET /controller/bluetooth_backend`); the connect-proxy writes the `controller` set through the flag service (#1420).

## Flow

| Step | Name                     | Description                                                                                         | Test Data                                    | Use Case                            |
|------|--------------------------|-----------------------------------------------------------------------------------------------------|----------------------------------------------|-------------------------------------|
| 1    | Start the canary         | Operator requests a rust canary for half of the controllers from inside the network                 | `/canary/rust?fraction=50`                   | [UC-052](../use_cases/UC-052.md)    |
| 2    | Verify routing           | `controller_routing_decisions_total` shows about half of the controllers on rust after the next cycle | -                                          | -                                   |
| 3    | Watch the comparison     | Operator opens Controllers & Bluetooth (its "Controllers per Backend" panel counts `controller_backend_info == 1` by `backend`) | `/grafana/d/controller-overview`   | [UC-035](../use_cases/UC-035.md)    |
| 4    | Inject a fault           | Operator injects a fault for the targeted controllers                                               | `/chaos/poll-drop?fraction=50` (`poll_drop`) | [UC-052](../use_cases/UC-052.md)    |
| 5    | Verify the fault         | `controller_chaos_faults_injected_total` grows (Grafana Explore query); `/chaos/status` shows the fault; the controller panels in Grafana show the faulted controllers' poll drops | `/grafana/`                          | [UC-035](../use_cases/UC-035.md)    |
| 6    | Reset chaos              | Operator resets chaos; `/chaos/status` shows `none`                                                 | `/chaos/reset`                               | [UC-052](../use_cases/UC-052.md)    |
| 7    | Roll the canary back     | Operator requests the rollback; every controller returns to python                                  | `/canary/rollback`                           | [UC-052](../use_cases/UC-052.md)    |

## Validation

1. **Back to baseline**: the flag service holds no fraction overlay on either flag; `bluetooth_backend` serves `python` (the rollback's stack overlay) and `chaos_fault_type` serves `none`.

## Postconditions

- The `controller` set serves its precondition values; no flag file changes (C-033).

## Automation

- **Manual** (needs real controllers for the chaos adapter). The connect-proxy handlers are unit-tested against a fake flag service (`services/connect-proxy/canary_test.go`: full and fractional canary, rollback, chaos set and reset, status, refusals and outages), run in its image build.
- Contract-level coverage (no live stack, no controllers): `tests/contract/test_envoy_routing.py` checks the envoy routes for `/grafana/` and `/chaos/` (steps 3, 5, 6; `/canary/` has no envoy route, so steps 1 and 7 call the connect-proxy from inside the network, UC-052 A2).
