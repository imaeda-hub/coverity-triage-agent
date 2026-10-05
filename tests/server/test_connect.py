"""Coverity Connect client against a fake server.

The request and response shapes follow the REST v2 search used by the public ``mlx.coverity``
package and the SOAP v9 ``defectservice`` / ``configurationservice`` operations.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from pydantic import ValidationError

from coverity_triage import connect
from coverity_triage.connect import ConnectClient, check_auth
from coverity_triage.coverity import CoverityError
from coverity_triage.models import TriageAttributes
from coverity_triage.settings import CoverityConfig, FilterSpec

COLUMNS = [{"name": n, "columnKey": k} for n, k in [
    ("CID", "cid"), ("Checker", "checker"), ("File", "displayFile"), ("Line Number", "lineNumber"),
    ("Function", "displayFunction"), ("Impact", "displayImpact"), ("Classification", "classification"),
    ("Action", "action"), ("Status", "status"), ("Severity", "severity")]]

ISSUES = {
    "main": [
        {"cid": 101, "checker": "NULL_RETURNS", "displayFile": "/build/src/a.c", "lineNumber": "12",
         "displayFunction": "f", "displayImpact": "High", "classification": "Unclassified",
         "action": "Undecided", "status": "New", "severity": "Unspecified"},
        {"cid": 102, "checker": "RESOURCE_LEAK", "displayFile": "/build/src/b.c", "lineNumber": "30",
         "displayFunction": "g", "displayImpact": "High", "classification": "Unclassified",
         "action": "Undecided", "status": "Triaged", "severity": "Unspecified"},
        {"cid": 103, "checker": "MISRA C-2012 Rule 10.3", "displayFile": "/build/src/c.c", "lineNumber": "5",
         "displayFunction": "h", "displayImpact": "Low", "classification": "Unclassified",
         "action": "Undecided", "status": "New", "severity": "Unspecified"},
    ],
    "rel": [
        {"cid": 101, "checker": "NULL_RETURNS", "displayFile": "/build/src/a.c", "lineNumber": "12",
         "displayFunction": "f", "displayImpact": "High", "classification": "Unclassified",
         "action": "Undecided", "status": "New", "severity": "Unspecified"},
    ],
}

STREAM_DEFECTS = """<?xml version="1.0"?>
<S:Envelope xmlns:S="http://schemas.xmlsoap.org/soap/envelope/"><S:Body>
<ns2:getStreamDefectsResponse xmlns:ns2="http://ws.coverity.com/v9"><return>
  <checkerName>NULL_RETURNS</checkerName><cid>101</cid>
  <defectInstances>
    <events><eventDescription>get_buf() may return NULL</eventDescription><eventNumber>1</eventNumber>
      <eventSet>0</eventSet><eventTag>returned_null</eventTag>
      <fileId><contentsMD5>aa</contentsMD5><filePathname>/build/src/a.c</filePathname></fileId>
      <lineNumber>10</lineNumber><main>false</main><polarity>false</polarity>
      <events><eventDescription>inside get_buf</eventDescription><eventNumber>1</eventNumber>
        <eventSet>0</eventSet><eventTag>null</eventTag>
        <fileId><contentsMD5>bb</contentsMD5><filePathname>/build/src/buf.c</filePathname></fileId>
        <lineNumber>3</lineNumber><main>false</main><polarity>false</polarity></events>
    </events>
    <events><eventDescription>buf is dereferenced</eventDescription><eventNumber>2</eventNumber>
      <eventSet>0</eventSet><eventTag>dereference</eventTag>
      <fileId><contentsMD5>aa</contentsMD5><filePathname>/build/src/a.c</filePathname></fileId>
      <lineNumber>12</lineNumber><main>true</main><polarity>false</polarity></events>
    <checkerName>NULL_RETURNS</checkerName><cwe>476</cwe>
    <function><functionDisplayName>f</functionDisplayName></function>
    <localEffect>May crash</localEffect>
    <longDescription>Dereference of a NULL return value</longDescription>
  </defectInstances>
