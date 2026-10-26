"""Regression contracts for workbook input failures observed in real evaluation."""

import tempfile
import unittest
import zipfile
from pathlib import Path

from datasets.lift.full_tools import FullLocalTools


def workbook(path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(
            "xl/workbook.xml",
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="assumptions" sheetId="1" r:id="rId1"/><sheet name="monthly_projection" sheetId="2" r:id="rId2"/></sheets></workbook>',
        )
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships><Relationship Id="rId1" Target="/xl/worksheets/sheet1.xml"/><Relationship Id="rId2" Target="worksheets/sheet2.xml"/></Relationships>',
        )
        z.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>name</t></is></c><c r="B1" t="inlineStr"><is><t>amount</t></is></c></row><row r="2"><c r="A2" t="inlineStr"/><c r="B2"><v>3</v></c></row></sheetData></worksheet>',
        )
        z.writestr(
            "xl/worksheets/sheet2.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>mrr</t></is></c></row><row r="2"><c r="A2"><v>10</v></c></row><row r="3"><c r="A3"><v>20</v></c></row></sheetData></worksheet>',
        )


class WorkbookRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        workbook(self.root / "model.xlsx")
        self.tools = FullLocalTools(self.root, readonly=True)
        self.tools.audit_path = None

    async def test_empty_inline_string_is_blank_and_default_first_sheet_is_compatible(self):
        result = await self.tools.read_table({"path": "model.xlsx"})
        self.assertEqual(result["rows"], [{"name": None, "amount": 3}])
        self.assertEqual(result["sheet"], "first")
        self.assertEqual(result["sheets"], ["assumptions", "monthly_projection"])

    async def test_named_sheet_is_reachable_through_public_tool_contract(self):
        spec = next(t for t in self.tools.specs() if t.name == "read_table")
        self.assertIn("sheet", spec.input_contract["properties"])
        result = await self.tools.read_table({"path": "model.xlsx", "sheet": "monthly_projection"})
        self.assertEqual(result["rows"], [{"mrr": 10}, {"mrr": 20}])
        self.assertEqual(result["sheet"], "monthly_projection")

    async def test_sql_selects_named_sheet_without_rewriting_workbook(self):
        before = (self.root / "model.xlsx").read_bytes()
        result = await self.tools.transform_tables(
            {
                "tables": {"projection": {"path": "model.xlsx", "sheet": "monthly_projection"}},
                "sql": "SELECT SUM(mrr) AS total FROM projection",
            }
        )
        self.assertEqual(result["rows"], [{"total": 30}])
        self.assertEqual((self.root / "model.xlsx").read_bytes(), before)

    async def test_missing_sheet_has_actionable_public_error(self):
        with self.assertRaisesRegex(Exception, "table_sheet_not_found"):
            await self.tools.read_table({"path": "model.xlsx", "sheet": "missing"})

    async def test_invalid_table_reference_has_actionable_public_error(self):
        with self.assertRaisesRegex(Exception, "invalid_table_reference"):
            await self.tools.transform_tables(
                {
                    "tables": {"t": {"path": "model.xlsx", "unexpected": "x"}},
                    "sql": "SELECT * FROM t",
                }
            )

    async def test_workbook_mutation_still_rejected_by_readonly_binding(self):
        with self.assertRaises(PermissionError):
            await self.tools.write_text({"path": "model.xlsx", "content": "replace"})
