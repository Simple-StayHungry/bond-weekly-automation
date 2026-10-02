#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""报告周期推断（壳层逻辑，CLI 与 Web 共享）：
从上周周报模板的 A1 实际 shared-string 引用解析上期区间，
再按 Part 的 week_end 推断本期区间。"""

import datetime
import os
import re

from app.core import template_engine as te

_DATE_IN_NAME_RE = re.compile(r"(20\d{6})")
_TITLE_RANGE_RE = re.compile(r"[\(（]\s*(\d{1,2})\.(\d{1,2})\s*-\s*(\d{1,2})\.(\d{1,2})\s*[\)）]")


def date_from_filename(path):
    m = _DATE_IN_NAME_RE.search(os.path.basename(path) if isinstance(path, str) else "")
    if not m:
        return None
    s = m.group(1)
    return datetime.date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def parse_prev_week(template_path):
    """从主 sheet A1 的实际 si 引用解析上一期区间（不是 sharedStrings 第 0 项）。"""
    fname = os.path.basename(template_path)
    m_name = _DATE_IN_NAME_RE.search(fname)
    file_date = m_name.group(1) if m_name else None
    entries = te.read_zip(template_path)
    sheet_file = te.find_main_sheet(entries)[1]
    sheet_xml = entries[sheet_file].decode("utf-8")
    m_a1 = re.search(r'<c r="A1"[^>]*t="s"[^>]*><v>(\d+)</v>', sheet_xml)
    if not m_a1:
        raise ValueError("无法从主 sheet 定位 A1 的 shared-string 引用")
    si_idx = int(m_a1.group(1))
    ss = entries["xl/sharedStrings.xml"].decode("utf-8")
    sis = re.findall(r"<si>.*?</si>|<si[^>]*/>", ss, re.S)
    if si_idx >= len(sis):
        raise ValueError(f"A1 引用的 shared-string 索引 {si_idx} 越界")
    m_title = _TITLE_RANGE_RE.search(sis[si_idx])
    if not m_title:
        raise ValueError("无法从 A1 标题解析上一期报告区间（格式应为 (MM.DD-MM.DD)）")
    year = int(file_date[:4]) if file_date else datetime.date.today().year
    prev_start = datetime.date(year, int(m_title.group(1)), int(m_title.group(2)))
    prev_end = datetime.date(year, int(m_title.group(3)), int(m_title.group(4)))
    if prev_end < prev_start:
        prev_start = prev_start.replace(year=year - 1)
    return prev_start, prev_end


def resolve_week_end(rules, prev_end, default=5):
    """解析 Part 的截止星期。

    week_end 支持两种口径：
    - 0~6 的整数：固定星期（4=周五，5=周六）；
    - inherit/template/auto：继承当前确认模板上一期的实际截止星期。

    广东历史模板同时出现过周五和周六版本，不能永久写死；继承模板可以
    保持当前模板口径，同时避免凭文件名或抓取日猜测报告截止日。
    """
    raw = (rules or {}).get("week_end", default)
    if isinstance(raw, str) and raw.strip().lower() in {"inherit", "template", "auto"}:
        wd = int(prev_end.weekday())
        if wd not in (4, 5):
            raise ValueError(
                f"模板上一期截止日 {prev_end:%Y-%m-%d} 不是周五/周六，"
                "无法安全继承 week_end；请检查模板标题或在 parts.yaml 显式指定 4/5")
        return wd
    wd = int(raw)
    if wd < 0 or wd > 6:
        raise ValueError(f"非法 week_end={raw!r}，应为 0~6 或 inherit/template/auto")
    return wd


def next_week_range(prev_start, prev_end, week_end=5):
    """本期区间只由“上期模板 + Part week_end + 法定节假日”确定：
    - 起点永远从上一期结束后的下一个周一开始；
    - 平时结束日对齐本 Part 的 week_end（4=周五，5=周六）；
    - 节假日合并：若截止日本身落在覆盖>=2个工作日的假期段，或未来 7 天内
      有>=5天的长假，则结束日顺延到假期段结束后的第一个周截止日。"""
    from . import holidays
    # 本期起点永远从上一期结束后的下一个周一开始（跳过整个周末）
    start = prev_end + datetime.timedelta(days=1)
    while start.weekday() != 0:      # 0 = 周一
        start += datetime.timedelta(days=1)
    aligned = start + datetime.timedelta(days=(week_end - start.weekday()) % 7)
    if aligned <= start:
        aligned = start
    end = aligned

    # 1) 截止日在假期段内，且该假期段覆盖的工作日 >= 2 → 顺延到段后第一个周截止日
    if holidays.is_holiday(end):
        seg_end = holidays.holiday_span_end(end)
        # 假期段覆盖的工作日数（周一~周五被放假占用的天数）
        workdays_in_seg = sum(
            1 for d in (end + datetime.timedelta(days=i) for i in range((seg_end - end).days + 1))
            if d.weekday() < 5)
        if workdays_in_seg >= 2:
            cursor = seg_end + datetime.timedelta(days=1)
            while cursor.weekday() != week_end:
                cursor += datetime.timedelta(days=1)
            end = cursor
    else:
        # 2) 未来 7 天内开始的长假（>=5 天）→ 顺延合并
        seg_end = holidays.future_long_holiday(end, horizon_days=7, min_days=5)
        if seg_end:
            cursor = seg_end + datetime.timedelta(days=1)
            while cursor.weekday() != week_end:
                cursor += datetime.timedelta(days=1)
            end = cursor
    return start, end
