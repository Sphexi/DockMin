"""SQL fragment restricting a query to the objects a user may reach through a control."""

import re
from dataclasses import dataclass
from typing import Any

from app.db.repositories.resource_control import AccessLevel, ResourceType

_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")

_LEVELS_SATISFYING: dict[AccessLevel, tuple[AccessLevel, ...]] = {
    "read": ("read", "write"),
    "write": ("write",),
}


@dataclass(frozen=True, slots=True)
class SqlFragment:
    """A boolean SQL expression and the values its placeholders bind, in order."""

    clause: str
    params: tuple[Any, ...]


def require_qualified_columns(column: str, environment_column: str) -> None:
    """Refuse a column name that is not a plain, table-qualified identifier."""
    for name in (column, environment_column):
        if _COLUMN.fullmatch(name) is None:
            raise ValueError("column must be a plain column name")
    # Both names are interpolated inside subqueries over resource_control, which has columns of
    # the same names. An unqualified name binds to the inner table, not the caller's row,
    # so the environment match compares a column with itself and admits every host.
    for name in (column, environment_column):
        if "." not in name:
            raise ValueError("column must name its table, as in item.environment_id")


def visible_clause(
    *,
    user_id: int,
    environment_id: int,
    resource_type: ResourceType,
    column: str,
    environment_column: str,
    required: AccessLevel,
    honour_default: bool,
) -> SqlFragment:
    """Build the expression that is true where column names an object the user may reach.

    environment_column is the row's own environment. The expression admits only rows in
    environment_id and matches controls in the row's environment, so a same-named object on
    another host is neither admitted nor mistaken for uncontrolled.

    Membership is evaluated inside the statement, so it is never older than the query. Only the
    object's own control is consulted: a control inherited through a stack label cannot be seen
    from a column, so inheritance never grants here. honour_default adds objects with no control
    of their own, and is for the caller to set only where nothing is inherited.
    """
    require_qualified_columns(column, environment_column)
    levels = _LEVELS_SATISFYING[required]
    marks = ", ".join("?" for _ in levels)
    granted = (
        "EXISTS (SELECT 1 FROM resource_control rc "
        f"WHERE rc.environment_id = {environment_column} AND rc.resource_type = ? "
        f"AND {_same_object('rc', resource_type, column)} "
        f"AND rc.is_administrators_only = 0 AND rc.access_level IN ({marks}) "
        "AND (rc.is_public = 1 OR rc.owner_user_id = ? "
        "OR EXISTS (SELECT 1 FROM resource_control_user_access ua "
        "WHERE ua.resource_control_id = rc.id AND ua.user_id = ?) "
        "OR EXISTS (SELECT 1 FROM resource_control_team_access ta "
        "JOIN team_membership tm ON tm.team_id = ta.team_id "
        "WHERE ta.resource_control_id = rc.id AND tm.user_id = ?)))"
    )
    params: list[Any] = [environment_id, resource_type, *levels, user_id, user_id, user_id]
    admitted = granted
    if honour_default:
        uncontrolled = (
            "NOT EXISTS (SELECT 1 FROM resource_control rc2 "
            f"WHERE rc2.environment_id = {environment_column} AND rc2.resource_type = ? "
            f"AND {_same_object('rc2', resource_type, column)})"
        )
        params.append(resource_type)
        admitted = f"{granted} OR {uncontrolled}"
    return SqlFragment(f"({environment_column} = ? AND ({admitted}))", tuple(params))


def _same_object(alias: str, resource_type: ResourceType, column: str) -> str:
    # lower() on both sides mirrors canonical_stack_key and the unique index over stack names.
    if resource_type == "stack":
        return f"lower({alias}.resource_id) = lower({column})"
    return f"{alias}.resource_id = {column}"
