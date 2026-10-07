"""Shared deterministic parsing and calendar primitives; domain scoring stays local."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, timedelta

_MONTHS_EN = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

_DT_RE = re.compile(
    r"(?:\d{1,2}:\d{2}\s*(?:am|pm)\s+on\s+)?(\d{1,2})\s+([A-Za-z]+),?\s+(\d{4})",
    re.I,
)


def parse_session_datetime(raw: str) -> date | None:
    """'1:56 pm on 8 May, 2023' / '8 May, 2023' → date。"""
    if not raw:
        return None
    m = _DT_RE.search(raw)
    if not m:
        return None
    day, mon_name, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
    mon = _MONTHS_EN.get(mon_name)
    if not mon:
        return None
    try:
        return date(year, mon, day)
    except ValueError:
        return None


_CN_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

_CN_MONTH_WORDS = {
    "一月": 1,
    "二月": 2,
    "三月": 3,
    "四月": 4,
    "五月": 5,
    "六月": 6,
    "七月": 7,
    "八月": 8,
    "九月": 9,
    "十月": 10,
    "十一月": 11,
    "十二月": 12,
    "元月": 1,
    "正月": 1,
}


def cn_num(s: str) -> int | None:
    """中文数字（<100）→ int：'三'→3，'十'→10，'二十一'→21，'两'→2。"""
    s = s.strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if len(s) == 1:
        return _CN_DIGITS.get(s)
    if s == "十":
        return 10
    if "十" in s:
        left, _, right = s.partition("十")
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        if (left and left not in _CN_DIGITS) or (right and right not in _CN_DIGITS):
            return None
        return tens * 10 + ones
    if all(c in _CN_DIGITS for c in s) and len(s) <= 4:  # 简单连写（一三 不常见，容忍）
        v = int("".join(str(_CN_DIGITS[c]) for c in s))
        return v if v < 32 else None
    return None


_WEEKDAY_WORDS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def _weekday_of(s: str) -> int | None:
    """'周日'/'星期三'/'礼拜天' → 0..6（周一=0）。"""
    m = re.search(r"[周星期礼拜]+\s*([一二三四五六日天])", s)
    return _WEEKDAY_WORDS.get(m.group(1)) if m else None


def _monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


_DATE_FULL_RE = re.compile(
    r"(?:(\d{4})\s*年)?\s*(\d{1,2}|[一二三四五六七八九十]{1,3}|[一二]十[一二三四五六七八九]?)\s*月\s*"
    r"(?:(\d{1,2}|[一二三四五六七八九十]{1,3})\s*[日号])?"
)

_YEAR_ONLY_RE = re.compile(r"(\d{4})\s*年")

_CN_MONTH_RE = re.compile(r"([一二三四五六七八九十]{1,3})\s*月")


def parse_cn_date(s: str, default_year: int | None = None) -> tuple[int, int, int] | None:
    """解析 '2023年5月7日'/'5月7日'/'2023年6月'，返回 (y, m, d)，缺失部分为 0。
    无法解析返回 None。"""
    m = _DATE_FULL_RE.search(s)
    if not m:
        return None
    y = int(m.group(1)) if m.group(1) else 0
    mon = cn_num(m.group(2))
    day = cn_num(m.group(3)) if m.group(3) else 0
    if mon is None or not (1 <= mon <= 12):
        return None
    if day and not (1 <= day <= 31):
        return None
    if not y:
        y = default_year or 0
    return (y, mon, day)


def _shift_month(d: date, k: int) -> date:
    """月份平移（日溢出夹到月末）。"""
    idx = d.year * 12 + (d.month - 1) + k
    y, m = divmod(idx, 12)
    m += 1
    last = _days_in_month(y, m)
    return date(y, m, min(d.day, last))


def _days_in_month(y: int, m: int) -> int:
    if m == 12:
        nxt = date(y + 1, 1, 1)
    else:
        nxt = date(y, m + 1, 1)
    return (nxt - timedelta(days=1)).day


def _last_weekday_before(base: date, wd: int) -> date:
    """严格早于 base 的最近一个周 wd（wd: 0=周一..6=周日）。"""
    d = base - timedelta(days=1)
    while d.weekday() != wd:
        d -= timedelta(days=1)
    return d


_PUNCT_RE = re.compile(r"[\s，。、；;：:！!？?\.\,\(\)（）\"'“”‘’\[\]【】~—]+")


def normalize_answer_text(s: str) -> str:
    """判题预检的归一化：NFKC→中文数字月→年月日规范化→剥标点空白。"""
    if s is None:
        return ""
    t = unicodedata.normalize("NFKC", str(s))

    def _mon_repl(m):
        v = cn_num(m.group(1))
        return f"{v}月" if v else m.group(0)

    t = _CN_MONTH_RE.sub(_mon_repl, t)
    # 中文数字日：X日/X号（≤三十一）
    t = re.sub(
        r"([一二三四五六七八九十]{1,3})\s*[日号]",
        lambda m: f"{cn_num(m.group(1))}日" if cn_num(m.group(1)) is not None else m.group(0),
        t,
    )
    # 年月日 → y-m-d（保留可缺省成分）
    t = re.sub(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?", r"\1-\2-\3", t)
    t = re.sub(r"(\d{4})\s*年\s*(\d{1,2})\s*月", r"\1-\2", t)
    t = re.sub(r"(\d{4})\s*年", r"\1", t)
    t = re.sub(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?", r"\1-\2", t)
    t = re.sub(r"(\d{1,2})\s*月", r"\1月", t)
    return _PUNCT_RE.sub("", t).lower()


def extract_digits(s: str) -> str:
    return "".join(re.findall(r"\d+", str(s or "")))


def date_components(s: str) -> tuple[int, int, int] | None:
    """从归一化答案中解析 (y, m, d)（缺失为 0）。仅接受日期形态。"""
    t = str(s or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", t)
    if m:
        return int(m.group(1)), int(m.group(2)), 0
    m = re.fullmatch(r"(\d{4})", t)
    if m:
        return int(m.group(1)), 0, 0
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", t)
    if m:
        return 0, int(m.group(1)), int(m.group(2))
    m = re.fullmatch(r"(\d{1,2})月", t)
    if m:
        return 0, int(m.group(1)), 0
    return None
