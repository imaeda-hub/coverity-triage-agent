"""Coverity Connect client against a fake server (REST v2 + SOAP v9 formats, spec D-77)."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from coverity_triage.config import CoverityConfig, FilterSpec
from coverity_triage.connect import ConnectClient
from coverity_triage.coverity import CoverityError
from coverity_triage.models import TriageAttributes

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
    return FakeServer()


def make(server, **kw) -> ConnectClient:
    return ConnectClient(CoverityConfig(url="https://cov.example:8443/", **kw),
                         transport=httpx.MockTransport(server))


def test_search_maps_columns_and_filters(server, monkeypatch):
    monkeypatch.setattr("coverity_triage.connect.PAGE_SIZE", 2)  # force paging
    client = make(server)
    spec = FilterSpec(streams=["main"], impacts=["High"], triage={"status": ["New"]})
    issues = client.search_issues(spec)
    assert [i.cid for i in issues] == [101]  # 102 is Triaged (client-side), 103 is Low (server-side)
    issue = issues[0]
    assert (issue.checker, issue.file, issue.line, issue.function, issue.stream) == \
        ("NULL_RETURNS", "/build/src/a.c", 12, "f", "main")
    search = [r for r in server.requests if r.url.path == "/api/v2/issues/search"][0]
    body = json.loads(search.content)
    assert body["snapshotScope"]["show"]["scope"] == "last()"
    assert {"columnKey": "displayImpact", "matchMode": "oneOrMoreMatch",
            "matchers": [{"type": "keyMatcher", "key": "High"}]} in body["filters"]


def test_search_checker_pattern_is_filtered_locally_and_streams_dedupe(server):
    client = make(server)
    issues = client.search_issues(FilterSpec(streams=["main", "rel"], checkers=["MISRA*", "NULL_*"]))
    assert sorted(i.cid for i in issues) == [101, 103]
    assert {i.cid: i.stream for i in issues}[101] == "main"
    for r in server.requests:
        if r.url.path == "/api/v2/issues/search":
            assert all(f["columnKey"] != "checker" for f in json.loads(r.content)["filters"])


def test_search_requires_streams(server):
    with pytest.raises(CoverityError, match="streams"):
        make(server).search_issues(FilterSpec(project="P"))


def test_auth_error_is_explained(server, monkeypatch):
    monkeypatch.setenv("COV_AUTH_KEY", "wrong")
    with pytest.raises(CoverityError, match="認証"):
        make(server).search_issues(FilterSpec(streams=["main"]))


def test_missing_credentials(monkeypatch):
    monkeypatch.delenv("COV_USER", raising=False)
    monkeypatch.setattr("coverity_triage.connect.get_env", lambda name: None)
    with pytest.raises(CoverityError, match="COV_USER"):
        ConnectClient(CoverityConfig(url="https://x"), transport=httpx.MockTransport(lambda r: None))


def test_issue_detail_flattens_events(server):
    client = make(server)
    issue = client.search_issues(FilterSpec(streams=["main"], checkers=["NULL_RETURNS"]))[0]
    detail = client.get_issue_detail(issue)
    assert [(e.file, e.line, e.tag, e.main) for e in detail.events] == [
        ("/build/src/a.c", 10, "returned_null", False),
        ("/build/src/buf.c", 3, "null", False),
        ("/build/src/a.c", 12, "dereference", True)]
    assert detail.issue.cwe == 476
    assert "Dereference of a NULL return value" in detail.checker_description
    assert "May crash" in detail.checker_description


def test_soap_fault_becomes_error(server):
    client = make(server)
    issue = client.search_issues(FilterSpec(streams=["main"]))[0].model_copy(update={"stream": "xyz"})
    with pytest.raises(CoverityError, match="No stream found"):
        client.get_issue_detail(issue)


def test_snapshot_revision_uses_latest_snapshot(server):
    client = make(server)
    assert client.snapshot_revision(FilterSpec(streams=["main"]), "sourceVersion") == "a1b2c3d"
    assert client.snapshot_revision(FilterSpec(streams=["main"]), "description") == "nightly"
    assert client.snapshot_revision(FilterSpec(streams=["main"]), "target") is None


def test_write_triage_uses_rest_put(server):
    client = make(server, triage_store="Product Store")
    client.write_triage([101, 103], TriageAttributes(classification="False Positive", action="Ignore",
                                                     severity="Unspecified"), "誤検知。理由。", FilterSpec())
    params, body = server.triage_writes[0]
    assert params == {"triageStoreName": "Product Store"}
    assert body == {"cids": [101, 103], "attributeValuesList": [
        {"attributeName": "Classification", "attributeValue": "False Positive"},
        {"attributeName": "Action", "attributeValue": "Ignore"},
        {"attributeName": "Severity", "attributeValue": "Unspecified"},
        {"attributeName": "Comment", "attributeValue": "誤検知。理由。"}]}
