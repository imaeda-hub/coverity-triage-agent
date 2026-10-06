"""Coverity Connect client.

REST API v2: issue search (``POST /api/v2/issues/search``, the request shape used by the public
``mlx.coverity`` package) and triage write-back (``PUT /api/v2/issues/triage``, Coverity Connect
2022.6.0 or later). SOAP API v9: warning path (``getStreamDefects``) and snapshot information
(``getSnapshotsForStream`` / ``getSnapshotInformation``).
"""

from __future__ import annotations

import ssl
import time
import xml.etree.ElementTree as ET
from html import escape
from typing import Any

import httpx

from .coverity import CoverityClient, CoverityError, SearchPage, filter_matches
from .envvars import get_env
from .models import Event, Issue, IssueDetail, TriageAttributes
from .settings import CoverityConfig, FilterSpec

TIMEOUT = 15.0
AUTH_CHECK_TIMEOUT = 10.0
MIN_PAGE, MAX_PAGE = 20, 200
SOAP_NS = "http://ws.coverity.com/v9"
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
PASSWORD_TEXT = ("http://docs.oasis-open.org/wss/2004/01/"
                 "oasis-200401-wss-username-token-profile-1.0#PasswordText")

# Issue fields and the REST column keys that hold them. Keys a server does not list in
# GET /api/v2/issues/columns are not requested and the field stays empty.
COLUMN_KEYS = {
    "cid": "cid", "checker": "checker", "file": "displayFile", "line": "lineNumber",
    "function": "displayFunction", "impact": "displayImpact", "category": "displayCategory",
    "cwe": "cwe", "merge_key": "mergeKey", "classification": "classification",
    "action": "action", "severity": "severity", "status": "status",
}
REQUIRED_FIELDS = ("cid", "checker", "file")
# Filter keys and the column names they filter on server side. Wildcard checker names and the
# status are filtered after the search, because a public example of their server-side form is missing.
FILTER_COLUMNS = {"checkers": "Checker", "impacts": "Impact",
                  "classification": "Classification", "action": "Action"}

_columns_cache: dict[str, dict[str, str]] = {}


def _ssl_context(ca_file: str | None) -> ssl.SSLContext:
    if ca_file:
        return ssl.create_default_context(cafile=ca_file)
    import truststore  # company PCs hold the in-house CA in the OS certificate store
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def _child(node: ET.Element | None, name: str) -> ET.Element | None:
    if node is None:
        return None
    return next((c for c in node if _local(c.tag) == name), None)


def _children(node: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in node if _local(c.tag) == name]


def _text(node: ET.Element | None, *path: str) -> str:
    for name in path:
        node = _child(node, name)
    return (node.text or "").strip() if node is not None else ""


def _int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _credentials(config: CoverityConfig) -> tuple[str | None, str | None]:
    return get_env(config.user_env), get_env(config.key_env)


def _http_kwargs(config: CoverityConfig, timeout: float,
                 transport: httpx.BaseTransport | None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"base_url": config.url.rstrip("/"), "timeout": timeout}
    if transport is not None:
        kwargs["transport"] = transport
    else:
        kwargs["verify"] = _ssl_context(config.ca_file)
    return kwargs


