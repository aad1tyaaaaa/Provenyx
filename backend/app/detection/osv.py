from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import quote

import httpx

BATCH_LIMIT = 1000  # OSV's maximum number of queries per /v1/querybatch request


@dataclass(frozen=True)
class OsvAdvisory:
    id: str
    aliases: tuple[str, ...]
    summary: str | None
    severity: str | None


class OsvClient:
    """Exact-version OSV lookups for supported package ecosystems.

    `fetch_many` returns, per release: a tuple of advisories (empty = none known), or None when the lookup
    was unavailable. Those are different security states and are never conflated. Successful results are
    cached for `cache_ttl_seconds`; failures are not cached, so a transient outage is retried next time.
    """

    def __init__(
        self,
        *,
        base_url: str = "https://api.osv.dev",
        concurrency: int = 10,
        timeout_seconds: float = 5.0,
        retries: int = 2,
        cache_ttl_seconds: float = 3600.0,
        batch_size: int = BATCH_LIMIT,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._concurrency = max(1, concurrency)
        self._timeout = timeout_seconds
        self._retries = max(0, retries)
        self._ttl = cache_ttl_seconds
        self._batch_size = max(1, min(batch_size, BATCH_LIMIT))
        self._transport = transport
        self._clock = clock
        self._cache: dict[tuple[str, str, str], tuple[float, tuple[OsvAdvisory, ...]]] = {}
        self._details: dict[str, tuple[float, OsvAdvisory]] = {}

    async def fetch_many(
        self, releases: Iterable[tuple[str, str]], *, ecosystem: str = "npm"
    ) -> dict[tuple[str, str], tuple[OsvAdvisory, ...] | None]:
        unique_releases = tuple(dict.fromkeys(releases))
        now = self._clock()
        results: dict[tuple[str, str], tuple[OsvAdvisory, ...] | None] = {}
        missing = []
        for release in unique_releases:
            cached = self._cache.get((ecosystem, *release))
            if cached is not None and cached[0] > now:
                results[release] = cached[1]
            else:
                missing.append(release)
        if missing:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                matched_ids = await self._query_batches(client, missing, ecosystem)
                needed = {vuln_id for ids in matched_ids.values() if ids for vuln_id in ids}
                details = await self._fetch_details(client, needed)
            expires = self._clock() + self._ttl
            for release in missing:
                ids = matched_ids.get(release)
                if ids is None:
                    results[release] = None
                    continue
                # A failed detail lookup still reports the advisory, but with unknown severity, and is not cached.
                advisories = tuple(
                    sorted(
                        (details.get(vuln_id) or OsvAdvisory(id=vuln_id, aliases=(), summary=None, severity=None) for vuln_id in ids),
                        key=lambda advisory: advisory.id,
                    )
                )
                results[release] = advisories
                if all(vuln_id in details for vuln_id in ids):
                    self._cache[(ecosystem, *release)] = (expires, advisories)
        return {release: results[release] for release in unique_releases}

    async def _query_batches(
        self, client: httpx.AsyncClient, releases: list[tuple[str, str]], ecosystem: str
    ) -> dict[tuple[str, str], tuple[str, ...] | None]:
        chunks = [releases[start : start + self._batch_size] for start in range(0, len(releases), self._batch_size)]
        matched: dict[tuple[str, str], tuple[str, ...] | None] = {}
        for chunk, ids in zip(
            chunks,
            await asyncio.gather(*(self._query_batch(client, chunk, ecosystem) for chunk in chunks)),
            strict=True,
        ):
            matched.update(zip(chunk, ids, strict=True))
        return matched

    async def _query_batch(
        self, client: httpx.AsyncClient, chunk: list[tuple[str, str]], ecosystem: str
    ) -> list[tuple[str, ...] | None]:
        body = {"queries": [{"package": {"ecosystem": ecosystem, "name": name}, "version": version} for name, version in chunk]}
        payload = await self._request(client, "POST", "/v1/querybatch", json=body)
        entries = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(entries, list) or len(entries) != len(chunk):
            return [None] * len(chunk)  # results are positional; a malformed response tells us nothing
        matched: list[tuple[str, ...] | None] = []
        for entry in entries:
            vulns = entry.get("vulns", []) if isinstance(entry, dict) else None
            if not isinstance(vulns, list):
                matched.append(None)
                continue
            # ponytail: next_page_token (over 1,000 advisories for one release) is not followed; no npm release is near that.
            matched.append(tuple(sorted({v["id"] for v in vulns if isinstance(v, dict) and isinstance(v.get("id"), str)})))
        return matched

    async def _fetch_details(self, client: httpx.AsyncClient, ids: set[str]) -> dict[str, OsvAdvisory]:
        now = self._clock()
        details = {vuln_id: cached[1] for vuln_id in ids if (cached := self._details.get(vuln_id)) and cached[0] > now}
        semaphore = asyncio.Semaphore(self._concurrency)

        async def fetch(vuln_id: str) -> None:
            async with semaphore:
                record = await self._request(client, "GET", f"/v1/vulns/{quote(vuln_id, safe='')}")
            if isinstance(record, dict) and record.get("id") == vuln_id:
                advisory = advisory_from_record(record)
                details[vuln_id] = advisory
                self._details[vuln_id] = (self._clock() + self._ttl, advisory)

        await asyncio.gather(*(fetch(vuln_id) for vuln_id in ids - details.keys()))
        return details

    async def _request(self, client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> Any:
        """JSON response, or None after retries; 5xx and network errors are retried, 4xx are not."""
        for attempt in range(self._retries + 1):
            try:
                response = await client.request(method, url, **kwargs)
                response.raise_for_status()
                return response.json()
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
                pass
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500:
                    return None
            except (ValueError, TypeError):
                return None
            if attempt < self._retries:
                await asyncio.sleep(0.1 * (attempt + 1))
        return None


def advisory_from_record(record: dict[str, Any]) -> OsvAdvisory:
    aliases = record.get("aliases")
    summary = record.get("summary")
    return OsvAdvisory(
        id=record["id"],
        aliases=tuple(alias for alias in aliases if isinstance(alias, str)) if isinstance(aliases, list) else (),
        summary=summary if isinstance(summary, str) else None,
        severity=normalized_severity(record),
    )


def normalized_severity(record: dict[str, Any]) -> str | None:
    """critical / high / medium / low, from whichever field the advisory source populates; None if unknown."""
    if str(record.get("id", "")).startswith("MAL-"):
        return "critical"  # OpenSSF malicious-package reports carry no severity, but are the worst case
    labels = [_label(record.get("database_specific"))]
    for affected in record.get("affected") or []:
        if isinstance(affected, dict):
            labels += [_label(affected.get("ecosystem_specific")), _label(affected.get("database_specific"))]
    for label in labels:
        if label:
            return label
    scores = [
        cvss3_base_score(entry["score"])
        for entry in record.get("severity") or []
        if isinstance(entry, dict) and entry.get("type") == "CVSS_V3" and isinstance(entry.get("score"), str)
    ]
    scores = [score for score in scores if score is not None]
    if scores:
        best = max(scores)
        return "critical" if best >= 9 else "high" if best >= 7 else "medium" if best >= 4 else "low" if best > 0 else None
    return None


def _label(section: Any) -> str | None:
    if not isinstance(section, dict) or not isinstance(section.get("severity"), str):
        return None
    value = section["severity"].strip().lower()
    return {"moderate": "medium"}.get(value, value) if value in {"critical", "high", "moderate", "medium", "low"} else None


_CVSS3_WEIGHTS = {
    "AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2},
    "AC": {"L": 0.77, "H": 0.44},
    "UI": {"N": 0.85, "R": 0.62},
    "C": {"H": 0.56, "L": 0.22, "N": 0.0},
    "I": {"H": 0.56, "L": 0.22, "N": 0.0},
    "A": {"H": 0.56, "L": 0.22, "N": 0.0},
}


def cvss3_base_score(vector: str) -> float | None:
    """CVSS v3.0/v3.1 base score from a vector string (specification section 7.1); None if malformed."""
    if not vector.startswith(("CVSS:3.0/", "CVSS:3.1/")):
        return None
    metrics = dict(part.split(":", 1) for part in vector.split("/")[1:] if ":" in part)
    try:
        changed = {"U": False, "C": True}[metrics["S"]]
        privileges = {"N": 0.85, "L": 0.68 if changed else 0.62, "H": 0.5 if changed else 0.27}[metrics["PR"]]
        av, ac, ui = (_CVSS3_WEIGHTS[m][metrics[m]] for m in ("AV", "AC", "UI"))
        c, i, a = (_CVSS3_WEIGHTS[m][metrics[m]] for m in ("C", "I", "A"))
    except KeyError:
        return None
    iss = 1 - (1 - c) * (1 - i) * (1 - a)
    impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15 if changed else 6.42 * iss
    if impact <= 0:
        return 0.0
    exploitability = 8.22 * av * ac * privileges * ui
    return _roundup(min((1.08 if changed else 1) * (impact + exploitability), 10))


def _roundup(value: float) -> float:
    as_int = round(value * 100_000)
    return as_int / 100_000.0 if as_int % 10_000 == 0 else (math.floor(as_int / 10_000) + 1) / 10.0
