"""Domain-layer exceptions. Each carries a stable `error_code` so a rejection
gives the client something better than a bare 409 — see CommandReceipt, §02."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gotiate.domain.events import GameEvent


class DomainError(Exception):
    error_code = "domain_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        # Populated by engine.handle_command when a command fails *after*
        # apply_due_time_transitions already produced legitimate mutations —
        # those still need to be persisted even though the command itself
        # was rejected. See engine.handle_command.
        self.partial_events: list["GameEvent"] = []


class IllegalCommandError(DomainError):
    error_code = "illegal_command"


class StaleVersionError(DomainError):
    error_code = "stale_version"


class NotFoundError(DomainError):
    error_code = "not_found"


class UnableToGenerateStartingStateError(IllegalCommandError):
    """Raised when generate_starting_state exhausts its attempt budget
    without a legal complete starting state -- deliberately never caught
    and silently degraded into a looser one. A hard constraint that can't
    be satisfied is a signal the theme/player-count/config combination is
    incompatible and needs a human, not a quietly worse game. See the
    initial-distribution-quality design writeup."""

    error_code = "unable_to_generate_valid_starting_state"
