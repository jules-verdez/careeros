"""External integration boundaries."""

from careeros.connectors.base import (
    CalendarConnector,
    ContactEnrichmentConnector,
    JobDiscoveryConnector,
    MailConnector,
    PeopleDiscoveryConnector,
    ResearchConnector,
    WebSearchConnector,
)
from careeros.connectors.local_json import LocalJsonJobConnector

__all__ = [
    "CalendarConnector",
    "ContactEnrichmentConnector",
    "JobDiscoveryConnector",
    "LocalJsonJobConnector",
    "MailConnector",
    "PeopleDiscoveryConnector",
    "ResearchConnector",
    "WebSearchConnector",
]
