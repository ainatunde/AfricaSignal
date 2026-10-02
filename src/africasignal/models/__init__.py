"""All models are imported here so ``Base.metadata`` is complete for Alembic."""

from africasignal.models.assessments import (
    AssessmentInput,
    AssessmentVersion,
    OperatorPolicySeries,
    Situation,
)
from africasignal.models.base import Base
from africasignal.models.claims import Claim
from africasignal.models.evidence import EvidenceDocument, GdeltDiscovery, ReportingOrigin
from africasignal.models.llm_cache import LlmResponseCache
from africasignal.models.measurements import Measurement, MeasurementReview, Series
from africasignal.models.ops import (
    AgentReachCandidate,
    AgentReachTask,
    AuditLog,
    ChannelPost,
    Event,
    ExternalAgentProfile,
    ExternalAgentTask,
    Feedback,
    Job,
    LlmBudgetReservation,
    LlmCall,
    Operator,
    OperatorSignInFailure,
    Outbox,
    Setting,
    WorkloadControl,
)
from africasignal.models.places import Place, PlaceAlias
from africasignal.models.sources import DiscoveredDomainDecision, Source, SourcePermission
from africasignal.models.users import (
    AccountDeletion,
    AppUser,
    Follow,
    LoginToken,
    Notification,
    Preference,
    UserSession,
)

__all__ = [
    "AccountDeletion",
    "AgentReachCandidate",
    "AgentReachTask",
    "AppUser",
    "AssessmentInput",
    "AssessmentVersion",
    "AuditLog",
    "Base",
    "ChannelPost",
    "Claim",
    "DiscoveredDomainDecision",
    "Event",
    "EvidenceDocument",
    "ExternalAgentProfile",
    "ExternalAgentTask",
    "Feedback",
    "Follow",
    "GdeltDiscovery",
    "Job",
    "LlmBudgetReservation",
    "LlmCall",
    "LlmResponseCache",
    "LoginToken",
    "Measurement",
    "MeasurementReview",
    "Notification",
    "Operator",
    "OperatorPolicySeries",
    "OperatorSignInFailure",
    "Outbox",
    "Place",
    "PlaceAlias",
    "Preference",
    "ReportingOrigin",
    "Series",
    "Setting",
    "WorkloadControl",
    "Situation",
    "Source",
    "SourcePermission",
    "UserSession",
]
