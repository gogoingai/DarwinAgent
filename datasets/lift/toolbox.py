"""Credential-free container primitives. Only /workspace/task is addressable."""

import csv
import hashlib
import io
import json
import sqlite3
import sys
import zipfile
from contextlib import closing
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path("/workspace/task")
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def scoped(name):
    root = ROOT.resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Path outside current task workspace")
    return path


def table(name):
    path = scoped(name)
    if path.suffix.lower() == ".csv":
        return list(csv.DictReader(io.StringIO(path.read_text())))
    if path.suffix.lower() != ".xlsx":
        raise ValueError("Only CSV and XLSX tables are supported")
    with zipfile.ZipFile(path) as z:
        strings = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ElementTree.fromstring(z.read("xl/sharedStrings.xml")):
                strings.append("".join(si.itertext()))
        book = ElementTree.fromstring(z.read("xl/workbook.xml"))
        sheets = book.find("s:sheets", NS)
        relation = sheets[0].attrib[
            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        ]
        rels = ElementTree.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        target = next(r.attrib["Target"] for r in rels if r.attrib["Id"] == relation)
        target = target.lstrip("/") if target.startswith("/") else "xl/" + target
        rows = []
        for row in ElementTree.fromstring(z.read(target)).findall(".//s:row", NS):
            cells = {}
            for cell in row:
                letters = "".join(c for c in cell.attrib["r"] if c.isalpha())
                column = 0
                for c in letters:
                    column = column * 26 + ord(c) - 64
                v = cell.find("s:v", NS)
                kind = cell.attrib.get("t")
                value = v.text if v is not None else None
                if kind == "inlineStr":
                    value = "".join(cell.find("s:is", NS).itertext())
                elif kind == "s" and value is not None:
                    value = strings[int(value)]
                elif kind in (None, "n") and value is not None:
                    value = float(value) if any(c in value for c in ".eE") else int(value)
                cells[column - 1] = value
            rows.append(cells)
        if not rows:
            return []
        headers = rows[0]
        return [{str(v): row.get(i) for i, v in headers.items()} for row in rows[1:]]


def quote(name):
    return '"' + str(name).replace('"', '""') + '"'


def execute(name, params):
    if name == "list_files":
        return {"files": sorted(str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file())}
    if name == "read_text":
        return {"path": params["path"], "content": scoped(params["path"]).read_text()}
    if name == "read_table":
        rows = table(params["path"])
        return {"rows": rows, "row_count": len(rows), "sheet": "first"}
    if name == "transform_tables":
        with closing(sqlite3.connect(":memory:")) as db:
            for alias, filename in params["tables"].items():
                rows = table(filename)
                if not rows:
                    raise ValueError("Empty input table")
                headers = list(rows[0])
                db.execute(
                    "CREATE TABLE " + quote(alias) + "(" + ",".join(map(quote, headers)) + ")"
                )
                db.executemany(
                    "INSERT INTO "
                    + quote(alias)
                    + " VALUES ("
                    + ",".join("?" for _ in headers)
                    + ")",
                    [[r[h] for h in headers] for r in rows],
                )
            db.commit()
            allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
            db.set_authorizer(
                lambda op, *args: sqlite3.SQLITE_OK if op in allowed else sqlite3.SQLITE_DENY
            )
            cursor = db.execute(params["sql"])
            return {
                "rows": [
                    dict(zip([d[0] for d in cursor.description], row)) for row in cursor.fetchall()
                ]
            }
    if name == "write_text":
        path = scoped(params["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(params["content"])
        return {
            "path": params["path"],
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
        }
    raise ValueError("Unknown primitive")


if __name__ == "__main__":
    try:
        request = json.loads(sys.argv[1])
        result = {"ok": True, "result": execute(request["name"], request["params"])}
        encoded = json.dumps(result, ensure_ascii=False, default=str)
        if len(encoded.encode()) > 180000:
            raise ValueError("Tool output exceeds 180000 bytes; narrow query")
        print(encoded)
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__, "message": str(exc)}))
