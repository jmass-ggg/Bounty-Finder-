"""Source-independent discovery orchestration for canonical GitHub issues.

External adapters must use a documented, permitted interface. A platform's
reward assertion is only a candidate signal; funding still needs verification.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlparse

from bountyradar.models import BountyCandidate, ScanResult, UnprocessedWindow


class BountySource(Protocol):
    """Discover normalized candidates from one independently checkpointed source."""

    @property
    def name(self) -> str: ...

    async def scan(self, hours: float | None = None) -> ScanResult: ...


@dataclass
class MultiSourceScan:
    candidates: list[BountyCandidate] = field(default_factory=list)
    results: dict[str, ScanResult] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)

    @property
    def unprocessed_windows(self) -> list[UnprocessedWindow]:
        return [window for result in self.results.values() for window in result.unprocessed_windows]

    @property
    def invalid_items(self) -> int:
        return sum(result.invalid_items for result in self.results.values())

    @property
    def complete(self) -> bool:
        return not self.failures and all(result.complete for result in self.results.values())


def canonical_issue_key(candidate: BountyCandidate) -> tuple[str, int]:
    """Deduplicate by the linked GitHub issue, irrespective of source IDs."""
    parsed = urlparse(candidate.url)
    parts = parsed.path.strip("/").split("/")
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or len(parts) != 4
        or parts[2] != "issues"
        or not parts[3].isdigit()
        or int(parts[3]) != candidate.issue_number
        or f"{parts[0]}/{parts[1]}".casefold() != candidate.repository.casefold()
    ):
        raise ValueError("candidate lacks a canonical GitHub issue link")
    return candidate.repository.casefold(), candidate.issue_number


async def scan_sources(
    sources: Sequence[BountySource], hours_by_source: Mapping[str, float | None]
) -> MultiSourceScan:
    """Continue after one adapter fails; never advance its checkpoint."""
    scan = MultiSourceScan()
    unique: dict[tuple[str, int], BountyCandidate] = {}
    names: set[str] = set()
    for source in sources:
        name = getattr(source, "name", "github")
        if name in names or not name:
            raise ValueError("source names must be unique and nonempty")
        names.add(name)
        try:
            result = await source.scan(hours_by_source.get(name))
            staged = unique.copy()
            for candidate in result.candidates:
                key = canonical_issue_key(candidate)
                existing = staged.get(key)
                if existing is None:
                    staged[key] = candidate
                else:
                    # GitHub's own issue ID and metadata take precedence when
                    # another source reports the same linked issue.
                    preferred = candidate if name == "github" else existing
                    # Keep all original evidence links; do not add reward values.
                    staged[key] = preferred.model_copy(
                        update={
                            "candidate_evidence": list(
                                dict.fromkeys(
                                    existing.candidate_evidence + candidate.candidate_evidence
                                )
                            )
                        }
                    )
            unique = staged
            scan.results[name] = result
        except Exception as exc:
            scan.failures[name] = type(exc).__name__
    scan.candidates = sorted(unique.values(), key=lambda item: item.updated_at, reverse=True)
    return scan
