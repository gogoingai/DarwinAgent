"""Independent counterexamples for migrated task assets, including renamed requests."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType

import networkx as nx

from darwinagent.config import RunConfig
from darwinagent.contracts import CorpusBlock, GraphResult, SourceRef
from darwinagent.kernel.execution import KernelRuntime
from darwinagent.kernel.registration import load_assets
from darwinagent.kg.graph import EntityCandidate, build_graph
from darwinagent.operators.data import DataCapabilities
from datasets.travelplanner.pipeline.data.queries import parse_dates, applicable_hc_keys

ROOT = Path(__file__).resolve().parents[3]


def records():
    return [
        (
            "Flight",
            {"Flight Number": "F1", "FlightDate": "2022-03-01"},
            {
                "OriginCityName": "Austin",
                "DestCityName": "Houston",
                "Price": 100.0,
                "DepTime": "08:00",
                "ArrTime": "09:00",
            },
        ),
        (
            "Flight",
            {"Flight Number": "F2", "FlightDate": "2022-03-03"},
            {
                "OriginCityName": "Houston",
                "DestCityName": "Austin",
                "Price": 100.0,
                "DepTime": "08:00",
                "ArrTime": "09:00",
            },
        ),
        (
            "Accommodation",
            {"NAME": "Huge, New Oasis", "city": "Houston"},
            {
                "price": 100.0,
                "minimum nights": 2.0,
                "maximum occupancy": 2,
                "room type": "Private room",
                "house_rules": "No smoking",
            },
        ),
        (
            "Distance",
            {"origin": "Austin", "destination": "Houston"},
            {"distance": "1,144 km", "duration": "11 hours"},
        ),
        *[
            (
                "Restaurant",
                {"Name": f"Food{i}", "City": "Houston"},
                {"Average Cost": 20.0, "Cuisines": "Asian, Italian"},
            )
            for i in range(5)
        ],
        (
            "Restaurant",
            {"Name": "Wrong City", "City": "Austin"},
            {"Average Cost": 20.0, "Cuisines": "Asian"},
        ),
        ("Attraction", {"Name": "Museum", "City": "Houston"}, {"Address": "Main Street"}),
    ]


class TravelAssets(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.runtime = KernelRuntime(
            load_assets(ROOT / "tasks/travel_planning").export(Path(self.td.name) / "assets"),
            RunConfig(),
        )
        self.sources = {}
        entities = []
        for i, (kind, key, props) in enumerate(records()):
            block = CorpusBlock(
                SourceRef("allowed_environment", "fixture", str(i)), json.dumps(dict(key, **props))
            )
            self.sources[block.source.id] = block
            entities.append(EntityCandidate(kind, key, props, block.source.id))
        graph = build_graph(entities, [], self.runtime.schema)
        for nd in graph.nodes.values():
            sid = nd["__sources__"][0]
            nd["__claims__"] = [{"source_id": sid, "quote": self.sources[sid].text}]
        self.graph = GraphResult(nx.freeze(graph), MappingProxyType(self.sources))
        self.rows = list(DataCapabilities(self.graph).rows.values())
        self.request = {
            "org": "Austin",
            "dest": "Houston",
            "days": 3,
            "date": ["2022-03-01", "2022-03-02", "2022-03-03"],
            "people_number": 5,
            "local_constraint": {},
            "budget": 3000,
            "visiting_city_number": 1,
        }
        self.plan = [
            {
                "days": 1,
                "current_city": "from Austin to Houston",
                "transportation": "Flight Number: F1, from Austin to Houston, Departure Time: 08:00, Arrival Time: 09:00",
                "breakfast": "-",
                "lunch": "Food0, Houston",
                "dinner": "Food1, Houston",
                "attraction": "Museum, Houston; ",
                "accommodation": "Huge, New Oasis, Houston",
            },
            {
                "days": 2,
                "current_city": "Houston",
                "transportation": "-",
                "breakfast": "Food2, Houston",
                "lunch": "Food3, Houston",
                "dinner": "Food4, Houston",
                "attraction": "Museum, Houston; ",
                "accommodation": "Huge, New Oasis, Houston",
            },
            {
                "days": 3,
                "current_city": "from Houston to Austin",
                "transportation": "Flight Number: F2, from Houston to Austin, Departure Time: 08:00, Arrival Time: 09:00",
                "breakfast": "-",
                "lunch": "-",
                "dinner": "-",
                "attraction": "-",
                "accommodation": "-",
            },
        ]

    def check(self, plan=None, request=None, rows=None):
        snapshot = {
            "stage": "answer",
            "status": "answered",
            "parameters": request or self.request,
            "structured_answer": plan or self.plan,
            "evidence": rows or self.rows,
        }
        return self.runtime.checks.run("answer", snapshot)[0]

    def test_good_plan_including_comma_name(self):
        self.assertTrue(self.check()["ok"])

    def test_wrong_city_meal(self):
        p = copy.deepcopy(self.plan)
        p[1]["lunch"] = "Wrong City, Austin"
        self.assertIn("Choice belongs to a different city", self.check(p)["issues"])

    def test_repeated_restaurant(self):
        p = copy.deepcopy(self.plan)
        p[1]["lunch"] = p[0]["lunch"]
        self.assertIn("Restaurant reused", self.check(p)["issues"])

    def test_minimum_stay(self):
        rows = copy.deepcopy(self.rows)
        next(r for r in rows if r["entity_type"] == "Accommodation")["minimum nights"] = 3.0
        self.assertIn(
            "Minimum consecutive stay constraint violated", self.check(rows=rows)["issues"]
        )

    def test_changed_budget_and_people(self):
        req = dict(self.request, budget=2000)
        self.assertIn("Budget exceeded", self.check(request=req)["issues"])
        req = dict(self.request, people_number=15)
        self.assertIn("Budget exceeded", self.check(request=req)["issues"])

    def test_wrong_transport_direction_and_date(self):
        p = copy.deepcopy(self.plan)
        p[2]["transportation"] = p[0]["transportation"]
        self.assertFalse(self.check(p)["ok"])
        req = dict(self.request, date=["2023-03-01", "2023-03-02", "2023-03-03"])
        self.assertIn("Flight date differs from request", self.check(request=req)["issues"])

    def test_request_room_constraint(self):
        req = dict(self.request, local_constraint={"room type": "Entire home/apt"})
        self.assertIn("Room type constraint violated", self.check(request=req)["issues"])

    def test_renamed_cities_remain_valid(self):
        def rename(value):
            if isinstance(value, str):
                return value.replace("Austin", "Alpha").replace("Houston", "Beta")
            if isinstance(value, list):
                return [rename(v) for v in value]
            if isinstance(value, dict):
                return {k: rename(v) for k, v in value.items()}
            return value

        self.assertTrue(
            self.check(rename(self.plan), rename(self.request), rename(self.rows))["ok"]
        )

    def test_room_people_formula_and_selected_lineage(self):
        selected = [r for r in self.rows if r["entity_type"] in {"Flight", "Accommodation"}]
        params = {
            "people": 5,
            "choices": [
                {
                    "node_id": r["node_id"],
                    **({"nights": 2} if r["entity_type"] == "Accommodation" else {}),
                }
                for r in selected
            ],
        }
        result = self.runtime.call("travel_cost", params, self.graph)
        self.assertEqual(
            result["data"], {"total": 1600.0, "valid": True}
        )  # 2 flights*100*5 + 2 nights*100*ceil(5/2)
        self.assertEqual(result["node_ids"], [])  # scalar summary returns no evidence rows
        self.assertEqual(set(result["read_node_ids"]), {r["node_id"] for r in selected})
        self.assertEqual(result["source_ids"], [])

    def test_ground_mode_formula(self):
        row = next(r for r in self.rows if r["entity_type"] == "Distance")

        def cost(mode):
            return self.runtime.call(
                "travel_cost",
                {"people": 6, "choices": [{"node_id": row["node_id"], "mode": mode}]},
                self.graph,
            )["data"]["total"]

        self.assertEqual(cost("taxi"), 2288)
        self.assertEqual(cost("self-driving"), 114)

    def test_parse_dates_and_frozen_hc_denominator(self):
        self.assertEqual(parse_dates("['2022-03-01']"), ["2022-03-01"])
        self.assertNotIn(
            "valid_transportation", applicable_hc_keys("medium", {"transportation": "no flight"})
        )
        self.assertIn(
            "valid_transportation", applicable_hc_keys("hard", {"transportation": "no flight"})
        )
