"""Coverity Connect client (spec D-40, D-77).

REST API v2 is used where the request format could be confirmed from public code:
issue search (``POST /api/v2/issues/search``, as used by the ``mlx.coverity`` package) and
triage write-back (``PUT /api/v2/issues/triage``, Coverity Connect 2022.6.0 or later).
SOAP v9 is used for the warning path (``getStreamDefects``) and snapshot information,
whose schema is published in the WSDL of Coverity Connect.
"""

from __future__ import annotations

import ssl
import time
import xml.etree.ElementTree as ET
from html import escape
from typing import Any

import httpx

from .config import CoverityConfig, FilterSpec
from .coverity import CoverityClient, CoverityError, filter_matches
from .envvars import get_env
from .models import Event, Issue, IssueDetail, TriageAttributes

TIMEOUT = 60.0
AUTH_CHECK_TIMEOUT = 10.0
PAGE_SIZE = 200
SOAP_NS = "http://ws.coverity.com/v9"
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
PASSWORD_TEXT = ("http://docs.oasis-open.org/wss/2004/01/"
                 "oasis-200401-wss-username-token-profile-1.0#PasswordText")

# Issue fields and the REST column keys that hold them. Keys that a server does not list in
# GET /api/v2/issues/columns are not requested and the field stays empty.
COLUMN_KEYS = {
    "cid": "cid",
    "checker": "checker",
    "file": "displayFile",
    "line": "lineNumber",
    "function": "displayFunction",
    "impact": "displayImpact",
    "category": "displayCategory",
    "cwe": "cwe",
    "merge_key": "mergeKey",
    "classification": "classification",
    "action": "action",
    "severity": "severity",
    "status": "status",
}
REQUIRED_FIELDS = ("cid", "checker", "file")

# Filter keys and the column names (as listed by GET /api/v2/issues/columns) they filter on.
FILTER_COLUMNS = {
    "checkers": "Checker",
    "impacts": "Impact",
    "classification": "Classification",
    "action": "Action",
}


def _ssl_context(ca_file: str | None) -> ssl.SSLContext:
    if ca_file:
        return ssl.create_default_context(cafile=ca_file)
    import truststore  # the OS certificate store holds in-house CAs on company PCs
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def _child(node: ET.Element, name: str) -> ET.Element | None:
    for c in node:
        if _local(c.tag) == name:
            return c
    return None


def _children(node: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in node if _local(c.tag) == name]


def _text(node: ET.Element | None, *path: str) -> str:
    for name in path:
        if node is None:
            return ""
        node = _child(node, name)
    return (node.text or "").strip() if node is not None else ""


