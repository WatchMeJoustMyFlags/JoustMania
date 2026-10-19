# Test Case: Canary a Backend by Hand

## Overview

**ID:** TC-015  
**Goal:** An operator routes half of the controllers to the python adapter (opt-in) on top of the rust baseline, watches the split on the Controllers & Bluetooth dashboard (uid `controller-overview`) and in Grafana Explore queries, and rolls back to the rust baseline.  
**Priority:** Medium  
**Status:** Draft  
**Use cases:** [UC-052](../use_cases/UC-052.md) main; [UC-035](../use_cases/UC-035.md) main

## Roles

- Operator (calls the connect-proxy and watches Grafana)

## Preconditions

- The full stack runs with real controllers, the rust-hid service up, the python opt-in active (UC-002 BR-003: `backend=python_rust`, the `python-hid` profile; otherwise step 1 is refused with 409, UC-052 A3), and controllers `A` … `F` connected.
- `bluetooth_backend` serves its baseline default `rust` (`GET /controller/bluetooth_backend`, no stack or fraction overlay); the connect-proxy writes the `controller` set through the flag service (#1420).

## Flow

| Step | Name                     | Description                                                                                         | Test Data                                    | Use Case                            |
|------|--------------------------|-----------------------------------------------------------------------------------------------------|----------------------------------------------|-------------------------------------|
| 1    | Start the canary         | Operator requests a python canary for half of the controllers from inside the network               | `/canary/python?fraction=50`                 | [UC-052](../use_cases/UC-052.md)    |
| 2    | Verify routing           | `controller_routing_decisions_total` shows about half of the controllers on python after the next cycle | -                                        | -                                   |
| 3    | Watch the comparison     | Operator opens Controllers & Bluetooth (its "Controllers per Backend" panel counts `controller_backend_info == 1` by `backend`) | `/grafana/d/controller-overview`   | [UC-035](../use_cases/UC-035.md)    |
| 4    | Roll the canary back     | Operator requests the rollback; every controller returns to the rust baseline (#1371), the python canary is removed | `/canary/rollback`              | [UC-052](../use_cases/UC-052.md)    |

## Validation

1. **Back to baseline**: the flag service holds neither a stack nor a fraction overlay on `bluetooth_backend`, which serves its baseline default `rust`.

## Postconditions

- The `controller` set serves its baseline values; no flag file changes (C-033).

## Automation

- **Manual** (needs real controllers). The connect-proxy handlers are unit-tested against a fake flag service (`services/connect-proxy/canary_test.go`: full and fractional canary, rollback, status, refusals, python not loaded (UC-052 A3) and outages), run in its image build.
- Contract-level coverage (no live stack, no controllers): `tests/contract/test_envoy_routing.py` checks the envoy route for `/grafana/` (step 3; `/canary/` has no envoy route, so steps 1 and 4 call the connect-proxy from inside the network, UC-052 A2).
