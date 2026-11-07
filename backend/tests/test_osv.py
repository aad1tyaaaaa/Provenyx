import asyncio
import json

import httpx

from app.detection.osv import OsvClient, cvss3_base_score, normalized_severity
from tests.fakes import osv_transport

GHSA = {"id": "GHSA-test-1234", "aliases": ["CVE-2026-0001"], "summary": "Synthetic advisory", "database_specific": {"severity": "HIGH"}}


def test_osv_client_batches_queries_and_fetches_details_only_for_matches():
    calls: list[str] = []
    client = OsvClient(transport=osv_transport({("minimist", "1.2.5"): [GHSA]}, calls), retries=0)

    result = asyncio.run(client.fetch_many([("minimist", "1.2.5"), ("safe", "1.0.0")]))

    assert calls == ["/v1/querybatch", "/v1/vulns/GHSA-test-1234"]
    advisory = result[("minimist", "1.2.5")][0]
    assert (advisory.id, advisory.aliases, advisory.severity) == ("GHSA-test-1234", ("CVE-2026-0001",), "high")
    assert result[("safe", "1.0.0")] == ()


def test_batches_are_split_at_the_batch_size():
    sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries = json.loads(request.content)["queries"]
        sizes.append(len(queries))
        return httpx.Response(200, json={"results": [{} for _ in queries]})

    client = OsvClient(transport=httpx.MockTransport(handler), retries=0, batch_size=2)
    asyncio.run(client.fetch_many([("a", "1"), ("b", "1"), ("c", "1")]))

    assert sorted(sizes) == [1, 2]


def test_osv_client_uses_requested_ecosystem_and_keeps_caches_separate():
    ecosystems: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries = json.loads(request.content)["queries"]
        ecosystems.extend(query["package"]["ecosystem"] for query in queries)
        return httpx.Response(200, json={"results": [{} for _ in queries]})

    client = OsvClient(transport=httpx.MockTransport(handler), retries=0)
    asyncio.run(client.fetch_many([("vendor/package", "1.0.0")], ecosystem="Packagist"))
    asyncio.run(client.fetch_many([("vendor/package", "1.0.0")], ecosystem="npm"))

    assert ecosystems == ["Packagist", "npm"]


def test_osv_client_distinguishes_no_advisories_from_unavailable():
    empty = OsvClient(transport=osv_transport(), retries=0)
    unavailable = OsvClient(
        transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("offline", request=request))),
        retries=0,
    )

    assert asyncio.run(empty.fetch_many([("safe", "1.0.0")]))[("safe", "1.0.0")] == ()
    assert asyncio.run(unavailable.fetch_many([("unknown", "1.0.0")]))[("unknown", "1.0.0")] is None


def test_failures_are_not_cached_but_successes_are_until_they_expire():
    outage = True
    calls = 0
    now = 0.0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503) if outage else httpx.Response(200, json={"results": [{}]})

    async def scenario():
        nonlocal outage, now
        client = OsvClient(transport=httpx.MockTransport(handler), retries=0, cache_ttl_seconds=60, clock=lambda: now)
        assert (await client.fetch_many([("pkg", "1.0.0")]))[("pkg", "1.0.0")] is None
        outage = False
        assert (await client.fetch_many([("pkg", "1.0.0")]))[("pkg", "1.0.0")] == ()  # retried, not stuck on None
        await client.fetch_many([("pkg", "1.0.0"), ("pkg", "1.0.0")])  # served from cache
        now = 61.0
        await client.fetch_many([("pkg", "1.0.0")])  # expired, fetched again

    asyncio.run(scenario())
    assert calls == 3


def test_failed_detail_lookup_still_reports_the_advisory_with_unknown_severity():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/querybatch":
            return httpx.Response(200, json={"results": [{"vulns": [{"id": "GHSA-gone"}]}]})
        return httpx.Response(503)

    client = OsvClient(transport=httpx.MockTransport(handler), retries=0)
    advisories = asyncio.run(client.fetch_many([("pkg", "1.0.0")]))[("pkg", "1.0.0")]

    assert [(advisory.id, advisory.severity) for advisory in advisories] == [("GHSA-gone", None)]


def test_severity_is_normalized_across_advisory_formats():
    assert normalized_severity({"id": "GHSA-1", "database_specific": {"severity": "MODERATE"}}) == "medium"
    assert normalized_severity({"id": "X-1", "affected": [{"ecosystem_specific": {"severity": "LOW"}}]}) == "low"
    cvss = {"id": "X-2", "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}]}
    assert normalized_severity(cvss) == "critical"
    assert normalized_severity({"id": "MAL-2026-1"}) == "critical"
    assert normalized_severity({"id": "X-3"}) is None


def test_cvss3_base_scores_match_the_specification():
    assert cvss3_base_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H") == 9.8
    assert cvss3_base_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N") == 6.1
    assert cvss3_base_score("CVSS:3.0/AV:L/AC:L/PR:L/UI:N/S:U/C:H/I:N/A:N") == 5.5
    assert cvss3_base_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:N") == 0.0
    assert cvss3_base_score("CVSS:4.0/AV:N") is None
    assert cvss3_base_score("CVSS:3.1/AV:X") is None