def check_auth(config: CoverityConfig, transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """Whether credentials are set, and one light authenticated request with its time."""
    user, key = _credentials(config)
    out: dict[str, Any] = {
        "user": f"環境変数 {config.user_env}: " + ("設定あり" if user else "なし"),
        "key": f"環境変数 {config.key_env}: " + (f"設定あり（{len(key)} 文字）" if key else "なし"),
        "ok": False, "seconds": None}
    if not user or not key:
        out["result"] = "ユーザ名または認証キーが設定されていません（/coverity-setup で入力します）"
        return out
    started = time.monotonic()
    try:
        with httpx.Client(**_http_kwargs(config, AUTH_CHECK_TIMEOUT, transport)) as http:
            resp = http.get("/api/v2/issues/columns", auth=(user, key), headers={"Accept": "application/json"},
                            params={"queryType": "bySnapshot", "retrieveGroupByColumns": "false"})
    except httpx.TimeoutException:
        out["result"] = f"{AUTH_CHECK_TIMEOUT:.0f} 秒以内に応答がありません（URL と社内のネットワークを確認してください）"
        return out
    except httpx.HTTPError as exc:
        out["result"] = f"Coverity に接続できません: {exc}"
        return out
    finally:
        out["seconds"] = round(time.monotonic() - started, 1)
    if resp.status_code in (401, 403):
        out["result"] = f"認証に失敗しました（HTTP {resp.status_code}）。ユーザ名と認証キーを確認してください"
    elif resp.status_code >= 400:
        out["result"] = f"Coverity が HTTP {resp.status_code} を返しました"
    else:
        out["ok"] = True
        out["result"] = f"認証できました（{out['seconds']} 秒）"
    return out


class ConnectClient(CoverityClient):
    FIRST_REQUEST_SECONDS = 5.0  # one request took about 4.6 seconds on a production server

    def __init__(self, config: CoverityConfig, transport: httpx.BaseTransport | None = None):
        super().__init__()
        user, key = _credentials(config)
        if not user or not key:
            raise CoverityError(f"環境変数 {config.user_env} / {config.key_env} が設定されていません"
                                "（/coverity-setup で入力します）")
        self.config = config
        self.user, self.key = user, key
        self.http = httpx.Client(**_http_kwargs(config, TIMEOUT, transport))

    def close(self) -> None:
        self.http.close()

    def _record(self, request: str, started: float, status: Any, **extra: Any) -> None:
        self.requests.append({"request": request, "status": status,
                              "seconds": round(time.monotonic() - started, 2), **extra})

    # ---- HTTP -------------------------------------------------------------------------------

    def _rest(self, method: str, path: str, params: dict | None = None, body: Any = None) -> Any:
        started = time.monotonic()
        extra = {"offset": params["offset"]} if params and "offset" in params else {}
        try:
            resp = self.http.request(method, path, params=params, json=body, auth=(self.user, self.key),
                                     headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            self._record(f"{method} {path}", started, type(exc).__name__, **extra)
            raise CoverityError(f"Coverity に接続できません: {exc}") from exc
        self._record(f"{method} {path}", started, resp.status_code, **extra)
        if resp.status_code in (401, 403):
            raise CoverityError(f"Coverity の認証に失敗しました（{resp.status_code}）。ユーザ名と認証キーを確認してください")
        if resp.status_code >= 400:
            try:
                message = resp.json().get("message", "")
            except Exception:
                message = resp.text[:300]
            raise CoverityError(f"Coverity REST {method} {path} が失敗しました（{resp.status_code}）: {message}",
                                fatal=resp.status_code >= 500)
        return resp.json() if resp.content else None

    def _soap(self, service: str, operation: str, body_xml: str) -> ET.Element:
        envelope = (
            '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
            f'xmlns:ws="{SOAP_NS}"><soapenv:Header><wsse:Security xmlns:wsse="{WSSE}">'
            f"<wsse:UsernameToken><wsse:Username>{escape(self.user)}</wsse:Username>"
            f'<wsse:Password Type="{PASSWORD_TEXT}">{escape(self.key)}</wsse:Password>'
            "</wsse:UsernameToken></wsse:Security></soapenv:Header>"
            f"<soapenv:Body><ws:{operation}>{body_xml}</ws:{operation}></soapenv:Body></soapenv:Envelope>"
        )
        started = time.monotonic()
        try:
            resp = self.http.post(f"/ws/v9/{service}", content=envelope.encode("utf-8"),
                                  headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": ""})
        except httpx.HTTPError as exc:
            self._record(f"SOAP {operation}", started, type(exc).__name__)
            raise CoverityError(f"Coverity に接続できません: {exc}") from exc
        self._record(f"SOAP {operation}", started, resp.status_code)
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as exc:
            raise CoverityError(f"Coverity SOAP {operation} の応答を読めません（{resp.status_code}）") from exc
        fault = next((n for n in root.iter() if _local(n.tag) == "faultstring"), None)
        if fault is not None:
            raise CoverityError(f"Coverity SOAP {operation} が失敗しました: {(fault.text or '').strip()}",
                                fatal=False)
        response = next((n for n in root.iter() if _local(n.tag) == f"{operation}Response"), None)
        if response is None:
            raise CoverityError(f"Coverity SOAP {operation} の応答が想定と違います（{resp.status_code}）")
        return response

    # ---- search -----------------------------------------------------------------------------

    def columns(self) -> dict[str, str]:
        """Column name -> column key, cached for the life of the server process."""
        key = self.config.url.rstrip("/")
        if key not in _columns_cache:
            data = self._rest("GET", "/api/v2/issues/columns",
                              {"queryType": "bySnapshot", "retrieveGroupByColumns": "false"}) or []
            _columns_cache[key] = {c["name"]: c["columnKey"] for c in data
                                   if isinstance(c, dict) and c.get("name") and c.get("columnKey")}
        return _columns_cache[key]

    def _filters(self, spec: FilterSpec, stream: str) -> list[dict]:
        filters = [{"columnKey": "streams", "matchMode": "oneOrMoreMatch",
                    "matchers": [{"class": "Stream", "name": stream, "type": "nameMatcher"}]}]
        names = {n.lower(): k for n, k in self.columns().items()}
        values = {"checkers": spec.checkers, "impacts": spec.impacts,
                  "classification": spec.triage.classification, "action": spec.triage.action}
        for field, column in FILTER_COLUMNS.items():
            wanted = values[field]
            key = names.get(column.lower())
            if not wanted or not key or any(ch in v for v in wanted for ch in "*?["):
                continue
            filters.append({"columnKey": key, "matchMode": "oneOrMoreMatch",
                            "matchers": [{"type": "keyMatcher", "key": v} for v in wanted]})
        return filters

    def search(self, spec: FilterSpec, limit: int, offset: int, seconds: float) -> SearchPage:
        started = time.monotonic()
        available = set(self.columns().values())
        missing = [COLUMN_KEYS[f] for f in REQUIRED_FIELDS if COLUMN_KEYS[f] not in available]
        if missing:
            raise CoverityError(f"Coverity の列に {', '.join(missing)} が見つかりません"
                                "（/coverity-selftest の結果を管理者に共有してください）")
        keys = [k for k in COLUMN_KEYS.values() if k in available]
        stream = spec.streams[0]
        body = {"filters": self._filters(spec, stream), "columns": keys,
                "snapshotScope": {"show": {"scope": "last()", "includeOutdatedSnapshots": False}}}
        page_size = min(MAX_PAGE, max(limit, MIN_PAGE))
        issues: list[Issue] = []
        total: int | None = None
        while True:
            page_started = time.monotonic()
            data = self._rest("POST", "/api/v2/issues/search", {
                "includeColumnLabels": "true", "offset": offset, "queryType": "bySnapshot",
                "rowCount": page_size, "sortOrder": "asc"}, body) or {}
            rows = data.get("rows") or []
            total = _int(data.get("totalRows")) if total is None else total
            for row in rows:
                issue = self._issue({c.get("key"): c.get("value") for c in row}, stream)
                if issue and filter_matches(issue, spec):
                    issues.append(issue)
            offset += len(rows)
            if not rows or offset >= (total or 0):
                return SearchPage(issues, total, None)
            now = time.monotonic()
            if len(issues) >= limit or (now - started) + (now - page_started) > seconds:
                return SearchPage(issues, total, offset)

    @staticmethod
    def _issue(row: dict[str, Any], stream: str) -> Issue | None:
        cid = _int(row.get("cid"))
        if cid is None:
            return None

        def value(field: str) -> str | None:
            v = row.get(COLUMN_KEYS[field])
            return str(v).strip() if v not in (None, "") else None

        return Issue(cid=cid, checker=value("checker") or "", file=value("file") or "",
                     line=_int(row.get("lineNumber")), function=value("function"),
                     impact=value("impact"), category=value("category"), cwe=_int(row.get("cwe")),
                     merge_key=value("merge_key"), classification=value("classification"),
                     action=value("action"), severity=value("severity"), status=value("status"),
                     stream=stream)

    # ---- warning path -----------------------------------------------------------------------

    def issue_detail(self, cid: int, stream: str) -> IssueDetail:
        body = (f"<mergedDefectIdDataObjs><cid>{cid}</cid></mergedDefectIdDataObjs>"
                "<filterSpec><includeDefectInstances>true</includeDefectInstances>"
                "<includeHistory>false</includeHistory>"
                f"<streamIdList><name>{escape(stream)}</name></streamIdList></filterSpec>")
        response = self._soap("defectservice", "getStreamDefects", body)
        defect = instance = None
        for candidate in _children(response, "return"):
            instance = _child(candidate, "defectInstances")
            if instance is not None:
                defect = candidate
                break
        if instance is None or defect is None:
            raise CoverityError(f"CID {cid} の警告経路がストリーム {stream} にありません", fatal=False)
        events: list[Event] = []

        def walk(node: ET.Element) -> None:
            for ev in _children(node, "events"):
                path = _text(ev, "fileId", "filePathname")
                if path:
                    events.append(Event(file=path, line=_int(_text(ev, "lineNumber")),
                                        tag=_text(ev, "eventTag"), description=_text(ev, "eventDescription"),
                                        main=_text(ev, "main").lower() == "true"))
                walk(ev)  # nested events, e.g. inside a called function

        walk(instance)
        main = next((e for e in events if e.main), events[-1] if events else None)
        issue = Issue(cid=cid, checker=_text(instance, "checkerName") or _text(defect, "checkerName"),
                      file=main.file if main else "", line=main.line if main else None,
                      function=_text(instance, "function", "functionDisplayName") or None,
                      cwe=_int(_text(instance, "cwe")), stream=stream)
        description = "\n\n".join(t for t in (_text(instance, "longDescription"),
                                              _text(instance, "localEffect")) if t)
        return IssueDetail(issue=issue, events=events, checker_description=description)

    # ---- snapshots --------------------------------------------------------------------------

    def snapshot_revision(self, stream: str, field: str) -> str | None:
        response = self._soap("configurationservice", "getSnapshotsForStream",
                              f"<streamId><name>{escape(stream)}</name></streamId>")
        ids = [i for i in (_int(_text(r, "id")) for r in _children(response, "return")) if i is not None]
        if not ids:
            return None
        info = self._soap("configurationservice", "getSnapshotInformation",
                          f"<snapshotIds><id>{max(ids)}</id></snapshotIds>")
        record = _child(info, "return")
        return (_text(record, field) or None) if record is not None else None

    # ---- write-back -------------------------------------------------------------------------

    def write_triage(self, cids: list[int], attributes: TriageAttributes, comment: str) -> None:
        values = [("Classification", attributes.classification), ("Action", attributes.action),
                  ("Severity", attributes.severity), ("Comment", comment)]
        body = {"cids": list(cids),
                "attributeValuesList": [{"attributeName": n, "attributeValue": v} for n, v in values if v]}
        self._rest("PUT", "/api/v2/issues/triage", {"triageStoreName": self.config.triage_store}, body)
