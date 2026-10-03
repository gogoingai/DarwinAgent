"""Historical evaluation record only. Extraction uses oak.agents.ExtractionAgent."""
from dataclasses import dataclass, field

@dataclass
class FactRecord:
    fid: str
    subject: str
    statement: str
    ftype: str
    date_iso: str = ""
    granularity: str = "无"
    date_raw: str = ""
    value: str = ""
    topics: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)   # ev 实体名
    session_no: int = 0

    def row(self) -> dict:
        return {"编号": self.fid, "陈述": self.statement, "主体": self.subject,
                "类型": self.ftype, "日期": self.date_iso, "日期粒度": self.granularity,
                "日期原文": self.date_raw, "数值": self.value,
                "主题": ";".join(self.topics), "出处": ";".join(self.sources)}
