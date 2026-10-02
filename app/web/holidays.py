#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中国法定节假日（2026 年官方安排，来源：国务院办公厅通知）。

放假集合（含调休放假日期）；规则只需要放假集合。
以后每年底国务院发布新一年安排后，在这里追加一年即可。
"""

from datetime import date

# 2026 年放假日期（含首尾）
HOLIDAYS_2026 = {
    # 元旦 1.1-1.3
    date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3),
    # 春节 2.15-2.23
    date(2026, 2, 15), date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),
    date(2026, 2, 19), date(2026, 2, 20), date(2026, 2, 21), date(2026, 2, 22),
    date(2026, 2, 23),
    # 清明 4.4-4.6
    date(2026, 4, 4), date(2026, 4, 5), date(2026, 4, 6),
    # 劳动节 5.1-5.5
    date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3), date(2026, 5, 4),
    date(2026, 5, 5),
    # 端午 6.19-6.21
    date(2026, 6, 19), date(2026, 6, 20), date(2026, 6, 21),
    # 中秋 9.25-9.27
    date(2026, 9, 25), date(2026, 9, 26), date(2026, 9, 27),
    # 国庆 10.1-10.7
    date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3), date(2026, 10, 4),
    date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7),
}


def is_holiday(d):
    return d in HOLIDAYS_2026


def holiday_span_end(d):
    """d 若是放假日期，返回它所在连续假期段的最后一天；否则返回 None。"""
    if not is_holiday(d):
        return None
    end = d
    while is_holiday(end):
        end = end.fromordinal(end.toordinal() + 1)
    return end.fromordinal(end.toordinal() - 1)


def future_long_holiday(d, horizon_days=7, min_days=5):
    """d 起 horizon_days 天内是否有长度 >= min_days 的连续假期段。
    返回该段最后一天；无则 None。"""
    cursor = d
    best = None
    while cursor <= d.fromordinal(d.toordinal() + horizon_days):
        if is_holiday(cursor):
            seg_end = holiday_span_end(cursor)
            seg_start = cursor
            seg_len = (seg_end.toordinal() - seg_start.toordinal()) + 1
            if seg_len >= min_days:
                return seg_end
            cursor = seg_end.fromordinal(seg_end.toordinal() + 1)
        else:
            cursor = cursor.fromordinal(cursor.toordinal() + 1)
    return best
