"""Repositories: the only package in the application that contains SQL."""

from app.db.repositories.activity_event import (
    ActivityEvent,
    ActivityEventRepository,
    ActivityFilter,
    PruneBatch,
    RetentionAnchor,
)
from app.db.repositories.activity_export_cursor import (
    ActivityExportCursorRepository,
    ExportCursor,
)
from app.db.repositories.api_token import ApiToken, ApiTokenRepository
from app.db.repositories.backup_record import (
    KEPT_BACKUP_RECORDS,
    BackupOrigin,
    BackupOutcome,
    BackupRecord,
    BackupRecordRepository,
)
from app.db.repositories.backup_snapshot import InstanceCounts
from app.db.repositories.base import Repository, row_as
from app.db.repositories.clocked import Clock, ClockedRepository
from app.db.repositories.environment import (
    DashboardQuery,
    DashboardRow,
    Environment,
    EnvironmentDashboardRepository,
    EnvironmentDependents,
    EnvironmentRepository,
    StatusRecord,
)
from app.db.repositories.environment_access import (
    EnvironmentAccessRepository,
    GrantedTeam,
    GrantedUser,
    environment_visible_clause,
)
from app.db.repositories.environment_group import (
    EnvironmentGroup,
    EnvironmentGroupRepository,
    GroupSummary,
    GroupSummaryRepository,
)
from app.db.repositories.environment_snapshot import (
    EnvironmentSnapshot,
    EnvironmentSnapshotRepository,
    SnapshotCounts,
)
from app.db.repositories.environment_tls import EnvironmentTlsRepository, StoredTls
from app.db.repositories.errors import (
    DuplicateError,
    MissingReferenceError,
    RepositoryError,
    UserHasActivityError,
)
from app.db.repositories.image_binding import (
    AffectedBinding,
    BindingRef,
    EnvironmentBindingCount,
    ImageBindingRepository,
    StoredBinding,
)
from app.db.repositories.login_throttle import (
    LoginThrottle,
    LoginThrottleRepository,
    ThrottleKind,
)
from app.db.repositories.object_storage_target import (
    ObjectStorageTargetRepository,
    ObjectStorageTargetStore,
    RuleReference,
    StoredObjectStorageTarget,
    TargetFields,
)
from app.db.repositories.pagination import (
    MAX_PAGE_SIZE,
    CursorPosition,
    InvalidCursorError,
    Page,
    decode_cursor,
    encode_cursor,
)
from app.db.repositories.readiness import schema_is_readable
from app.db.repositories.registry_access import (
    GrantedEnvironment,
    OfferedCredentialSet,
    RegistryAccessRepository,
)
from app.db.repositories.registry_credential import (
    AddressMatch,
    CredentialFields,
    RegistryCredentialRepository,
    RegistryListEntry,
    RegistryListRepository,
    RevealedSecrets,
    SecretUnreadableError,
    StillReferencedError,
    StoredRegistryCredential,
)
from app.db.repositories.resource_control import (
    ControlGrants,
    ControlStanding,
    ResourceControl,
    ResourceControlRepository,
)
from app.db.repositories.setting import Setting, SettingRepository, SettingValueType
from app.db.repositories.snapshot import snapshot_database
from app.db.repositories.tag import Tag, TagRepository, TagSummary, TagSummaryRepository
from app.db.repositories.team import Team, TeamRepository
from app.db.repositories.team_membership import TeamMembership, TeamMembershipRepository
from app.db.repositories.team_roster import RosterEntry, TeamRosterRepository
from app.db.repositories.user import Role, User, UserRepository
from app.db.repositories.user_session import UserSession, UserSessionRepository
from app.db.repositories.visibility_filter import SqlFragment, visible_clause

__all__ = [
    "KEPT_BACKUP_RECORDS",
    "MAX_PAGE_SIZE",
    "ActivityEvent",
    "ActivityEventRepository",
    "ActivityExportCursorRepository",
    "ActivityFilter",
    "AddressMatch",
    "AffectedBinding",
    "ApiToken",
    "ApiTokenRepository",
    "BackupOrigin",
    "BackupOutcome",
    "BackupRecord",
    "BackupRecordRepository",
    "BindingRef",
    "Clock",
    "ClockedRepository",
    "ControlGrants",
    "ControlStanding",
    "CredentialFields",
    "CursorPosition",
    "DashboardQuery",
    "DashboardRow",
    "DuplicateError",
    "Environment",
    "EnvironmentAccessRepository",
    "EnvironmentBindingCount",
    "EnvironmentDashboardRepository",
    "EnvironmentDependents",
    "EnvironmentGroup",
    "EnvironmentGroupRepository",
    "EnvironmentRepository",
    "EnvironmentSnapshot",
    "EnvironmentSnapshotRepository",
    "EnvironmentTlsRepository",
    "ExportCursor",
    "GrantedEnvironment",
    "GrantedTeam",
    "GrantedUser",
    "GroupSummary",
    "GroupSummaryRepository",
    "ImageBindingRepository",
    "InstanceCounts",
    "InvalidCursorError",
    "LoginThrottle",
    "LoginThrottleRepository",
    "MissingReferenceError",
    "ObjectStorageTargetRepository",
    "ObjectStorageTargetStore",
    "OfferedCredentialSet",
    "Page",
    "PruneBatch",
    "RegistryAccessRepository",
    "RegistryCredentialRepository",
    "RegistryListEntry",
    "RegistryListRepository",
    "Repository",
    "RepositoryError",
    "ResourceControl",
    "ResourceControlRepository",
    "RetentionAnchor",
    "RevealedSecrets",
    "Role",
    "RosterEntry",
    "RuleReference",
    "SecretUnreadableError",
    "Setting",
    "SettingRepository",
    "SettingValueType",
    "SnapshotCounts",
    "SqlFragment",
    "StatusRecord",
    "StillReferencedError",
    "StoredBinding",
    "StoredObjectStorageTarget",
    "StoredRegistryCredential",
    "StoredTls",
    "Tag",
    "TagRepository",
    "TagSummary",
    "TagSummaryRepository",
    "TargetFields",
    "Team",
    "TeamMembership",
    "TeamMembershipRepository",
    "TeamRepository",
    "TeamRosterRepository",
    "ThrottleKind",
    "User",
    "UserHasActivityError",
    "UserRepository",
    "UserSession",
    "UserSessionRepository",
    "decode_cursor",
    "encode_cursor",
    "environment_visible_clause",
    "row_as",
    "schema_is_readable",
    "snapshot_database",
    "visible_clause",
]
