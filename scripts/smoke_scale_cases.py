"""24 synthetic Chinese cases (72 questions), with controller-only references.

No model, transport, or embedding is created here. These bounded fixtures exercise
the existing serial-filtered f_page_facts API, not filtering beyond its 5000-node
cap. All original graphs contain at most 150 records; every page plan needs fewer
than 16 tool steps, including a final ready step.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import smoke_extended as extended

from darwinagent.kg.graph import node_id


@dataclass(frozen=True)
class Query:
    """Selection semantics used only by the controller and offline preflight."""

    serial: str
    kind: str
    value: str = ""
    paging: str = ""

    def text(self):
        serial = self.serial
        body = {
            "count": f"{serial}共有多少条维护记录？返回count。",
            "latest": f"{serial}按日期排序最后一次维护由谁在何时执行？返回technician和date。",
            "earliest": f"{serial}按日期排序最早一次维护由谁在何时执行？返回technician和date。",
            "technicians": f"{serial}全部记录中有哪些不同技术员？返回去重technicians数组。",
            "date": f"{serial}在{self.value}由谁维护？返回technician和date；该日没有记录则abstained。",
            "month-count": f"{serial}在{self.value}这个月共有多少条维护记录？返回count。",
            "month-technicians": f"{serial}在{self.value}这个月有哪些不同技术员？返回去重technicians数组。",
            "person-latest": f"{self.value}对{serial}执行维护的最后日期是什么？返回technician和date；没有记录则abstained。",
            "after-count": f"{serial}在{self.value}之后（不含当天）共有多少条维护记录？返回count。",
        }[self.kind]
        return f"设备编号必须完整精确匹配。{body}{self.paging}"

    def reference(self, records):
        rows = [(day, person) for serial, day, person in records if serial == self.serial]
        if self.kind.startswith("month-"):
            rows = [(day, person) for day, person in rows if day.startswith(self.value + "-")]
        elif self.kind == "date":
            rows = [(day, person) for day, person in rows if day == self.value]
        elif self.kind == "person-latest":
            rows = [(day, person) for day, person in rows if person == self.value]
        elif self.kind == "after-count":
            rows = [(day, person) for day, person in rows if day > self.value]
        if self.kind in {"count", "month-count", "after-count"}:
            return {"count": len(rows)}
        if not rows:
            return None
        if self.kind in {"technicians", "month-technicians"}:
            return {"technicians": sorted({person for _, person in rows})}
        day, person = min(rows) if self.kind == "earliest" else max(rows)
        return {"technician": person, "date": day}


PAGE = "使用f_page_facts，offset=0，limit=20，按next_offset逐页读取到more_remain=false。"
RESET = (
    "先调用f_page_facts，offset=999，limit=20；越界空页不能证明无记录，"
    "再调用offset=0，limit=20，按next_offset读取到more_remain=false。"
)


def specifications():
    """Original records and explicit query semantics; never pass Query to Pipeline."""
    output = []

    def add(name, records, queries):
        output.append(("scale-" + name, records, queries))

    def daily(serial, n, people=("林",), start="2028-01-01"):
        return [
            (serial, str(date.fromisoformat(start) + timedelta(days=i)), people[i % len(people)])
            for i in range(n)
        ]

    for n in (1, 19, 20, 21, 39, 40, 41, 75, 101):
        serial = f"SC-P{n}"
        records = daily(serial, n, ("林", "赵", "周"))
        if n == 19:
            records = [
                (serial + "0", "2028-01-01", "吴"),
                *records,
                ("X-" + serial, "2028-06-01", "钱"),
            ]
        elif n == 21:
            records[-1] = (serial, records[-1][1], "尾页员")
        elif n == 39:
            records = [
                row
                for pair in zip(records, daily("SC-DECOY39", n, ("孙",)), strict=True)
                for row in pair
            ]
        elif n in {40, 75}:
            records = records[::2][::-1] + records[1::2]
        elif n == 41:
            records = [
                *daily("SC-BEFORE41", 12, ("刘",)),
                *records,
                *daily("SC-AFTER41", 9, ("李",)),
            ]
        add(
            f"page-{n}",
            records,
            [
                Query(serial, "count", paging=PAGE),
                Query(serial, "latest", paging=PAGE),
                Query(serial, "technicians", paging=PAGE),
            ],
        )

    add(
        "out-of-range-reset",
        daily("SC-RESET", 21, ("甲", "乙")),
        [
            Query("SC-RESET", "count", paging=RESET),
            Query("SC-RESET", "earliest", paging=RESET),
            Query("SC-RESET", "latest", paging=RESET),
        ],
    )
    add(
        "empty-pages",
        [("SC-EMPTY-DECOY", "2028-03-03", "丙")],
        [
            Query("SC-EMPTY", "latest", paging=PAGE),
            Query("SC-EMPTY", "count", paging=PAGE),
            Query("SC-EMPTY-DECOY", "date", "2028-03-03"),
        ],
    )

    add(
        "scrambled-dates",
        [
            ("SC-SORT", "2028-12-01", "丁"),
            ("SC-SORT", "2028-01-01", "戊"),
            ("SC-SORT", "2028-05-01", "己"),
        ],
        [Query("SC-SORT", "earliest"), Query("SC-SORT", "latest"), Query("SC-SORT", "count")],
    )
    add(
        "cross-year",
        [
            ("SC-YEAR", "2029-01-01", "庚"),
            ("SC-YEAR", "2027-12-31", "辛"),
            ("SC-YEAR", "2028-12-31", "壬"),
        ],
        [
            Query("SC-YEAR", "earliest"),
            Query("SC-YEAR", "latest"),
            Query("SC-YEAR", "month-count", "2028-12"),
        ],
    )
    add(
        "leap-day",
        [
            ("SC-LEAP", "2028-03-01", "癸"),
            ("SC-LEAP", "2028-02-29", "何"),
            ("SC-LEAP", "2028-02-28", "蒋"),
        ],
        [
            Query("SC-LEAP", "date", "2028-02-29"),
            Query("SC-LEAP", "month-count", "2028-02"),
            Query("SC-LEAP", "month-technicians", "2028-02"),
        ],
    )
    add(
        "same-day-subjects",
        [
            ("SC-DAY-A", "2028-07-10", "刘"),
            ("SC-DAY-B", "2028-07-10", "周"),
            ("SC-DAY-C", "2028-07-10", "郑"),
        ],
        [Query(serial, "date", "2028-07-10") for serial in ("SC-DAY-A", "SC-DAY-B", "SC-DAY-C")],
    )
    add(
        "prefix-identifiers",
        [
            ("SC-ID-1", "2028-04-01", "甲"),
            ("SC-ID-10", "2028-04-02", "乙"),
            ("SC-ID-100", "2028-04-03", "丙"),
        ],
        [Query(serial, "latest") for serial in ("SC-ID-1", "SC-ID-100", "SC-ID-1000")],
    )
    add(
        "suffix-identifiers",
        [
            ("SC-END", "2028-04-01", "丁"),
            ("SC-END-X", "2028-04-02", "戊"),
            ("X-SC-END", "2028-04-03", "己"),
        ],
        [Query(serial, "latest") for serial in ("SC-END", "SC-END-X", "X-SC-END")],
    )
    add(
        "hyphen-identifiers",
        [
            ("SC-H-12", "2028-08-01", "庚"),
            ("SC-H1-2", "2028-08-02", "辛"),
            ("SC-H12", "2028-08-03", "壬"),
        ],
        [Query(serial, "latest") for serial in ("SC-H-12", "SC-H1-2", "SC-H12")],
    )
    add(
        "duplicate-technicians",
        daily("SC-REPEAT", 7, ("王", "李", "王")),
        [
            Query("SC-REPEAT", "count"),
            Query("SC-REPEAT", "technicians"),
            Query("SC-REPEAT", "person-latest", "李"),
        ],
    )
    add(
        "requested-month",
        [
            ("SC-MONTH", "2028-01-31", "王"),
            ("SC-MONTH", "2028-02-01", "李"),
            ("SC-MONTH", "2028-02-29", "李"),
            ("SC-MONTH", "2028-03-01", "赵"),
        ],
        [
            Query("SC-MONTH", "month-count", "2028-02"),
            Query("SC-MONTH", "month-technicians", "2028-02"),
            Query("SC-MONTH", "month-count", "2028-04"),
        ],
    )
    add(
        "person-date-relations",
        [
            ("SC-PERSON", "2028-06-11", "马"),
            ("SC-PERSON", "2028-06-01", "余"),
            ("SC-PERSON", "2028-06-10", "余"),
            ("SC-PERSON-OTHER", "2028-06-12", "余"),
        ],
        [
            Query("SC-PERSON", "person-latest", "余"),
            Query("SC-PERSON", "date", "2028-06-11"),
            Query("SC-PERSON", "person-latest", "莫"),
        ],
    )
    add(
        "absent-date",
        [
            ("SC-GAP", "2028-09-01", "曹"),
            ("SC-GAP", "2028-09-03", "韩"),
            ("SC-GAP-OTHER", "2028-09-02", "曹"),
        ],
        [
            Query("SC-GAP", "date", "2028-09-02"),
            Query("SC-GAP", "date", "2028-09-03"),
            Query("SC-GAP", "count"),
        ],
    )
    add(
        "exclusive-cutoff",
        daily("SC-CUTOFF", 5, ("杨", "邹"), "2028-10-29"),
        [
            Query("SC-CUTOFF", "after-count", "2028-10-31"),
            Query("SC-CUTOFF", "month-count", "2028-10"),
            Query("SC-CUTOFF", "month-technicians", "2028-11"),
        ],
    )
    add(
        "interleaved-month-person",
        [
            row
            for pair in zip(
                daily("SC-MIX-A", 24, ("何", "蒋"), "2028-02-20"),
                daily("SC-MIX-B", 24, ("何", "陈"), "2028-02-20"),
                strict=True,
            )
            for row in pair
        ],
        [
            Query("SC-MIX-A", "month-count", "2028-02", PAGE),
            Query("SC-MIX-B", "month-technicians", "2028-03", PAGE),
            Query("SC-MIX-A", "person-latest", "蒋", PAGE),
        ],
    )
    return output


def definitions():
    return [
        (
            case_id,
            records,
            [
                (f"{case_id}-q{i}", query.text(), query.serial, query.reference(records))
                for i, query in enumerate(queries, 1)
            ],
        )
        for case_id, records, queries in specifications()
    ]


def audit_fixtures(cases, original=None):
    """Fail closed on changed source, graph, questions, or leaked references."""
    original = definitions() if original is None else original
    if original != definitions():
        raise ValueError("Scale definitions differ from original question/source/reference version")
    if len(cases) != 24 or sum(len(item[0].questions) for item in cases) != 72:
        raise ValueError("Scale suite requires exactly 24 cases and 72 questions")
    total = 0
    for (case, snapshot, _, _, references), (case_id, records, questions) in zip(
        cases, original, strict=True
    ):
        if case.id != case_id or not 0 < len(records) <= 150:
            raise ValueError(f"Case identity/size mismatch: {case_id}")
        if len({(serial, day) for serial, day, _ in records}) != len(records):
            raise ValueError(f"Duplicate graph key: {case_id}")
        source = case.corpus[0]
        texts = [f"设备{s}于{d}由{t}维护。" for s, d, t in records]
        if source.text != "\n".join(texts):
            raise ValueError(f"Original source mismatch: {case_id}")
        payload = case.to_dict()
        if set(payload) != {"id", "corpus", "questions"} or len(case.corpus) != 1:
            raise ValueError(f"Unexpected CaseInput keys: {case_id}")
        if source.metadata:
            raise ValueError(f"Unexpected generation metadata: {case_id}")
        for question, (qid, text, serial, _) in zip(case.questions, questions, strict=True):
            if question.to_dict() != {"id": qid, "text": text, "parameters": {"serial": serial}}:
                raise ValueError(f"Original question mismatch: {case_id}/{qid}")
        if references != {qid: expected for qid, _, _, expected in questions}:
            raise ValueError(f"Controller reference mismatch: {case_id}")
        graph = json.loads((snapshot / "graph.json").read_text())
        expected_nodes = {
            node_id("Maintenance", {"serial": serial, "date": day}): (serial, day, person)
            for serial, day, person in records
        }
        actual_nodes = {
            row["id"]: (row["serial"], row["date"], row["technician"]) for row in graph["nodes"]
        }
        if len(graph["nodes"]) != len(records) or actual_nodes != expected_nodes:
            raise ValueError(f"Original graph mismatch: {case_id}")
        if any(
            row["etype"] != "Maintenance"
            or row["__sources__"] != [source.source.id]
            or json.loads(row["__key__"]) != {"serial": row["serial"], "date": row["date"]}
            for row in graph["nodes"]
        ):
            raise ValueError(f"Graph source/key mismatch: {case_id}")
        facts = [json.loads(line) for line in (snapshot / "facts.jsonl").read_text().splitlines()]
        if facts != [
            {"id": str(i), "text": text, "source_ids": [source.source.id]}
            for i, text in enumerate(texts)
        ]:
            raise ValueError(f"Original facts mismatch: {case_id}")
        manifest = json.loads((snapshot / "manifest.json").read_text())
        if (
            manifest["n_facts"] != len(records)
            or manifest["n_vector_records"] != 0
            or manifest["graph_digest"] != extended.digest(graph)
            or manifest["snapshot_digest"] != extended.digest(graph)
            or manifest["facts_digest"] != extended.digest(facts)
            or manifest["vector_digest"] != extended.digest([])
            or (snapshot / "vector/index.jsonl").read_text()
        ):
            raise ValueError(f"Original manifest mismatch: {case_id}")
        total += len(records)
    return {
        "cases": len(cases),
        "questions": 72,
        "records": total,
        "max_records_per_case": max(len(records) for _, records, _ in original),
        "synthetic": True,
        "model_calls": 0,
    }


def preflight(root: Path):
    """Prepare and audit only; safe to run without model credentials."""
    original = definitions()
    cases = extended.fixtures(root, original)
    return audit_fixtures(cases, original)
