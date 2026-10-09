"""Shared schema for offline LoCoMo graph construction tests."""

MINIMAL_S = """\
meta:
  task: conversation_memory
entity_types:
  原子事实:
    primary_key: [编号]
    attributes:
      - {name: 编号, dtype: string}
      - {name: 陈述, dtype: string}
      - {name: 主体, dtype: string}
      - {name: 类型, dtype: string}
      - {name: 日期, dtype: string}
      - {name: 日期粒度, dtype: string}
      - {name: 日期原文, dtype: string}
      - {name: 数值, dtype: string}
      - {name: 主题, dtype: string}
      - {name: 出处, dtype: string}
  人物:
    primary_key: [姓名]
    attributes:
      - {name: 姓名, dtype: string}
  主题:
    primary_key: [名称]
    attributes:
      - {name: 名称, dtype: string}
  会话:
    primary_key: [序号]
    attributes:
      - {name: 序号, dtype: int}
      - {name: 日期, dtype: string}
      - {name: 星期, dtype: string}
relation_types:
  归属于: {domain: 原子事实, range: 人物}
  属于主题: {domain: 原子事实, range: 主题}
  记录于: {domain: 原子事实, range: 会话}
"""


def fixture_dataset(root, questions=199):
    """Tiny synthetic conversation and generated QA IDs for offline boundary tests."""
    import json
    from pathlib import Path

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    turns = [
        {"dia_id": "D1:1", "speaker": "甲", "text": "我昨天修好了打印机。"},
        {
            "dia_id": "D1:2",
            "speaker": "乙",
            "text": "我买了一盒纸。",
            "blip_caption": "fixture caption",
            "query": "fixture search intent",
        },
        {"dia_id": "D1:3", "speaker": "甲", "text": "下周再检查一次。"},
    ]
    raw = [
        {
            "sample_id": "conv-26",
            "conversation": {
                "speaker_a": "甲",
                "speaker_b": "乙",
                "session_1_date_time": "8 May 2023",
                "session_1": turns,
                "session_2_date_time": "25 May 2023",
                "session_2": [{"dia_id": "D2:1", "speaker": "乙", "text": "今天检查了纸盒。"}],
            },
            "qa": [
                {
                    "question": f"测试问题{i}",
                    "answer": "测试答案",
                    "category": i % 4 + 1,
                    "evidence": ["D1:1"],
                }
                for i in range(questions)
            ],
        }
    ]
    for name in ("locomo10_zh.json", "locomo10.json"):
        (root / name).write_text(json.dumps(raw, ensure_ascii=False))
    return root