def _int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def check_auth(config: CoverityConfig, transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """Credentials set, and one light authenticated request with its time (spec D-84).

    Separates "credentials wrong" from "cannot connect / slow" within a few seconds.
    """
    user, key = get_env(config.user_env), get_env(config.key_env)
    out: dict[str, Any] = {
        "user": f"環境変数 {config.user_env}: " + ("設定あり" if user else "なし"),
        "key": f"環境変数 {config.key_env}: " + (f"設定あり（{len(key)} 文字）" if key else "なし"),
        "ok": False, "seconds": None}
    if not user or not key:
        out["result"] = "ユーザ名または認証キーが設定されていません（/coverity-setup の段階 3 で入力します）"
        return out
    kwargs: dict[str, Any] = {"base_url": config.url.rstrip("/"), "timeout": AUTH_CHECK_TIMEOUT}
    if transport is not None:
        kwargs["transport"] = transport
    else:
        kwargs["verify"] = _ssl_context(config.ca_file)
    started = time.monotonic()
    try:
        with httpx.Client(**kwargs) as http:
            resp = http.get("/api/v2/issues/columns", auth=(user, key), headers={"Accept": "application/json"},
                            params={"queryType": "bySnapshot", "retrieveGroupByColumns": "false"})
    except httpx.TimeoutException:
        out["result"] = f"{AUTH_CHECK_TIMEOUT:.0f} 秒以内に応答がありません（URL、社内のネットワーク・プロキシを確認してください）"
        return out
    except httpx.HTTPError as exc:
        out["result"] = f"Coverity に接続できません: {exc}"
        return out
    finally:
        out["seconds"] = round(time.monotonic() - started, 1)
    if resp.status_code in (401, 403):
        out["result"] = f"認証に失敗しました（HTTP {resp.status_code}）。ユーザ名・認証キーを確認してください"
    elif resp.status_code >= 400:
        out["result"] = f"Coverity が HTTP {resp.status_code} を返しました"
    else:
        out["ok"] = True
        out["result"] = f"認証できました（HTTP {resp.status_code}、{out['seconds']} 秒）"
    return out


class ConnectClient(CoverityClient):
    def __init__(self, config: CoverityConfig, transport: httpx.BaseTransport | None = None):
        user, key = get_env(config.user_env), get_env(config.key_env)
        if not user or not key:
            raise CoverityError(f"環境変数 {config.user_env} / {config.key_env} が設定されていません"
                                "（/coverity-setup で入力できます）")
        self.config = config
        self.user, self.key = user, key
        kwargs: dict[str, Any] = {"base_url": config.url.rstrip("/"), "timeout": TIMEOUT}
        if transport is not None:
            kwargs["transport"] = transport
        else:
            kwargs["verify"] = _ssl_context(config.ca_file)
        self.http = httpx.Client(**kwargs)
        self._columns: dict[str, str] | None = None  # column name -> column key
        self._request_log: list[dict[str, Any]] = []

    def _record(self, request: str, started: float, status: Any, **extra: Any) -> None:
        self._request_log.append({"request": request, "status": status,
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
            raise CoverityError(f"Coverity の認証に失敗しました（{resp.status_code}）。ユーザ名・認証キーを確認してください")
        if resp.status_code >= 400:
            try:
                message = resp.json().get("message", "")
            except Exception:
                message = resp.text[:300]
            raise CoverityError(f"Coverity REST {method} {path} が失敗しました（{resp.status_code}）: {message}")
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
            raise CoverityError(f"Coverity SOAP {operation} が失敗しました: {(fault.text or '').strip()}")
        response = next((n for n in root.iter() if _local(n.tag) == f"{operation}Response"), None)
        if response is None:
            raise CoverityError(f"Coverity SOAP {operation} の応答が想定と違います（{resp.status_code}）")
        return response

    # ---- search -----------------------------------------------------------------------------

    def columns(self) -> dict[str, str]:
        if self._columns is None:
            data = self._rest("GET", "/api/v2/issues/columns",
                              {"queryType": "bySnapshot", "retrieveGroupByColumns": "false"}) or []
            self._columns = {c["name"]: c["columnKey"] for c in data
                             if isinstance(c, dict) and c.get("name") and c.get("columnKey")}
        return self._columns

    def _filters(self, spec: FilterSpec, stream: str) -> list[dict]:
        filters = [{"columnKey": "streams", "matchMode": "oneOrMoreMatch",
                    "matchers": [{"class": "Stream", "name": stream, "type": "nameMatcher"}]}]
        names = {n.lower(): k for n, k in self.columns().items()}
        values = {"checkers": spec.checkers, "impacts": spec.impacts,
                  "classification": spec.triage.classification, "action": spec.triage.action}
        for field, column in FILTER_COLUMNS.items():
            wanted = values[field]
            key = names.get(column.lower())
            # Patterns and unknown columns are filtered after the search instead.
            if not wanted or not key or any(ch in v for v in wanted for ch in "*?["):
                continue
            filters.append({"columnKey": key, "matchMode": "oneOrMoreMatch",
                            "matchers": [{"type": "keyMatcher", "key": v} for v in wanted]})
        return filters

    def search_issues(self, spec: FilterSpec, limit: int | None = None) -> list[Issue]:
        if not spec.streams:
            raise CoverityError("条件ファイルに streams（ストリーム名）を指定してください")
        available = set(self.columns().values())
        missing = [COLUMN_KEYS[f] for f in REQUIRED_FIELDS if COLUMN_KEYS[f] not in available]
        if missing:
            raise CoverityError(f"Coverity の列に {', '.join(missing)} が見つかりません（/coverity-selftest ③ の結果を管理者に共有してください）")
        keys = [k for k in COLUMN_KEYS.values() if k in available]
        stream = spec.streams[0]  # one stream per run (spec D-79)
        body = {"filters": self._filters(spec, stream), "columns": keys,
                "snapshotScope": {"show": {"scope": "last()", "includeOutdatedSnapshots": False}}}
        issues: dict[int, Issue] = {}
        offset = 0
        # Stop paging once enough are found: a run takes only the first max_items (spec D-85).
        page = min(PAGE_SIZE, max(limit, 20)) if limit else PAGE_SIZE
        self.last_total = None
        while True:
            data = self._rest("POST", "/api/v2/issues/search", {
                "includeColumnLabels": "true", "offset": offset, "queryType": "bySnapshot",
                "rowCount": page, "sortOrder": "asc"}, body) or {}
            rows = data.get("rows") or []
            if self.last_total is None:
                self.last_total = _int(data.get("totalRows"))
            for row in rows:
                issue = self._issue({c.get("key"): c.get("value") for c in row}, stream)
                if issue and issue.cid not in issues and filter_matches(issue, spec):
                    issues[issue.cid] = issue
            offset += len(rows)
            if limit and len(issues) >= limit:
                break
            if not rows or offset >= (_int(data.get("totalRows")) or 0):
                break
        found = list(issues.values())
        return found[:limit] if limit else found

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

    def get_issue_detail(self, issue: Issue) -> IssueDetail:
        if not issue.stream:
            raise CoverityError(f"CID {issue.cid} のストリームが分かりません")
        body = (f"<mergedDefectIdDataObjs><cid>{issue.cid}</cid></mergedDefectIdDataObjs>"
                "<filterSpec><includeDefectInstances>true</includeDefectInstances>"
                "<includeHistory>false</includeHistory>"
                f"<streamIdList><name>{escape(issue.stream)}</name></streamIdList></filterSpec>")
        response = self._soap("defectservice", "getStreamDefects", body)
        instance = None
        for defect in _children(response, "return"):
            instance = _child(defect, "defectInstances")
            if instance is not None:
                break
        if instance is None:
            raise CoverityError(f"CID {issue.cid} の警告経路がストリーム {issue.stream} にありません")
        events: list[Event] = []

        def walk(node: ET.Element) -> None:
            for ev in _children(node, "events"):
                path = _text(ev, "fileId", "filePathname")
                if path:
                    events.append(Event(file=path, line=_int(_text(ev, "lineNumber")),
                                        tag=_text(ev, "eventTag"), description=_text(ev, "eventDescription"),
                                        main=_text(ev, "main").lower() == "true"))
                walk(ev)  # nested events (e.g. inside a called function)

        walk(instance)
        description = "\n\n".join(t for t in (_text(instance, "longDescription"),
                                              _text(instance, "localEffect")) if t)
        update: dict[str, Any] = {}
        if issue.cwe is None and _int(_text(instance, "cwe")) is not None:
            update["cwe"] = _int(_text(instance, "cwe"))
        if not issue.function and _text(instance, "function", "functionDisplayName"):
            update["function"] = _text(instance, "function", "functionDisplayName")
        return IssueDetail(issue=issue.model_copy(update=update), events=events,
                           checker_description=description)

    # ---- snapshots --------------------------------------------------------------------------

    def snapshot_revision(self, spec: FilterSpec, field: str) -> str | None:
        if not spec.streams:
            return None
        response = self._soap("configurationservice", "getSnapshotsForStream",
                              f"<streamId><name>{escape(spec.streams[0])}</name></streamId>")
        ids = [i for i in (_int(_text(r, "id")) for r in _children(response, "return")) if i is not None]
        if not ids:
            return None
        info = self._soap("configurationservice", "getSnapshotInformation",
                          f"<snapshotIds><id>{max(ids)}</id></snapshotIds>")
        record = _child(info, "return")
        return (_text(record, field) or None) if record is not None else None

    # ---- write-back -------------------------------------------------------------------------

    def write_triage(self, cids: list[int], attributes: TriageAttributes, comment: str,
                     spec: FilterSpec) -> None:
        values = [("Classification", attributes.classification), ("Action", attributes.action),
                  ("Severity", attributes.severity), ("Comment", comment)]
        body = {"cids": list(cids),
                "attributeValuesList": [{"attributeName": n, "attributeValue": v} for n, v in values if v]}
        self._rest("PUT", "/api/v2/issues/triage", {"triageStoreName": self.config.triage_store}, body)

