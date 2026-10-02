"""Read and edit source files without breaking their encoding or line endings (spec D-36).

Files are edited on their raw text, never normalized, so untouched lines keep their
exact bytes. Supported encodings are UTF-8 (with or without BOM) and CP932.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

UTF8_BOM = b"\xef\xbb\xbf"


class EncodingError(Exception):
    """Raised when a file cannot be decoded or new text cannot be encoded."""


@dataclass
class SourceText:
    text: str
    encoding: str  # "utf-8" or "cp932"
    bom: bool

    @property
    def newline(self) -> str:
        """Dominant line ending of the file."""
        crlf = self.text.count("\r\n")
        lf = self.text.count("\n") - crlf
        return "\r\n" if crlf > lf else "\n"

    def lines(self) -> list[str]:
        """Lines without their line endings."""
        return self.text.splitlines()

    def encode(self) -> bytes:
        try:
            body = self.text.encode(self.encoding)
        except UnicodeEncodeError as exc:
            raise EncodingError(
                f"{self.encoding} で表現できない文字が含まれています: {exc.object[exc.start:exc.end]!r}"
            ) from exc
        return (UTF8_BOM if self.bom else b"") + body


def decode(data: bytes) -> SourceText:
    if data.startswith(UTF8_BOM):
        try:
            return SourceText(data[len(UTF8_BOM):].decode("utf-8"), "utf-8", True)
        except UnicodeDecodeError as exc:
            raise EncodingError("BOM 付き UTF-8 として読めません") from exc
    for encoding in ("utf-8", "cp932"):
        try:
            return SourceText(data.decode(encoding), encoding, False)
        except UnicodeDecodeError:
            continue
    raise EncodingError("UTF-8 / CP932 のどちらとしても読めません")


def read_source(path: str | Path) -> SourceText:
    return decode(Path(path).read_bytes())


def write_source(path: str | Path, source: SourceText) -> None:
    Path(path).write_bytes(source.encode())


def replace_once(source: SourceText, old: str, new: str) -> SourceText:
    """Replace exactly one occurrence of ``old`` with ``new``.

    The AI writes ``\\n`` line endings. When the file uses CRLF, ``old`` is also tried
    with CRLF and ``new`` is converted to match, so the file's line endings are kept.
    """
    if not old:
        raise ValueError("置換前の文字列が空です")
    candidates = [(old, new)]
    if "\n" in old and "\r\n" not in old:
        candidates.append((old.replace("\n", "\r\n"), new.replace("\r\n", "\n").replace("\n", "\r\n")))
    for target, replacement in candidates:
        count = source.text.count(target)
        if count == 1:
            updated = SourceText(source.text.replace(target, replacement), source.encoding, source.bom)
            updated.encode()  # fail early when the new text cannot be encoded
            return updated
        if count > 1:
            raise ValueError(f"置換前の文字列が {count} 箇所に一致しました。前後の行を含めて一意にしてください")
    raise ValueError("置換前の文字列が見つかりません（空白・インデントも含めて一致させてください）")
