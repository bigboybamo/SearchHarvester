"""ATS clients. Every ATS-specific URL pattern and API detail lives in this package."""

from harvester.ats.ashby import AshbyClient
from harvester.ats.base import AtsClient, AtsError
from harvester.ats.greenhouse import GreenhouseClient
from harvester.ats.lever import LeverClient
from harvester.ats.registry import AtsRegistry, default_registry
from harvester.ats.workable import WorkableClient

__all__ = [
    "AshbyClient",
    "AtsClient",
    "AtsError",
    "AtsRegistry",
    "GreenhouseClient",
    "LeverClient",
    "WorkableClient",
    "default_registry",
]
