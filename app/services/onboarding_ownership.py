"""The owner and team an onboarding flow gives the objects it adopts or assigns."""

from dataclasses import dataclass
from typing import Final

READ: Final = "read"
WRITE: Final = "write"
LEVELS: Final = (READ, WRITE)


@dataclass(frozen=True, slots=True)
class Ownership:
    """An optional owner and team grant; with neither, objects stay administrators-only."""

    owner_user_id: int | None = None
    owner_name: str = ""
    team_id: int | None = None
    team_name: str = ""
    team_level: str = WRITE

    @property
    def assigns(self) -> bool:
        """Report whether a control would be written at all."""
        return self.owner_user_id is not None or self.team_id is not None

    @property
    def team_ids(self) -> tuple[int, ...]:
        """Return the teams the control grants access to."""
        return () if self.team_id is None else (self.team_id,)

    @property
    def level(self) -> str:
        """Return the access level the control carries."""
        return self.team_level if self.team_id is not None else WRITE
