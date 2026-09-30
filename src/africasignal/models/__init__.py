"""All models are imported here so ``Base.metadata`` is complete for Alembic."""

from africasignal.models.assessments import (
    AssessmentInput,
    AssessmentVersion,
    Situation,
)
from africasignal.models.base import Base
from africasignal.models.claims import Claim
from africasignal.models.evidence import EvidenceDocument, GdeltDiscovery, ReportingOrigin
from africasignal.models.llm_cache import LlmResponseCache
from africasignal.models.measurements import Measurement, MeasurementReview, Series
from africasignal.models.ops import (
    AuditLog,
    Event,
    Feedback,
    Job,
    LlmCall,
    Operator,
    Outbox,
    Setting,
)
from africasignal.models.places import Place, PlaceAlias
from africasignal.models.sources import Source, SourcePermission
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
    "AppUser",
    "AssessmentInput",
    "AssessmentVersion",
    "AuditLog",
    "Base",
    "Claim",
    "Event",
    "EvidenceDocument",
    "Feedback",
    "Follow",
    "GdeltDiscovery",
    "Job",
    "LlmCall",
    "LlmResponseCache",
    "LoginToken",
    "Measurement",
    "MeasurementReview",
    "Notification",
    "Operator",
    "Outbox",
    "Place",
    "PlaceAlias",
    "Preference",
    "ReportingOrigin",
    "Series",
    "Setting",
    "Situation",
    "Source",
    "SourcePermission",
    "UserSession",
]
