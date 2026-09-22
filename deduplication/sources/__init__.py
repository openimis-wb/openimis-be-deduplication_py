"""
Seam owned by the deduplication module: the shapes and registry other modules
(e.g. biometric_verification) use to feed duplicate-subject candidates into
deduplication, without deduplication importing them back.
"""
import abc
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, List, Optional


@dataclass(frozen=True)
class Watermark:
    """Cursor a source resumes scanning from; both fields None means 'from the start'."""
    updated_at: Optional[datetime] = None
    last_id: Optional[str] = None


@dataclass(frozen=True)
class Candidate:
    """One suspected duplicate pair, as reported by a source."""
    subject_model: str
    subject_a: str          # ordered: subject_a < subject_b as strings
    subject_b: str
    kind: str                # "demographic" | "identifier" | "biometric" | any registered kind
    score: Optional[float]   # higher = more likely the same person; None for exact matches
    evidence: dict           # JSON-serialisable; what a reviewer needs to see


class CandidateSource(abc.ABC):
    """A pluggable producer of duplicate-subject candidates."""
    kind: str

    @abc.abstractmethod
    def scan(self, since: Optional[Watermark]) -> Iterable[Candidate]:
        """Yield candidates found since the given watermark (None: scan everything)."""
        raise NotImplementedError

    @abc.abstractmethod
    def watermark(self) -> Watermark:
        """Cursor where the next scan should start from."""
        raise NotImplementedError


_REGISTRY: List[CandidateSource] = []


def register(source: CandidateSource) -> None:
    """Add a source to the registry, in call order."""
    _REGISTRY.append(source)


def sources() -> List[CandidateSource]:
    """Registered sources, in registration order."""
    return list(_REGISTRY)


def order_pair(a: str, b: str):
    """Return (a, b) sorted so the first element is strictly the smaller string."""
    return (a, b) if a < b else (b, a)
