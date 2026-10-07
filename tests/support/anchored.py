"""Offline anchored asset builder."""

from darwinagent.kernel.assets import Asset, KernelAssets
from tests.support.facts import SEED


def anchored_bundle(root, graph_check_ok=True):
    check = (
        'def check(candidate):\n return {"ok": True, "issues": []}'
        if graph_check_ok
        else 'def check(candidate):\n return {"ok": False, "issues": ["总是拒绝"]}'
    )
    assets = [
        Asset(
            "schema",
            "S",
            SEED.read_text(),
            {"type": "any"},
            {"type": "any"},
            (),
            description="seed",
        ),
        Asset(
            "f_find",
            "F",
            "def run(params):\n rows = nodes('AtomicFact', {'predicate': params.get('predicate','')}, limit=5)\n return [{'node_id': r.get('node_id'), 'text': r.get('text','')} for r in rows]",
            {"type": "object", "properties": {"predicate": {"type": "string"}}},
            {"type": "array"},
            ["schema"],
            description="find facts",
            trial_inputs=({"predicate": "维修"},),
        ),
        Asset(
            "c_graph",
            "C",
            check,
            stage="graph",
            schema_dependencies=["schema"],
            description="graph check",
        ),
        Asset(
            "c_answer",
            "C",
            'def check(candidate):\n return {"ok": True, "issues": []}',
            stage="answer",
            schema_dependencies=["schema"],
            description="answer check",
        ),
        *[
            Asset(
                f"p_{role}",
                "P",
                text,
                role=role,
                schema_dependencies=["schema"],
                description=f"{role} prompt",
            )
            for role, text in [
                ("extract", "抽取指引 ${schema}"),
                ("tools", "工具指引 ${schema} ${tools}"),
                ("answer", "作答指引 ${schema}"),
                ("review", "审查指引 ${schema}"),
            ]
        ],
    ]
    return KernelAssets(tuple(assets), {"kind": "test"}).export(root / "assets")
