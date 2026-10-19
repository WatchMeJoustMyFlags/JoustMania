"""Compositions shared by service-tier modules (#1561). The harness core stays untouched."""

from tests.service.harness import Composition

# The flag plane alone (flag service + flagd): no game services, for tests of the
# flag contract itself. The stack still serves a fake audio, which nothing dials.
FLAG_PLANE = Composition(())
