"""Make docs/sample-output: the layout the scripts produce, with judgements written by hand.

Run from the repository root:  uv run python tests/tools/make_sample_output.py

The flow is the real one (ct.py and the MCP tools with the fake Coverity data). Only the part of the
worker subagent is played by this script: its edits and result.json are written by hand below, so
the sample shows the layout of the reports, not what the AI would write.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugins" / "coverity-triage"
sys.path.insert(0, str(PLUGIN / "skills" / "coverity-triage-scripts" / "scripts"))

import ct  # noqa: E402
from coverity_triage import server  # noqa: E402

SAMPLE = PLUGIN / "skills" / "coverity-selftest" / "assets" / "sample-target"
OUT = REPO / "docs" / "sample-output"

FIX = {"classification": "Bug", "action": "Fix Required"}
WORK = {
    "20001": {
        "edit": ("        return -1;\n    }\n    fclose(fp);", "        fclose(fp);\n        return -1;\n    }\n    fclose(fp);"),
        "result": {
            "verdict": {"judgement": "true_bug", "summary": "fgets() が失敗したとき fp を閉じずに return している",
                        "rationale": "15 行目で fopen() が成功したあと、19 行目の fgets() が NULL を返すと 20 行目で return し、"
                                     "22 行目の fclose(fp) を通らない。fgets() は空のファイルや読み取りエラーで NULL を返すため、"
                                     "この経路は実際に成り立つ。fp は関数の外に渡されておらず、ほかで閉じられることもない。",
                        "evidence": [{"file": "src/reader.c", "line": 15, "note": "fopen() で開く"},
                                     {"file": "src/reader.c", "line": 20, "note": "閉じずに return"}]},
            "recommendation": "fix", "confidence": "high",
            "confidence_reason": "警告経路をコードで最後まで追え、根拠がすべてコードにある",
            "deviation": {"classification": "Bug", "action": "Ignore", "severity": "Moderate",
                          "comment": "本物の不具合。fgets() が失敗したとき fp を閉じずに戻るため、ファイルハンドルが漏れる"
                                     "（src/reader.c:20）。呼び出しは read_config() からの 1 回だけで、プロセスの終了時に解放されるため、"
                                     "1 回の実行では実害は小さいが、繰り返し呼ばれるとハンドルが尽きるおそれがある。"},
            "fix": {**FIX, "severity": "Moderate", "summary": "fgets() が失敗したときの return の前に fclose(fp) を足す",
                    "impact": "読み取りに失敗したときだけ動きが変わる（ファイルを閉じる）。戻り値と呼び出し元への影響はない"},
            "revision_drift": {"status": "none"}}},
    "20002": {
        "edit": ("    FILE *fp = fopen(path, \"r\");\n", "    FILE *fp;\n    if (path == NULL) {\n        return -1;\n    }\n"
                                                       "    fp = fopen(path, \"r\");\n"),
        "result": {
            "verdict": {"judgement": "false_positive", "summary": "唯一の呼び出し元 read_config() が path の NULL を除いている",
                        "rationale": "open_and_read() は static 関数で、呼び出し元は read_config()（36 行目）の 1 箇所だけ"
                                     "（ファイル内を検索して確かめた）。read_config() は 32 行目で path が NULL なら return するため、"
                                     "open_and_read() に NULL が渡る経路はない。",
                        "evidence": [{"file": "src/reader.c", "line": 32, "note": "path が NULL なら return"},
                                     {"file": "src/reader.c", "line": 36, "note": "唯一の呼び出し"}]},
            "recommendation": "deviation", "confidence": "high",
            "confidence_reason": "呼び出し元を 1 箇所に特定でき、NULL を除く処理をコードで確かめた",
            "deviation": {"classification": "False Positive", "action": "Ignore", "severity": "Unspecified",
                          "comment": "誤検知。open_and_read() は static 関数で、唯一の呼び出し元 read_config()"
                                     "（src/reader.c:32）が path が NULL のときは呼び出さずに return するため、この経路で NULL が "
                                     "fopen() に渡ることはなく、動作・安全性への影響はない。"},
            "fix": {**FIX, "severity": "Minor", "summary": "open_and_read() の先頭で path の NULL を確かめる（念のための修正）",
                    "impact": "今の呼び出し元では動きは変わらない。NULL が渡されたときは -1 を返すようになる"},
            "revision_drift": {"status": "none"}}},
    "20003": {
        "edit": ("    int c;\n    buf[0] = 'a';", "    int c;\n    if (buf == NULL) {\n        return -1;\n    }\n    buf[0] = 'a';"),
        "result": {
            "verdict": {"judgement": "true_bug", "summary": "get_buf()（malloc）の戻り値を確かめずに参照している",
                        "rationale": "get_buf() は malloc() の戻り値をそのまま返す（9 行目）。first_char() は 47 行目で受け取った "
                                     "buf を確かめずに 49 行目で書き込むため、メモリが足りないと NULL を参照する。",
                        "evidence": [{"file": "src/reader.c", "line": 9, "note": "malloc() の戻り値をそのまま返す"},
                                     {"file": "src/reader.c", "line": 49, "note": "確かめずに書き込む"}]},
            "recommendation": "fix", "confidence": "medium",
            "confidence_reason": "経路はコードで成り立つが、first_char() の呼び出し元がこのリポジトリになく、-1 を返したときの扱いは推定",
            "deviation": {"classification": "Bug", "action": "Ignore", "severity": "Major",
                          "comment": "本物の不具合。get_buf() の戻り値を確かめずに参照している（src/reader.c:49）。"
                                     "メモリが足りないときに NULL を参照して異常終了するおそれがあり、直さない理由は説明できない。"},
            "fix": {**FIX, "severity": "Major", "summary": "buf が NULL なら -1 を返す",
                    "impact": "メモリが足りないときだけ動きが変わる（-1 を返す）。呼び出し元が -1 を扱えるかは確かめていない"},
            "revision_drift": {"status": "none"}}},
    "G1": {
        "edit": ("    unsigned char a = value;\n    unsigned char b = value + 1;\n    unsigned char c = value + 2;",
                 "    unsigned char a = (unsigned char)value;\n    unsigned char b = (unsigned char)(value + 1);\n"
                 "    unsigned char c = (unsigned char)(value + 2);"),
        "result": {
            "verdict": {"judgement": "true_bug", "summary": "int を unsigned char へ暗黙に変換して代入している（Rule 10.3）",
                        "rationale": "57〜59 行目で int の式を unsigned char の変数へ代入しており、より狭い型への暗黙の変換に"
                                     "当たる。3 件とも同じ関数で同じ書き方のため、同じ原因と判断した。",
                        "evidence": [{"file": "src/reader.c", "line": 57, "note": "int から unsigned char への暗黙の変換"}]},
            "recommendation": "fix", "confidence": "high",
            "confidence_reason": "規則の違反をコードで確かめた。直し方も明示のキャストだけで済む",
            "deviation": {"classification": "Intentional", "action": "Ignore", "severity": "Unspecified",
                          "comment": "意図的。to_u8() は下位 8 ビットを取り出す関数で、上位ビットが切り捨てられることを前提にしている"
                                     "（src/reader.c:55〜60）ため、動作への影響はない。"},
            "fix": {**FIX, "severity": "Minor", "summary": "3 箇所の代入に明示のキャストを付ける",
                    "impact": "動きは変わらない（暗黙の変換を明示にするだけ）"},
            "revision_drift": {"status": "none"}}},
}


def run(*args: str) -> dict:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = ct.main([str(a) for a in args])
    out = json.loads(buffer.getvalue())
    if code:
        raise SystemExit(f"ct.py {args[0]} failed: {out}")
    return out


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        repo = tmp_path / "sample-target"
        shutil.copytree(SAMPLE, repo)
        config = repo / ".coverity-triage" / "config.yaml"
        data = yaml.safe_load(config.read_text(encoding="utf-8"))
        data["output_dir"] = str(tmp_path / "out")
        config.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        ident = ["-c", "user.name=sample", "-c", "user.email=sample@example.com"]
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", *ident, "commit", "-q", "-m", "sample"], cwd=repo, check=True)

        new = run("new-run", "--repo", repo, "--filter", "all.yaml")
        server.search_issues(str(repo), new["filter_file"], new["issues_file"])
        run_dir = Path(new["run_dir"])
        run("plan", "--run", run_dir)
        while (item := run("next", "--run", run_dir))["item"]:
            server.get_issues(item["repo_root"], item["stream"], item["cids"], item["item_dir"])
            brief = run("brief", "--run", run_dir, "--item", item["item"])
            work = WORK[item["item"]]
            source = run_dir / "work" / "fix-1" / "src" / "reader.c"
            text = source.read_text(encoding="utf-8")
            assert work["edit"][0] in text, item["item"]
            source.write_text(text.replace(work["edit"][0], work["edit"][1], 1), encoding="utf-8")
            result = {"item": item["item"], **work["result"]}
            Path(brief["result_file"]).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            run("finish", "--run", run_dir, "--item", item["item"])
        run("summary", "--run", run_dir)

        if OUT.exists():
            for child in OUT.iterdir():
                if child.name != "README.md":
                    shutil.rmtree(child) if child.is_dir() else child.unlink()
        OUT.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(run_dir / "summary.md", OUT / "summary.md")
        shutil.copytree(run_dir / "cid", OUT / "cid")
        shutil.copytree(run_dir / "patches", OUT / "patches")
        for path in (OUT / "cid").glob("*.md"):
            text = path.read_text(encoding="utf-8")
            text = "\n".join(line for line in text.splitlines() if not line.startswith("- 処理時間:")) + "\n"
            path.write_text(text, encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
