"""Public V1 response contracts. Authorization material only appears in manual script delivery."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AuthorizationError(BaseModel):
    detail: str
    code: Literal['invalid_session_cookie', 'cookie_account_mismatch'] | None = None


class WorkspaceCapabilities(BaseModel):
    manage_accounts: bool
    manage_members: bool
    manage_admins: bool
    transfer_owner: bool
    delete: bool
    audit: bool
    export_credentials: bool


class WorkspaceCreated(BaseModel):
    id: str
    name: str
    kind: Literal['personal', 'team']
    role: Literal['owner', 'admin', 'member', 'viewer']


class WorkspaceView(WorkspaceCreated):
    capabilities: WorkspaceCapabilities


class Me(BaseModel):
    id: str
    login: str
    instance_admin: bool
    workspaces: list[WorkspaceView]


class Bootstrap(BaseModel):
    mode: Literal['server', 'local']
    initialized: bool
    api_version: int
    capabilities: dict[str, bool]
    app_version: str = "0.0.1"


class ReleaseInfo(BaseModel):
    current_version: str
    latest_version: str | None
    available: bool
    installable: bool
    notes: str
    release_url: str
    published_at: str | None


class UpdateStatus(BaseModel):
    enabled: bool
    stage: Literal['idle', 'queued', 'downloading', 'verifying', 'backing_up', 'upgrading',
                   'restarting', 'rolling_back', 'complete', 'failed']
    job_id: str | None
    version: str | None
    message: str | None


class LoginResult(BaseModel):
    csrf_token: str
    expires_at: float


class Csrf(BaseModel):
    csrf_token: str


class SessionView(BaseModel):
    id: str
    created_at: float
    expires_at: float
    current: bool
    kind: Literal['web', 'device'] = 'web'
    device_id: str | None = None
    device_name: str | None = None


class DeviceApproved(BaseModel):
    callback_url: str


class DeviceLogin(BaseModel):
    token: str
    session_id: str
    expires_at: float


class DeviceDelivery(BaseModel):
    ticket_id: str
    workspace_id: str
    account_id: str
    expires_at: float
    access_token: str
    refresh_token: str
    email: str
    subject: str


class MemberView(BaseModel):
    id: str
    login: str
    role: Literal['owner', 'admin', 'member', 'viewer']
    active: bool


class UserView(BaseModel):
    id: str
    login: str
    active: bool
    instance_admin: bool


class InvitationView(BaseModel):
    id: str
    login: str
    role: Literal['admin', 'member', 'viewer']
    expires_at: float


class InvitationIssued(BaseModel):
    id: str
    token: str
    expires_at: float


class Joined(BaseModel):
    user_id: str
    workspace_id: str


class GrantView(BaseModel):
    user_id: str
    level: Literal['view', 'use']


class AuditView(BaseModel):
    id: str
    actor_id: str | None
    workspace_id: str | None
    resource_id: str | None
    action: str
    result: str
    created_at: float
    request_id: str | None
    changes: dict | None


class AccountCapabilities(BaseModel):
    view: bool
    detail: bool
    refresh: bool
    switch: bool
    edit: bool
    delete: bool
    authorize: bool
    grant: bool


class UsageFields(BaseModel):
    # Preserve existing provider presentation fields, including imported snapshots.
    model_config = ConfigDict(extra='allow')


class QuotaSlot(UsageFields):
    used_pct: float | None = None
    remaining_pct: float | None = None
    limit_usd: float | None = None
    used_usd: float | None = None
    remaining_usd: float | None = None
    limit_inferred: bool = False
    limit_source: Literal['history', 'plan', 'reference'] | None = None


class Plan(UsageFields):
    name: str | None = None
    included_usd: float | None = None


class Cycle(UsageFields):
    start: str | None = None
    reset_at: str | None = None
    days_left: int | None = None


class Grok(UsageFields):
    remaining_pct: float | None = None
    reset_at: str | None = None


class Spend(UsageFields):
    total: float | None = None
    from_included: float | None = None
    from_bonus: float | None = None


class OnDemand(UsageFields):
    enabled: bool = False
    used_usd: float | None = None
    limit_usd: float | None = None


class Usage(UsageFields):
    plan: Plan = Field(default_factory=Plan)
    cycle: Cycle = Field(default_factory=Cycle)
    quota: dict[str, QuotaSlot] = Field(default_factory=dict)
    grok_weekly: Grok | None = None
    notice: str | None = None
    spend_usd: Spend | None = None
    on_demand: OnDemand | None = None


class AccountView(BaseModel):
    id: str
    workspace_id: str
    label: str
    email: str | None
    tags: list[str]
    capabilities: AccountCapabilities
    authorization_generation: str
    credential_version: int
    auth_invalid: bool
    expires_at: float
    refreshed_at: float
    data: Usage | None
    ok_at: float
    attempted_at: float
    error_kind: str | None
    error_message: str | None
    failures: int
    expired: bool
    stale: bool
    pending: bool


class AccountStats(BaseModel):
    accounts: int
    invalid: int
    with_snapshot: int


class AccountPage(BaseModel):
    items: list[AccountView]
    total: int
    tags: dict[str, int]
    stats: AccountStats


class TokenCounts(UsageFields):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0


class ModelUsage(TokenCounts):
    model: str
    group: str
    total_tokens: int
    spend_usd: float
    spend_cents: float


class DetailGroup(BaseModel):
    key: str
    name: str
    spend_usd: float
    total_tokens: int
    models: list[ModelUsage]


class DetailTotals(TokenCounts):
    spend_usd: float
    total_tokens: int
    model_count: int


class DetailView(BaseModel):
    id: str
    workspace_id: str
    cycle_start: str
    fetched_at: str
    groups: list[DetailGroup]
    totals: DetailTotals


class SwitchIssued(BaseModel):
    token: str
    expires_at: float


class SwitchCommand(BaseModel):
    platform: Literal['macos', 'windows']
    command: str
    expires_at: float


class ManualScript(SwitchCommand):
    script: str


class Health(BaseModel):
    status: Literal['ok']
    api_version: int