</return></ns2:getStreamDefectsResponse></S:Body></S:Envelope>"""

SNAPSHOTS = """<S:Envelope xmlns:S="http://schemas.xmlsoap.org/soap/envelope/"><S:Body>
<ns2:getSnapshotsForStreamResponse xmlns:ns2="http://ws.coverity.com/v9">
<return><id>10001</id></return><return><id>10007</id></return><return><id>10003</id></return>
</ns2:getSnapshotsForStreamResponse></S:Body></S:Envelope>"""

SNAPSHOT_INFO = """<S:Envelope xmlns:S="http://schemas.xmlsoap.org/soap/envelope/"><S:Body>
<ns2:getSnapshotInformationResponse xmlns:ns2="http://ws.coverity.com/v9"><return>
<description>nightly</description><hasSummaries>true</hasSummaries><purgedOfDetails>false</purgedOfDetails>
<snapshotId><id>10007</id></snapshotId><sourceVersion>a1b2c3d</sourceVersion>
</return></ns2:getSnapshotInformationResponse></S:Body></S:Envelope>"""

FAULT = """<S:Envelope xmlns:S="http://schemas.xmlsoap.org/soap/envelope/"><S:Body><S:Fault>
<faultcode>S:Server</faultcode><faultstring>No stream found with name xyz</faultstring>
</S:Fault></S:Body></S:Envelope>"""


class FakeServer:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.triage_writes: list[tuple[dict, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        auth = request.headers.get("authorization", "")
        path = request.url.path
        if path.startswith("/api/") and auth != "Basic " + base64.b64encode(b"alice:secret").decode():
            return httpx.Response(401, json={"message": "unauthorized"})
        if path == "/api/v2/issues/columns":
            return httpx.Response(200, json=COLUMNS)
        if path == "/api/v2/issues/search":
            body = json.loads(request.content)
            stream = body["filters"][0]["matchers"][0]["name"]
            rows = ISSUES.get(stream, [])
            for f in body["filters"][1:]:
                keys = {m["key"] for m in f["matchers"]}
                rows = [r for r in rows if r.get(f["columnKey"]) in keys]
            offset, count = int(request.url.params["offset"]), int(request.url.params["rowCount"])
            page = rows[offset:offset + count]
            return httpx.Response(200, json={
                "offset": offset, "totalRows": len(rows), "columns": body["columns"],
                "rows": [[{"key": k, "value": r.get(k)} for k in body["columns"]] for r in page]})
        if path == "/api/v2/issues/triage" and request.method == "PUT":
            self.triage_writes.append((dict(request.url.params), json.loads(request.content)))
            return httpx.Response(200, json={})
        if path.startswith("/ws/v9/"):
            text = request.content.decode()
            assert "<wsse:Username>alice</wsse:Username>" in text
            if "getStreamDefects" in text:
                body = STREAM_DEFECTS if "<name>main</name>" in text else FAULT
                return httpx.Response(200 if body is STREAM_DEFECTS else 500, text=body)
            if "getSnapshotsForStream" in text:
                return httpx.Response(200, text=SNAPSHOTS)
            if "getSnapshotInformation" in text:
                assert "<id>10007</id>" in text
                return httpx.Response(200, text=SNAPSHOT_INFO)
        return httpx.Response(404, json={"message": f"no route {path}"})


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("COV_USER", "alice")
    monkeypatch.setenv("COV_AUTH_KEY", "secret")
    connect._columns_cache.clear()
    return FakeServer()


def make(server, **kw) -> ConnectClient:
    return ConnectClient(CoverityConfig(url="https://cov.example:8443/", **kw),
                         transport=httpx.MockTransport(server))


def search_all(client: ConnectClient, spec: FilterSpec, limit: int = 100) -> list:
    return client.search(spec, limit, 0, 60).issues


def test_search_maps_columns_and_filters(server, monkeypatch):
    monkeypatch.setattr(connect, "MIN_PAGE", 1)
    monkeypatch.setattr(connect, "MAX_PAGE", 1)  # one row per page
    spec = FilterSpec(streams=["main"], impacts=["High"], triage={"status": ["New"]})
    page = make(server).search(spec, 10, 0, 60)
    assert [i.cid for i in page.issues] == [101]  # 102 is Triaged (local filter), 103 is Low (server filter)
    assert page.next_offset is None and page.total == 2
    issue = page.issues[0]
    assert (issue.checker, issue.file, issue.line, issue.function, issue.stream) == \
        ("NULL_RETURNS", "/build/src/a.c", 12, "f", "main")
    search = [r for r in server.requests if r.url.path == "/api/v2/issues/search"][0]
    body = json.loads(search.content)
    assert body["snapshotScope"]["show"]["scope"] == "last()"
    assert {"columnKey": "displayImpact", "matchMode": "oneOrMoreMatch",
            "matchers": [{"type": "keyMatcher", "key": "High"}]} in body["filters"]


def test_search_checker_pattern_is_filtered_locally(server):
    issues = search_all(make(server), FilterSpec(streams=["main"], checkers=["MISRA*", "NULL_*"]))
    assert sorted(i.cid for i in issues) == [101, 103]
    for r in server.requests:
        if r.url.path == "/api/v2/issues/search":
            assert all(f["columnKey"] != "checker" for f in json.loads(r.content)["filters"])


def test_search_stops_at_limit_and_returns_where_to_continue(server, monkeypatch):
    monkeypatch.setattr(connect, "MIN_PAGE", 1)
    spec = FilterSpec(streams=["main"])
    client = make(server)
    first = client.search(spec, 1, 0, 60)
    assert [i.cid for i in first.issues] == [101] and first.next_offset == 1 and first.total == 3
    rest = client.search(spec, 10, first.next_offset, 60)
    assert [i.cid for i in rest.issues] == [102, 103] and rest.next_offset is None
    searches = [r for r in client.requests if r["request"] == "POST /api/v2/issues/search"]
    assert [r["offset"] for r in searches] == [0, 1] and all(r["status"] == 200 for r in searches)
    assert all("seconds" in r for r in client.requests)


def test_search_stops_before_running_out_of_time(server, monkeypatch):
    monkeypatch.setattr(connect, "MIN_PAGE", 1)
    monkeypatch.setattr(connect, "MAX_PAGE", 1)
    page = make(server).search(FilterSpec(streams=["main"]), 10, 0, 0)
    assert len(page.issues) == 1 and page.next_offset == 1  # one page, then no time left


def test_columns_are_asked_once_per_server(server):
    search_all(make(server), FilterSpec(streams=["main"]))
    search_all(make(server), FilterSpec(streams=["main"]))
    assert sum(r.url.path == "/api/v2/issues/columns" for r in server.requests) == 1


def test_auth_error_is_explained(server, monkeypatch):
    monkeypatch.setenv("COV_AUTH_KEY", "wrong")
    with pytest.raises(CoverityError, match="認証") as info:
        search_all(make(server), FilterSpec(streams=["main"]))
    assert info.value.fatal


def test_missing_credentials(monkeypatch):
    monkeypatch.setattr(connect, "get_env", lambda name: None)
    with pytest.raises(CoverityError, match="COV_USER"):
        ConnectClient(CoverityConfig(url="https://x"), transport=httpx.MockTransport(lambda r: None))


def test_check_auth_reports_without_the_key(server):
    out = check_auth(CoverityConfig(url="https://cov.example:8443"), transport=httpx.MockTransport(server))
    assert out["ok"] and out["seconds"] is not None
    assert "secret" not in json.dumps(out, ensure_ascii=False)


def test_check_auth_wrong_key(server, monkeypatch):
    monkeypatch.setenv("COV_AUTH_KEY", "wrong")
    out = check_auth(CoverityConfig(url="https://cov.example:8443"), transport=httpx.MockTransport(server))
    assert not out["ok"] and "認証に失敗" in out["result"]


def test_issue_detail_flattens_events(server):
    detail = make(server).issue_detail(101, "main")
    assert [(e.file, e.line, e.tag, e.main) for e in detail.events] == [
        ("/build/src/a.c", 10, "returned_null", False),
        ("/build/src/buf.c", 3, "null", False),
        ("/build/src/a.c", 12, "dereference", True)]
    assert (detail.issue.cid, detail.issue.checker, detail.issue.file, detail.issue.line) == \
        (101, "NULL_RETURNS", "/build/src/a.c", 12)
    assert detail.issue.cwe == 476 and detail.issue.function == "f"
    assert "Dereference of a NULL return value" in detail.checker_description
    assert "May crash" in detail.checker_description


def test_soap_fault_fails_only_that_request(server):
    with pytest.raises(CoverityError, match="No stream found") as info:
        make(server).issue_detail(101, "xyz")
    assert not info.value.fatal


def test_snapshot_revision_uses_latest_snapshot(server):
    client = make(server)
    assert client.snapshot_revision("main", "sourceVersion") == "a1b2c3d"
    assert client.snapshot_revision("main", "description") == "nightly"
    assert client.snapshot_revision("main", "target") is None


def test_write_triage_uses_rest_put(server):
    client = make(server, triage_store="Product Store")
    client.write_triage([101, 103], TriageAttributes(classification="False Positive", action="Ignore",
                                                     severity="Unspecified"), "誤検知。理由。")
    params, body = server.triage_writes[0]
    assert params == {"triageStoreName": "Product Store"}
    assert body == {"cids": [101, 103], "attributeValuesList": [
        {"attributeName": "Classification", "attributeValue": "False Positive"},
        {"attributeName": "Action", "attributeValue": "Ignore"},
        {"attributeName": "Severity", "attributeValue": "Unspecified"},
        {"attributeName": "Comment", "attributeValue": "誤検知。理由。"}]}


def test_write_triage_leaves_out_empty_values(server):
    make(server).write_triage([101], TriageAttributes(classification="Intentional", action="Ignore"), "理由")
    names = [a["attributeName"] for a in server.triage_writes[0][1]["attributeValuesList"]]
    assert names == ["Classification", "Action", "Comment"]
