"""Writes an edited stack file and its variables inside the caller's audited transaction."""

from collections.abc import Sequence
from dataclasses import dataclass

from app.db.repositories.environment import Environment, EnvironmentRepository
from app.db.repositories.stack import (
    StackRepository,
    StackVariableError,
    StoredStack,
    VariableWrite,
)
from app.services.activity import current_audit
from app.services.stack_deploy import StackRejected

__all__ = ["StackUpdated", "store_update"]


@dataclass(frozen=True, slots=True)
class StackUpdated:
    """The result of saving a stack; `changed` is false when nothing differed."""

    environment_id: int
    name: str
    file_changed: bool
    variables_changed: int
    variables_removed: int

    @property
    def changed(self) -> bool:
        """Report whether anything was saved."""
        return self.file_changed or bool(self.variables_changed or self.variables_removed)


async def store_update(
    stacks: StackRepository,
    environments: EnvironmentRepository,
    environment: Environment,
    row: StoredStack,
    *,
    text: str,
    writes: Sequence[VariableWrite] | None,
    compose_digest: str,
    compose_version: str,
) -> StackUpdated:
    """Store the file when it differs and the variables when given; call inside the transaction."""
    audit = current_audit()
    file_changed = text != row.compose_file
    if file_changed:
        await stacks.update_file(row.id, compose_file=text, compose_file_digest=compose_digest)
    changed = removed = 0
    if writes is not None:
        try:
            result = await stacks.replace_variables(row.id, writes)
        except StackVariableError:
            raise StackRejected(
                {"variables": "A variable could not be stored."}, "invalid"
            ) from None
        changed, removed = result.changed, result.removed
    await environments.record_compose_version(environment.id, compose_version)
    audit.detail.update(
        compose_digest=compose_digest,
        file_changed=file_changed,
        variables_changed=changed,
        variables_removed=removed,
        compose_version=compose_version,
    )
    if not (file_changed or changed or removed):
        audit.unchanged()
    return StackUpdated(environment.id, row.name, file_changed, changed, removed)
