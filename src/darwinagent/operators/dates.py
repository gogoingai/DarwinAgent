"""确定性日期工具：英文会话日期解析、中文相对日期推算、答案归一化比较。

全部纯函数（零 LLM）——时间题的"计算日期"工具与判题预检共用此内核。
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from darwinagent.operators.calendar import (
    _CN_DIGITS as _CN_DIGITS,
)
from darwinagent.operators.calendar import (
    _CN_MONTH_RE as _CN_MONTH_RE,
)
from darwinagent.operators.calendar import (
    _CN_MONTH_WORDS as _CN_MONTH_WORDS,
)
from darwinagent.operators.calendar import (
    _DATE_FULL_RE as _DATE_FULL_RE,
)
from darwinagent.operators.calendar import (
    _DT_RE as _DT_RE,
)
from darwinagent.operators.calendar import (
    _MONTHS_EN as _MONTHS_EN,
)
from darwinagent.operators.calendar import (
    _PUNCT_RE as _PUNCT_RE,
)
from darwinagent.operators.calendar import (
    _WEEKDAY_WORDS as _WEEKDAY_WORDS,
)
from darwinagent.operators.calendar import (
    _YEAR_ONLY_RE as _YEAR_ONLY_RE,
)
from darwinagent.operators.calendar import (
    _days_in_month as _days_in_month,
)
from darwinagent.operators.calendar import (
    _last_weekday_before as _last_weekday_before,
)
from darwinagent.operators.calendar import (
    _monday_of as _monday_of,
)
from darwinagent.operators.calendar import (
    _shift_month as _shift_month,
)
from darwinagent.operators.calendar import (
    _weekday_of as _weekday_of,
)
from darwinagent.operators.calendar import (
    cn_num as cn_num,
)
from darwinagent.operators.calendar import (
    date_components as date_components,
)
from darwinagent.operators.calendar import (
    extract_digits as extract_digits,
)
from darwinagent.operators.calendar import (
    normalize_answer_text as normalize_answer_text,
)
from darwinagent.operators.calendar import (
    parse_cn_date as parse_cn_date,
)
from darwinagent.operators.calendar import (
    parse_session_datetime as parse_session_datetime,
)

# ---------------------------------------------------------------- 英文会话日期


# ---------------------------------------------------------------- 中文数字


# ---------------------------------------------------------------- 星期与月份


# ---------------------------------------------------------------- 显式中文日期


# ---------------------------------------------------------------- 相对日期推算
def resolve_relative(anchor: date, expr: str) -> tuple[str, str]:
    """中文相对时间表达 → (iso日期 | YYYY-MM | YYYY | 区间'起~止', 粒度)。

    粒度 ∈ {日, 周, 月, 年, 无}。无法解析返回 ("", "无")。
    周约定：周一为一周之始（中国惯例）。
    """
    e = expr.strip()
    granularity = "日"

    # —— 纯年份：'2022' / '2022年'（核心解析不处理无月日的串，先短路）
    if re.fullmatch(r"(\d{4})\s*年?", e.replace(" ", "")):
        return (re.search(r"\d{4}", e).group(0), "年")

    # —— 组合表达："X之前的那个周日 / 那一周"
    m = re.search(r"(.+?)之前的那个?(周日|星期日|礼拜天|礼拜日)", e)
    if m:
        base = _resolve_core(anchor, m.group(1))
        if base:
            d = _last_weekday_before(base, 6)  # 周日=6
            return (d.isoformat(), "日")
    m = re.search(r"(.+?)之前的那个?(周|星期|礼拜|那?一周)", e)
    if m:
        base = _resolve_core(anchor, m.group(1))
        if base:
            start = base - timedelta(days=7)
            return (f"{start.isoformat()}~{(base - timedelta(days=1)).isoformat()}", "周")

    # 去年 / 今年 / 前年（年粒度）
    if "前年" in e:
        return (str(anchor.year - 2), "年")
    if "去年" in e:
        return (str(anchor.year - 1), "年")
    if "今年" in e:
        return (str(anchor.year), "年")

    # N 个月前/后、上上个月/上个月/下个月（月粒度）
    m = re.search(r"([0-9一二两三四五六七八九十]+)\s*(?:个)?月[前后]", e)
    if m:
        n = cn_num(m.group(1)) or 0
        if n < 200:
            d = _shift_month(anchor, -n if "前" in e else n)
            return (f"{d.year:04d}-{d.month:02d}", "月")
    if "上上个月" in e:
        d = _shift_month(anchor, -2)
        return (f"{d.year:04d}-{d.month:02d}", "月")
    if "上个月" in e:
        d = _shift_month(anchor, -1)
        return (f"{d.year:04d}-{d.month:02d}", "月")
    if "下个月" in e:
        d = _shift_month(anchor, 1)
        return (f"{d.year:04d}-{d.month:02d}", "月")

    # N 个周末前/后（周粒度）
    m = re.search(r"([0-9一二两三四五六七八九十]+)\s*个?周末[前后]", e)
    if m:
        n = cn_num(m.group(1)) or 0
        if 0 < n < 100:
            base = anchor - timedelta(weeks=n) if "前" in e else anchor + timedelta(weeks=n)
            return (base.isoformat(), "周")

    # 上周末/上礼拜末：锚日之前的最近一个周末（周六~周日，周粒度）
    if re.search(r"上[周礼拜]+末", e):
        sat = _last_weekday_before(anchor, 5)  # 最近周六（周一=0…周六=5）
        return (f"{sat.isoformat()}~{(sat + timedelta(days=1)).isoformat()}", "周")

    # 上周/这周/下周（整周，周粒度，取周一为锚）
    if re.fullmatch(r"(上|上上|这|本|下)?(周|星期|礼拜)", e.replace(" ", "")):
        base = _resolve_core(anchor, e)
        if base:
            return (base.isoformat(), "周")

    val = _resolve_core(anchor, e)
    if val is None:
        return ("", "无")
    # 粒度推断：显式年月无日 → 月；仅年 → 年
    if _YEAR_ONLY_RE.fullmatch(e.replace(" ", "")):
        granularity = "年"
        return (f"{val.year}", granularity)
    m = _DATE_FULL_RE.search(e)
    if m and not m.group(3) and m.group(1):
        return (f"{val.year:04d}-{val.month:02d}", "月")
    if m and not m.group(3) and not m.group(1):
        return (f"{val.month}月", "月")
    return (val.isoformat(), granularity)


def _resolve_core(anchor: date, e: str) -> date | None:
    """相对表达 → 具体 date（尽量）。返回 None 表示无法解析。"""
    e = e.strip()

    # 今天 / 昨天 / 前天 / 明天 / 后天
    if "今天" in e:
        return anchor
    if "大前天" in e:
        return anchor - timedelta(days=3)
    if "前天" in e:
        return anchor - timedelta(days=2)
    if "昨天" in e:
        return anchor - timedelta(days=1)
    if "大后天" in e:
        return anchor + timedelta(days=3)
    if "后天" in e:
        return anchor + timedelta(days=2)
    if "明天" in e:
        return anchor + timedelta(days=1)

    # 上上周X / 上周X / 这周X / 本周X / 下周X
    # 语义对齐数据集 gold（英语 "last Friday" 惯例）：上周X = 严格早于锚日的最近一个周X
    wd = _weekday_of(e)
    if wd is not None:
        if "上上周" in e:
            return _last_weekday_before(anchor, wd) - timedelta(days=7)
        if "上周" in e or "上星期" in e or "上礼拜" in e:
            return _last_weekday_before(anchor, wd)
        this_mon = _monday_of(anchor)
        if "下周" in e or "下星期" in e or "下礼拜" in e:
            mon = this_mon + timedelta(days=7)
        else:  # 这周/本周/周X
            mon = this_mon
        return mon + timedelta(days=wd)

    # 上上个月 / 上个月 / 这个月/本月 / 下个月
    if "上上个月" in e:
        return _shift_month(anchor, -2)
    if "上个月" in e:
        return _shift_month(anchor, -1)
    if "下个月" in e:
        return _shift_month(anchor, 1)

    # N 个星期/周/月/年 前/后
    # g_v_test 修复：原正则只捕获 2 组但代码读 group(3)（"前/后"未捕获）——conv-26
    # 语料从未命中该分支故未暴露，conv-42 的"N个星期前"首次触发 IndexError
    m = re.search(r"([0-9一二两三四五六七八九十]+)\s*(?:个)?(星期|周|礼拜)([前后])", e)
    if m:
        n = cn_num(m.group(1)) or 0
        delta = timedelta(weeks=n) if n < 200 else None
        if delta is not None:
            return anchor - delta if "前" in m.group(3) else anchor + delta
    m = re.search(r"([0-9一二两三四五六七八九十]+)\s*(?:个)?月[前后]", e)
    if m:
        n = cn_num(m.group(1)) or 0
        if n < 200:
            return _shift_month(anchor, -n if "前" in e else n)
    m = re.search(r"([0-9一二两三四五六七八九十]+)\s*(?:个)?年[前后]", e)
    if m:
        n = cn_num(m.group(1)) or 0
        if n < 200:
            try:
                return date(
                    anchor.year - n if "前" in e else anchor.year + n, anchor.month, anchor.day
                )
            except ValueError:
                return None
    m = re.search(r"([0-9一二两三四五六七八九十]+)\s*天[前后]", e)
    if m:
        n = cn_num(m.group(1)) or 0
        if n < 10000:
            return anchor - timedelta(days=n) if "前" in e else anchor + timedelta(days=n)

    # 去年 / 今年 / 前年
    if "前年" in e:
        return date(anchor.year - 2, anchor.month, anchor.day)
    if "去年" in e:
        return date(anchor.year - 1, anchor.month, anchor.day)
    if "今年" in e:
        return anchor

    # 上周（整周）/ 这周 / 下周 —— 取该周周一
    if e in ("上周", "上星期", "上礼拜"):
        return _monday_of(anchor) - timedelta(days=7)
    if e in ("这周", "本周", "这星期", "本星期"):
        return _monday_of(anchor)
    if e in ("下周", "下星期", "下礼拜"):
        return _monday_of(anchor) + timedelta(days=7)

    # 显式日期
    ymd = parse_cn_date(e, default_year=anchor.year)
    if ymd and ymd[0]:
        try:
            return date(*ymd) if ymd[2] else date(ymd[0], ymd[1], 1)
        except ValueError:
            return None
    if ymd and ymd[1]:  # 只有 X月X日，无年
        try:
            return date(anchor.year, ymd[1], ymd[2] or 1)
        except ValueError:
            return None

    return None


# ---------------------------------------------------------------- 答案归一化


def answer_equivalent(gold: str | int, pred: str) -> bool:
    """确定性等价判断（判题预检用，只短路明显 exact，拿不准返回 False 交 LLM）。"""
    if pred is None:
        return False
    g = normalize_answer_text(gold)
    p = normalize_answer_text(pred)
    if not g or not p:
        return False
    if g == p:
        return True
    # "N年前" 模式：gold=N年前 且 pred 含同一数值的"X年前"表述（允许"约/大约"）→ exact
    m = re.fullmatch(r"(\d+|[一二两三四五六七八九十]+)年前", str(gold).strip())
    if m:
        n = cn_num(m.group(1))
        if n is not None:
            if n == 2 and "两年前" in p:
                return True
            if n == 10 and "十年前" in p:
                return True
            if f"{n}年前" in p.replace("约", "").replace("大约", ""):
                return True
    # 纯数字等价：gold=2022 / pred="2022年"
    gd, pd_ = extract_digits(g), extract_digits(p)
    if gd and pd_ and gd == pd_ and re.fullmatch(r"\d+", g):
        return pd_.startswith(gd) and len(re.sub(r"\D", "", p)) == len(gd)
    # 日期等价：gold 的各成分都被 pred 覆盖且相等（pred 可更细）
    gc, pc = date_components(g), date_components(p)
    if gc and pc:
        for gv, pv in ((gc[0], pc[0]), (gc[1], pc[1]), (gc[2], pc[2])):
            if gv and pv and gv != pv:
                return False
            if gv and not pv:
                return False  # gold 有年份 pred 没有 → 交给 LLM
        return True
    # 多元素列表（、/,/;分隔）集合等价（顺序无关，元素须逐一完全一致）
    gs = {normalize_answer_text(x) for x in re.split(r"[、,，;；]", str(gold)) if x.strip()}
    ps = {normalize_answer_text(x) for x in re.split(r"[、,，;；]", str(pred)) if x.strip()}
    if gs and ps and gs == ps and len(gs) > 0:
        return not gs & {""}  # 元素皆非空
    return False
