# DEPRECATED: Use domain.policies.run_state_machine
# Re-exported for backward compatibility.

from .policies.run_state_machine import transition, next_action

__all__ = ["transition", "next_action"]
