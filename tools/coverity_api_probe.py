#!/usr/bin/env python3
"""Coverity Connect API の調査スクリプト（仕様 U-1: REST / SOAP、U-2: 認証方式）。

社内の Coverity Connect に対して **読み取りのみ** の要求を送り、次を調べて Markdown に出力します。

* REST API v2 で、トリアージに必要な情報（仕様 D-33）が取れるか
* SOAP API（v9）に、必要な操作（警告経路の取得、トリアージの書き戻し）があるか
* ユーザ名＋認証キー（またはパスワード）で REST / SOAP に接続できるか
* スナップショットに解析リビジョンを記録する項目（仕様 D-17）

書き込み（トリアージの更新など）は一切行いません。書き戻しの可否は、操作が存在するかだけを調べます。
Python 3.9 以上の標準ライブラリだけで動きます。

使い方（Windows の例）::

    set COV_USER=your-name
    set COV_AUTH_KEY=xxxxxxxx
    python coverity_api_probe.py --url https://coverity.example.co.jp:8443 ^
        --project MyProduct --stream MyProduct-main --cid 12345 --out probe-report.md

社内の CA 証明書が必要な場合は ``--ca-file`` で指定してください。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from html import escape
from urllib.parse import urlparse

TIMEOUT = 30
SOAP_NS = "http://ws.coverity.com/v9"
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
PASSWORD_TEXT = ("http://docs.oasis-open.org/wss/2004/01/"
                 "oasis-200401-wss-username-token-profile-1.0#PasswordText")

# 仕様 D-33 / D-31 / D-17 に関係する SOAP 操作
SOAP_OPERATIONS = {
    "defectservice": {
        "getMergedDefectsForSnapshotScope": "CID の検索（スナップショット範囲）",
        "getMergedDefectsForStreams": "CID の検索（ストリーム）",
        "getMergedDefectsForProjectScope": "CID の検索（プロジェクト）",
        "getStreamDefects": "警告経路（イベント）の取得 ※includeDefectInstances",
        "getMergedDefectHistory": "トリアージ履歴の取得",
        "updateTriageForCIDsInTriageStore": "トリアージの書き戻し（D-31）",
        "getTriageStores": "トリアージストアの取得（書き戻し先）",
        "getFileContents": "解析時のソースの取得（ずれ検出に使える可能性）",
    },
    "configurationservice": {
        "getVersion": "サーバのバージョン",
        "getSnapshotsForStream": "ストリームのスナップショット一覧",
        "getSnapshotInformation": "スナップショットの情報（解析リビジョンの記録先、D-17）",
        "getCheckerProperties": "チェッカーの説明・CWE",
        "getStreams": "ストリーム一覧",
        "getProjects": "プロジェクト一覧",
    },
}

# 取りたい情報（D-33）に対応しそうな REST の列キー（実在するかを列一覧と突き合わせる）
WANTED_COLUMNS = ["cid", "checker", "displayFile", "displayFunction", "lineNumber", "displayImpact",
                  "cwe", "mergeKey", "classification", "action", "severity", "status",
                  "displayCategory", "displayType", "stream", "project"]


class Probe:
    def __init__(self, args: argparse.Namespace):
        self.base = args.url.rstrip("/")
        self.user = os.environ.get(args.user_env, "")
        self.key = os.environ.get(args.key_env, "")
        self.args = args
        self.ctx = ssl.create_default_context(cafile=args.ca_file) if args.ca_file else ssl.create_default_context()
        self.lines: list[str] = []
        self.host = urlparse(self.base).hostname or ""

    # ---- output ---------------------------------------------------------------------------

    def out(self, text: str = "") -> None:
        if self.args.mask and self.host:
            text = text.replace(self.host, "<coverity-host>")
        for secret in (self.key,):
            if secret:
                text = text.replace(secret, "****")
        self.lines.append(text)
        print(text)

    # ---- HTTP ---------------------------------------------------------------------------------

    def request(self, method: str, path: str, body: bytes | None = None,
                headers: dict | None = None, auth: bool = True) -> tuple[int, bytes]:
        req = urllib.request.Request(self.base + path, data=body, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        if auth and self.user:
            token = base64.b64encode(f"{self.user}:{self.key}".encode()).decode()
            req.add_header("Authorization", f"Basic {token}")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=self.ctx) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except Exception as exc:  # connection, TLS, timeout
            return 0, str(exc).encode()

    def soap(self, service: str, operation: str, body_xml: str) -> tuple[int, ET.Element | None, str]:
        envelope = (
            '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
            f'xmlns:ws="{SOAP_NS}"><soapenv:Header><wsse:Security xmlns:wsse="{WSSE}">'
            f"<wsse:UsernameToken><wsse:Username>{escape(self.user)}</wsse:Username>"
            f'<wsse:Password Type="{PASSWORD_TEXT}">{escape(self.key)}</wsse:Password>'
            "</wsse:UsernameToken></wsse:Security></soapenv:Header>"
            f"<soapenv:Body><ws:{operation}>{body_xml}</ws:{operation}></soapenv:Body></soapenv:Envelope>"
        )
        status, data = self.request("POST", f"/ws/v9/{service}", envelope.encode("utf-8"),
                                    {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": ""},
                                    auth=False)
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            return status, None, data[:300].decode("utf-8", "replace")
        fault = root.find(".//faultstring")
        return status, root, fault.text if fault is not None and fault.text else ""

    # ---- checks -------------------------------------------------------------------------------

    def check_rest(self) -> None:
        self.out("## 1. REST API v2\n")
        for path in ("/api/v2/projects", "/api/v2/streams"):
            status, data = self.request("GET", path, headers={"Accept": "application/json"})
            self.out(f"- `GET {path}` → {status} {self._brief(status, data)}")

        path = "/api/v2/issues/columns?queryType=bySnapshot&retrieveGroupByColumns=false"
        status, data = self.request("GET", path, headers={"Accept": "application/json"})
        self.out(f"- `GET {path.split('?')[0]}` → {status} {self._brief(status, data)}")
        available: list[str] = []
        if status == 200:
            try:
                available = [c.get("columnKey") for c in json.loads(data) if isinstance(c, dict)]
            except Exception:
                available = []
            found = [c for c in WANTED_COLUMNS if c in available]
            missing = [c for c in WANTED_COLUMNS if c not in available]
            self.out(f"  - 利用できる列キー（{len(available)} 個）: {', '.join(sorted(filter(None, available)))}")
            self.out(f"  - 必要な情報に対応しそうな列のうち、ある: {', '.join(found) or 'なし'}")
            self.out(f"  - ない（別名の可能性あり）: {', '.join(missing) or 'なし'}")

        if self.args.project:
            columns = [c for c in WANTED_COLUMNS if c in available] or ["cid"]
            body = {
                "filters": [{"columnKey": "project", "matchMode": "oneOrMoreMatch",
                             "matchers": [{"class": "Project", "name": self.args.project, "type": "nameMatcher"}]}],
                "columns": columns,
            }
            path = ("/api/v2/issues/search?includeColumnLabels=true&offset=0"
                    "&queryType=bySnapshot&rowCount=3&sortOrder=asc")
            status, data = self.request("POST", path, json.dumps(body).encode(),
                                        {"Content-Type": "application/json", "Accept": "application/json"})
            self.out(f"- `POST /api/v2/issues/search`（プロジェクト {self.args.project}、3 件）→ {status} {self._brief(status, data)}")
            if status == 200:
                try:
                    result = json.loads(data)
                    self.out(f"  - 総件数: {result.get('totalRows')}、取得した列: "
                             f"{', '.join(c.get('columnKey', '') for c in (result.get('columns') or [])) or columns}")
                except Exception:
                    pass
        self.out("\n- 補足: REST v2 での警告経路（イベント）の取得と、トリアージの書き戻しの可否は、"
                 "Coverity Connect の Help > API Reference（REST）で該当するエンドポイントの有無を確認してください。\n")

    def check_soap(self) -> None:
        self.out("## 2. SOAP API（v9）\n")
        for service, operations in SOAP_OPERATIONS.items():
            status, data = self.request("GET", f"/ws/v9/{service}?wsdl", auth=False)
            self.out(f"### {service}\n")
            self.out(f"- WSDL `GET /ws/v9/{service}?wsdl` → {status}")
            if status != 200:
                continue
            names = set(re.findall(rb'<(?:\w+:)?operation\s+name="([^"]+)"', data))
            names = {n.decode() for n in names}
            for op, purpose in operations.items():
                self.out(f"- {'✅' if op in names else '❌'} `{op}` — {purpose}")
            self.out("")

        status, root, fault = self.soap("configurationservice", "getVersion", "")
        version = ""
        if root is not None and not fault:
            node = root.find(".//externalVersion")
            version = node.text if node is not None else ""
        self.out(f"- 認証付き呼び出し `getVersion` → {status} "
                 f"{'成功 バージョン: ' + version if version else '失敗: ' + fault}")

        if self.args.stream:
            self.check_snapshot()
            if self.args.cid:
                self.check_events()

    def check_snapshot(self) -> None:
        stream = escape(self.args.stream)
        status, root, fault = self.soap("configurationservice", "getSnapshotsForStream",
                                        f"<streamId><name>{stream}</name></streamId>")
        ids = [n.text for n in root.iter("id")] if root is not None and not fault else []
        self.out(f"- `getSnapshotsForStream`（{self.args.stream}）→ {status} "
                 f"{f'{len(ids)} 件' if ids else '失敗: ' + fault}")
        if not ids:
            return
        latest = max(ids, key=lambda x: int(x) if x and x.isdigit() else -1)
        status, root, fault = self.soap("configurationservice", "getSnapshotInformation",
                                        f"<snapshotIds><id>{escape(latest)}</id></snapshotIds>")
        if root is None or fault:
            self.out(f"- `getSnapshotInformation` → {status} 失敗: {fault}")
            return
        self.out(f"- 最新スナップショット {latest} の項目（D-17 の解析リビジョンの記録先候補）:")
        info = root.find(".//return")
        for child in list(info) if info is not None else []:
            tag = child.tag.split("}")[-1]
            if tag in ("description", "sourceVersion", "target", "buildHost", "analysisHost",
                       "analysisVersion", "commitUser", "dateCreated", "version"):
                self.out(f"  - `{tag}`: {(child.text or '').strip()[:80]}")

    def check_events(self) -> None:
        body = (f"<mergedDefectIdDataObjs><cid>{int(self.args.cid)}</cid></mergedDefectIdDataObjs>"
                f"<filterSpec><streamIdList><name>{escape(self.args.stream)}</name></streamIdList>"
                "<includeDefectInstances>true</includeDefectInstances>"
                "<includeHistory>true</includeHistory></filterSpec>")
        status, root, fault = self.soap("defectservice", "getStreamDefects", body)
        if root is None or fault:
            self.out(f"- `getStreamDefects`（CID {self.args.cid}）→ {status} 失敗: {fault}")
            return
        events = list(root.iter("events"))
        histories = list(root.iter("history"))
        self.out(f"- `getStreamDefects`（CID {self.args.cid}）→ {status} 成功: "
                 f"イベント {len(events)} 件、履歴 {len(histories)} 件")
        if events:
            sample = events[0]
            keys = sorted({c.tag.split('}')[-1] for c in sample})
            self.out(f"  - イベントの項目: {', '.join(keys)}")

    @staticmethod
    def _brief(status: int, data: bytes) -> str:
        if status == 200:
            return "成功"
        if status == 0:
            return "接続失敗: " + data.decode("utf-8", "replace")[:150]
        if status in (401, 403):
            return "認証エラー（認証方式・権限を確認）"
        if status == 404:
            return "なし（このバージョンに存在しない可能性）"
        return data.decode("utf-8", "replace")[:150].replace("\n", " ")

    def run(self) -> None:
        self.out("# Coverity Connect API 調査結果\n")
        self.out(f"- 実施日時: {datetime.now().isoformat(timespec='seconds')}")
        self.out(f"- サーバ: {self.base}")
        self.out(f"- ユーザ: {'設定あり' if self.user else '未設定（' + self.args.user_env + '）'} ／ "
                 f"認証キー: {'設定あり' if self.key else '未設定（' + self.args.key_env + '）'}")
        self.out("- このスクリプトは読み取りのみで、Coverity のデータを変更していません。\n")
        self.check_rest()
        self.check_soap()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="Coverity Connect の URL（例: https://host:8443）")
    parser.add_argument("--user-env", default="COV_USER", help="ユーザ名の環境変数名")
    parser.add_argument("--key-env", default="COV_AUTH_KEY", help="認証キー（またはパスワード）の環境変数名")
    parser.add_argument("--project", help="検索を試すプロジェクト名")
    parser.add_argument("--stream", help="スナップショット・イベント取得を試すストリーム名")
    parser.add_argument("--cid", help="イベント取得を試す CID（--stream と併用）")
    parser.add_argument("--ca-file", help="社内 CA 証明書のファイル")
    parser.add_argument("--out", default="probe-report.md", help="出力する Markdown ファイル")
    parser.add_argument("--no-mask", dest="mask", action="store_false",
                        help="レポートのホスト名を伏せない（既定は伏せる）")
    args = parser.parse_args()
    probe = Probe(args)
    probe.run()
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(probe.lines) + "\n")
    print(f"\n出力しました: {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
