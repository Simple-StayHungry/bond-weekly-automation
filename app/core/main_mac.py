#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""债券周报的数据读取及文本生成函数，供统一 engine 复用。

三所通过 exchange_fetch 的官方接口获取并严格校验分页与代码字典。
直接执行本历史模块已停止支持；请使用项目根目录 weekly.py 或 跑周报.command，
由统一引擎执行快照验证、异常阻断及正式版/预览版判定。
"""

import json
import logging
import os
import re

import pandas as pd

import rules as bond_rules

APP_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_DIR = os.path.join(APP_DIR, "input")
OUTPUT_DIR = os.path.join(APP_DIR, "output")
RAW_DIR = os.path.join(APP_DIR, "raw_data")
DEFAULT_DATADIR = os.path.expanduser("~/Desktop/债券更新")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36")

# 模块导入零副作用；日志与任务目录统一由 engine 管理。
log = logging.getLogger("bond")

# 可选：将某个承销商项目优先排序。公开版默认不设置任何特定机构。
# 需要时可在启动前设置 WEEKLY_PRIORITY_UNDERWRITER，例如：
#   export WEEKLY_PRIORITY_UNDERWRITER="示例证券"
PRIORITY_UNDERWRITER = os.environ.get("WEEKLY_PRIORITY_UNDERWRITER", "").strip()

def _is_priority_underwriter(value):
    return bool(PRIORITY_UNDERWRITER and PRIORITY_UNDERWRITER in str(value or ""))


# ---------------------------------------------------------------- 省份标准化

# 直辖市及特殊地区在“新发行债券.xlsx / NAFMII进度.xlsx”中的实际写法
# （实测：京津沪渝为短名，其余省区为全称）
DATA_PROVINCE_MAP = {
    "北京": "北京", "天津": "天津", "上海": "上海", "重庆": "重庆",
    "内蒙古": "内蒙古自治区", "广西": "广西壮族自治区", "西藏": "西藏自治区",
    "宁夏": "宁夏回族自治区", "新疆": "新疆维吾尔自治区",
    "香港": "香港特别行政区", "澳门": "澳门特别行政区",
}


def to_data_province(p: str) -> str:
    """输入短名 → 数据文件里的省份写法。"""
    p = str(p).strip()
    if p in DATA_PROVINCE_MAP:
        return DATA_PROVINCE_MAP[p]
    return p + "省"


def split_provinces(text: str):
    return [p.strip() for p in str(text).split(",") if p.strip()]


# ---------------------------------------------------------------- 数据文件定位

def find_data_files(datadir):
    """在数据源目录找最新日期的输入文件；缺的再回退到 input/。"""
    def _pick(dirs, pattern):
        cands = []
        for d in dirs:
            if not os.path.isdir(d):
                continue
            for f in os.listdir(d):
                if f.startswith("~$") or not f.endswith(".xlsx"):
                    continue
                if pattern.match(f):
                    cands.append(os.path.join(d, f))
        if not cands:
            return None
        cands.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        return cands[0]

    dirs = [datadir, INPUT_DIR]
    new_bond = _pick(dirs, re.compile(r"^.*新发行债券.*\.xlsx$"))
    nafmii = _pick(dirs, re.compile(r"^.*NAFMII.*\.xlsx$"))
    short_map = _pick(dirs, re.compile(r"^简称\.xlsx$"))
    return {"新发行债券": new_bond, "NAFMII": nafmii, "简称": short_map}


# ---------------------------------------------------------------- 1. 上交所

SSE_TYPE_MAP = {"0": "小公募", "5": "小公募", "1": "私募", "6": "私募",
                "2": "ABS", "3": "大公募", "7": "大公募", "4": "公募REITs"}

SSE_STATUS_MAP = {
    "0": "已申报", "1": "已受理", "2": "已反馈", "3": "已接收反馈意见",
    "4": "通过", "5": "未通过", "8": "终止", "9": "中止",
    "10": "已回复交易所意见", "11": "提交注册", "12": "注册生效",
}


def sse_status(row):
    if row.get("AUDIT_SUB_STATUS") == "901":
        return "承销商/管理人超期中止"
    return SSE_STATUS_MAP.get(str(row.get("AUDIT_STATUS", "")), "-")


def fetch_sse(province_list, logger=None):
    """Strict official capture; preserves the historical call signature."""
    try:
        from .exchange_fetch import fetch_sse as fetch
    except ImportError:
        from exchange_fetch import fetch_sse as fetch
    return fetch(province_list, logger=logger)


# ---------------------------------------------------------------- 2. 深交所

def strip_html(s):
    return re.sub(r"<[^>]+>", "", str(s or "")).strip()


def fetch_szse(province_list, logger=None):
    """Strict official capture; preserves the historical call signature."""
    try:
        from .exchange_fetch import fetch_szse as fetch
    except ImportError:
        from exchange_fetch import fetch_szse as fetch
    return fetch(province_list, logger=logger)


# ---------------------------------------------------------------- 3. 北交所

def to_short_province(p: str) -> str:
    """全称省名 → 短名（用于 B 列三所省份统一，对齐人工版）。"""
    return re.sub(r"(壮族自治区|回族自治区|维吾尔自治区|特别行政区|自治区|省|市)$", "",
                  str(p).strip())


def fetch_bse(province_full_list, logger=None):
    """Strict official capture; preserves the historical call signature."""
    try:
        from .exchange_fetch import fetch_bse as fetch
    except ImportError:
        from exchange_fetch import fetch_bse as fetch
    return fetch(province_full_list, logger=logger)


# ---------------------------------------------------------------- 4. 文本生成（沿用原程序逻辑）

# 保留旧导入兼容；规则实现只在 rules.py 维护。
_ExactNameMap = bond_rules.ExactNameMap


def abbreviate(raw_text, name_map, fallback_out=None, mapping_audit=None,
               delimiter="，", audit_context=None, split_whitespace=False):
    """兼容入口；三栏承销商均调用 rules.py 的唯一精确映射实现。"""
    return bond_rules.abbreviate(
        raw_text, name_map, fallback_out=fallback_out, mapping_audit=mapping_audit,
        delimiter=delimiter, audit_context=audit_context, split_whitespace=split_whitespace)


def format_new_bond(row, idx, name_map=None, fallback_out=None, mapping_audit=None):
    """板块一：本周新发行债券。"""
    name = str(row.get("债券全称", "")).strip()
    if not name or name.lower() in ("nan", "none"):
        return ""

    scale = ""
    try:
        scale_val = row.get("发行规模(亿)", "")
        if pd.notna(scale_val) and str(scale_val).strip() != "" and str(scale_val).lower() not in ("nan", "none"):
            scale = f"{round(float(scale_val), 2)}亿元"
        else:
            plan_val = row.get("计划发行规模(亿)", "")
            if pd.notna(plan_val) and str(plan_val).strip() != "" and str(plan_val).lower() not in ("nan", "none"):
                scale = f"{round(float(plan_val), 2)}亿元（计划）"
    except Exception:
        pass

    rate = ""
    try:
        rate_val = row.get("票面利率(%)", "")
        if pd.notna(rate_val) and str(rate_val).strip() != "" and str(rate_val).lower() not in ("nan", "none"):
            rate = f"{round(float(rate_val), 2)}%"
    except Exception:
        pass

    term = ""
    try:
        term_val = row.get("发行期限(年)", "")
        if pd.notna(term_val) and str(term_val).strip() != "" and str(term_val).lower() not in ("nan", "none"):
            term = f"{round(float(term_val), 2)}年"
            if "特殊期限" in row and isinstance(row["特殊期限"], str) and row["特殊期限"].strip():
                term += f"（{row['特殊期限'].strip()}）"
    except Exception:
        pass

    rating = str(row.get("主体评级", "")).strip()
    if rating.lower() in ("", "nan", "none"):
        rating = ""

    # R8：债项评级。数据源“债券评级”列有值即列示。
    debt_rating = bond_rules.fmt_debt_rating(row.get("债券评级", ""))

    underwriter = abbreviate(row.get("主承销商", ""), NAME_MAP if name_map is None else name_map,
                             fallback_out=fallback_out, mapping_audit=mapping_audit,
                             audit_context={"section": "newbond", "project": name})
    if underwriter.lower() in ("", "nan", "none"):
        underwriter = ""

    elements = []
    if scale:
        elements.append(f"发行规模：{scale}")
    if term:
        elements.append(f"期限：{term}")
    if rate:
        elements.append(f"利率：{rate}")
    if rating:
        elements.append(f"主体评级：{rating}")
    if debt_rating:
        elements.append(f"债项评级：{debt_rating}")
    if underwriter:
        elements.append(underwriter)

    info_line = "，".join(elements)
    return f"{idx}、{name}\n{info_line}" if info_line else f"{idx}、{name}"


def build_nib_section(df, data_province_list, name_map=None, start_date=None,
                      end_date=None, logger=None, stats=None, mapping_audit=None):
    """板块一：本周新发行债券。
    先按“发行起始日”落在报告周期内过滤（防错周输入），再按省份筛选。
    stats: 可选 dict；简称表未命中、且未被“股份有限公司”安全兜底规则处理而
    保留原文的机构名会写入 stats["fallback_underwriters"]（去重），供上层 warning 提示。"""
    df = df.copy()
    if "发行起始日" in df.columns and (start_date or end_date):
        df["发行起始日"] = pd.to_datetime(df["发行起始日"], errors="coerce")
        if start_date:
            df = df[df["发行起始日"] >= pd.to_datetime(start_date)]
        if end_date:
            df = df[df["发行起始日"] <= pd.to_datetime(end_date)]

    full_text_parts = []
    fallback_seen = []
    for province in data_province_list:
        df_sub = df[df["发行人省份"].astype(str).str.strip() == province].dropna(how="all").reset_index(drop=True)
        if df_sub.empty:
            continue
        # 可选优先承销商排序；公开版默认不启用。
        df_sub = df_sub.assign(
            _priority=df_sub["主承销商"].astype(str).map(_is_priority_underwriter))
        # 排序后重置索引，保证编号连续。
        df_sub = df_sub.sort_values("_priority", ascending=False, kind="stable").reset_index(drop=True)
        bond_lines = [format_new_bond(row, i + 1, name_map, fallback_out=fallback_seen,
                                      mapping_audit=mapping_audit)
                      for i, row in df_sub.iterrows()]
        bond_lines = [b for b in bond_lines if b.strip()]
        if not bond_lines:
            continue
        summary = f"本周{province.replace('省', '').replace('市', '')}一共发行 {len(bond_lines)} 支债券：\n"
        full_text_parts.append(summary + "\n".join(bond_lines))
    if stats is not None:
        stats["fallback_underwriters"] = fallback_seen
    return "\n\n".join(full_text_parts)


def generate_bond_summary_by_province(SH_RAW_df, SZ_RAW_df, BJ_RAW_df, start_date=None,
                                      end_date=None, business_rules=None, name_map=None,
                                      fallback_out=None, mapping_audit=None):
    """板块二：公司债券项目情况（三所，按更新日期切片）。
    business_rules: parts.yaml 的 rules（exclude_abs_reits / amount_style_sse /
                    amount_style_szse），未配置时用默认口径。"""
    br = business_rules or {}
    exclude_abs_reits = br.get("exclude_abs_reits", True)
    amount_style_sse = br.get("amount_style_sse", "sse")
    amount_style_szse = br.get("amount_style_szse", "raw")
    exchange_uw_delimiter = br.get("exchange_underwriter_delimiter", ",")

    def process(df, name_col, underwriter_col, type_col, amount_col, status_col, date_col,
                province_col, amount_fmt="raw"):
        if df is None or df.empty:
            return {}
        df = df.copy()
        df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
        if start_date:
            df = df[df[date_col] >= pd.to_datetime(start_date)]
        if end_date:
            df = df[df[date_col] <= pd.to_datetime(end_date)]
        # R3：剔除 ABS 与公募 REITs（可配置）
        if exclude_abs_reits:
            df = bond_rules.exclude_non_company(df, type_col)
        df = df[df[province_col].notna()]
        df = df.reset_index(drop=True)

        province_summary = {}
        for prov, group in df.groupby(province_col):
            raw_lines = []
            for _, row in group.iterrows():
                name = str(row.get(name_col, "")).strip()
                uw_raw = str(row.get(underwriter_col, "")).strip()
                underwriter = abbreviate(
                    uw_raw, NAME_MAP if name_map is None else name_map,
                    fallback_out=fallback_out, mapping_audit=mapping_audit,
                    delimiter=exchange_uw_delimiter, split_whitespace=True,
                    audit_context={"section": "exchange", "project": name})
                bond_type = str(row.get(type_col, "")).strip()
                amount = row.get(amount_col, "")
                # R1/R2：金额格式按交易所口径（可配置）
                amount = (bond_rules.fmt_amount_sse(amount) if amount_fmt == "sse"
                          else bond_rules.fmt_amount_raw(amount))
                status = str(row.get(status_col, "")).strip()
                date = row.get(date_col, "")
                info_line = (f"品种：{bond_type}，金额：{amount}亿元，主承销商：{underwriter}，"
                             f"状态：{status}，更新日期：{date.strftime('%Y-%m-%d') if pd.notnull(date) else ''}")
                raw_lines.append((name, info_line))
            # 可选优先承销商排序；其余保持原顺序。
            raw_lines.sort(key=lambda x: 0 if _is_priority_underwriter(x[1]) else 1)
            lines = [f"{i}、{name}\n{info}" for i, (name, info) in enumerate(raw_lines, 1)]
            province_summary[prov] = lines
        return province_summary

    SH_result = process(SH_RAW_df, "债券名称/公募REITs名称", "承销商/管理人", "品种",
                        "拟发行金额(亿元)", "项目状态", "更新日期", "省份",
                        amount_fmt=amount_style_sse)
    SZ_result = process(SZ_RAW_df, "项目名称", "承销商/管理人", "债券类别",
                        "申请规模(亿元)", "项目进度", "更新日期", "省份",
                        amount_fmt=amount_style_szse)
    BJ_result = process(BJ_RAW_df, "项目名称", "承销商", "债券类型",
                        "计划发行金额(亿元)", "办理状态", "更新日期", "省份",
                        amount_fmt="raw")

    all_provinces = sorted(
        set(SH_RAW_df["省份"].dropna().unique())
        | set(SZ_RAW_df["省份"].dropna().unique())
        | set(BJ_RAW_df["省份"].dropna().unique())
    )

    def format_exchange_section(title, result_dict):
        lines = [f"{title}："]
        if not all_provinces:
            lines.append("无")
            return "\n".join(lines)
        for prov in all_provinces:
            lines.append(f"{prov}：")
            if prov not in result_dict:
                lines.append("无")
            else:
                lines.extend(result_dict[prov])
        return "\n".join(lines)

    return "\n\n".join([
        format_exchange_section("上交所", SH_result),
        format_exchange_section("深交所", SZ_result),
        format_exchange_section("北交所", BJ_result),
    ])


def extract_ent_bond_section(final_text):
    """板块四：企业债检查（三所文本中是否出现“企业债”）。"""
    ent_dict = {"上交所": "无", "深交所": "无", "北交所": "无"}
    pattern = r"(上交所：|深交所：|北交所：)"
    splits = re.split(pattern, final_text)
    sections = {}
    for i in range(1, len(splits), 2):
        title = splits[i].replace("：", "")
        content = splits[i + 1] if i + 1 < len(splits) else ""
        sections[title] = content
    for ex in ent_dict.keys():
        ent_dict[ex] = "有企业债" if "企业债" in sections.get(ex, "") else "无"
    return "\n\n".join([f"{k}：{v}" for k, v in ent_dict.items()])


def format_nafmii_item(row, idx, name_map=None, fallback_out=None, mapping_audit=None):
    """板块三：交易商协会项目。"""
    project_name = re.sub(r"的注册报告", "", str(row.get("项目名称", "")).strip())
    bond_type = bond_rules.fmt_nafmii_bond_type(row.get("品种"))
    amount = bond_rules.fmt_amount_nafmii(row.get("金额(亿)"))

    underwriter = abbreviate(
        row.get("管理人/主承销商", ""), NAME_MAP if name_map is None else name_map,
        fallback_out=fallback_out, mapping_audit=mapping_audit, delimiter=",",
        audit_context={"section": "nafmii", "project": str(row.get("项目名称", ""))}) or "未知"
    if row.get("_status_confirmation_pending", False):
        project_name = "【状态待确认预览】" + project_name

    status = str(row.get("项目状态", "未知")).strip()
    try:
        date = pd.to_datetime(row["更新日期"]).strftime("%Y-%m-%d")
    except Exception:
        date = "日期未知"
    return (f"{idx}、{project_name}\n"
            f"品种：{bond_type}，金额：{amount}，主承销商：{underwriter}，状态：{status}，更新日期：{date}")


# NAFMII 审核状态流程。公开版流程：同项目同一最新更新日期出现多个状态时，
# 不是按 Excel 行顺序判断，而是取“审核流程中更靠后的状态”。
#
# 普通注册项目主流程：
#   已受理 → 预评中 → 反馈中 → 待上会 → 已上会 → 完成注册
# 定向发行（PPN）主流程：
#   草稿 → 终稿 → 上会 → 核对 → 完成
#
# 为兼容历史 NAFMII 导出中偶见的旧状态词，将两个流程映射到同一阶段分值；
# “完成/完成注册”都视为终态。最新日期遇未知状态时不猜流程、不按行号选择；
# 保留原始候选预览并阻断最终成品，只有明确人工裁决可解除该项目的阻断。
NAFMII_STATUS_STAGE = {
    "已受理": 10,
    "草稿": 10,
    "预评中": 20,
    "反馈中": 30,
    "终稿": 35,
    "待上会": 40,
    "上会": 50,
    "已上会": 50,
    "核对": 60,
    "完成": 70,
    "完成注册": 70,
}


def _pick_nafmii_latest_stage(rows_same):
    """返回 (chosen_row_or_none, audit, warning_or_none)。未知状态绝不自动选行。"""
    rows = rows_same.sort_values("_orig_idx", ascending=True, kind="stable").copy()
    if rows.empty:
        raise ValueError("NAFMII 状态选择候选为空")
    states = [str(x).strip() for x in rows["项目状态"].tolist()]
    ranks = [NAFMII_STATUS_STAGE.get(x) for x in states]
    unknown = sorted({s for s, r in zip(states, ranks) if r is None})
    reference = rows.iloc[0]
    date = (reference["更新日期"].strftime("%Y-%m-%d")
            if pd.notna(reference.get("更新日期")) else "?")
    audit = {"project": str(reference.get("项目名称", "")), "date": date,
             "candidates": sorted(set(states)), "chosen": None,
             "method": "workflow_unknown_blocked" if unknown else "workflow_stage_max",
             "stage": None}
    if unknown:
        warning = {"type": "nafmii_unknown_status_flow", "project": audit["project"],
                   "date": date, "statuses": sorted(set(states)),
                   "candidates": sorted(set(states)), "unknown_statuses": unknown,
                   "severity": "BLOCKED"}
        return None, audit, warning
    max_rank = max(ranks)
    candidate_pos = [i for i, r in enumerate(ranks) if r == max_rank]
    # 同阶段才沿用原来的重复行裁决；已知阶段优先级与原表顺序无关。
    chosen_row = rows.iloc[candidate_pos[-1]]
    chosen = str(chosen_row.get("项目状态", "")).strip()
    audit.update(chosen=chosen, stage=NAFMII_STATUS_STAGE[chosen])
    return chosen_row, audit, None


def build_nafmii_section(df, data_province_list, start_date=None, end_date=None,
                         empty_province_break=True, resolutions=None,
                         force_first_all=False, logger=None, name_map=None,
                         fallback_out=None, mapping_audit=None):
    """板块三：交易商协会项目。
    R9 品种回填：先使用同项目其他行的明确品种；若仍为空且项目已完成注册，
    可从“交易所确认文件号”的标准代码可靠识别（PDFI 通知书按配置口径写 DFI）；
    不得仅由项目名称猜品种。其余保持空（按 R4 显示“/”）并记入 warnings。

    状态口径（公开版流程）：
    1. 每个项目先取最新“更新日期”；
    2. 若该最新日期同一项目存在多条状态，不看 Excel 谁排在后面，而是按审核
       流程取阶段更靠后的状态。普通注册项目核心流程为
       “已受理→预评中→反馈中→待上会→已上会→完成注册”；定向发行核心流程为
       “草稿→终稿→上会→核对→完成”。
    3. 同阶段重复记录才用文件靠后的行作为稳定 tie-breaker。

    resolutions[(project, date)] 只用于未知状态的显式人工选择，值必须精确命中候选。
    force_first_all 为兼容参数，不能绕过未知状态阻断。单条未知状态同样需要确认。
    只有当前地区、当前范围内各项目最新日期参与状态判断；旧日期的未知状态不阻断。
    返回 (文本或明确标注的候选预览, blockers, resolutions_applied, warnings)。
    """
    log_ = logger or log
    resolutions = resolutions or {}
    df = df.copy()
    df["更新日期"] = pd.to_datetime(df["更新日期"], errors="coerce")

    # R9：品种回填（全表做一次，回填规则与省份无关）。完成注册后的确认文件号
    # 属于强证据，可在源“品种”为空时辅助识别；显式源品种始终优先。
    df, type_unresolved = bond_rules.backfill_nafmii_types(df)
    # backfill 在全表执行是为了能利用同项目历史行补品种；但 warning 只能报告
    # 当前 Part / 当前报告范围实际会展示的项目，不能把其他省份的空品种也带进来。
    unresolved_names = {u["project"] for u in type_unresolved}
    warnings_out, warned_unresolved = [], set()

    if start_date:
        df = df[df["更新日期"] >= pd.to_datetime(start_date)]
    if end_date:
        df = df[df["更新日期"] <= pd.to_datetime(end_date)]

    results, blockers, applied = [], [], []
    for province in data_province_list:
        mask = df["省份"].fillna("").apply(lambda x: province in str(x))
        df_sub = df[mask].reset_index(drop=True)
        if df_sub.empty:
            results.append(bond_rules.empty_province_section(province, empty_province_break))
            continue
        df_sub["_orig_idx"] = range(len(df_sub))

        # 先确定每个项目的最新更新日期；只有该日期的状态参与“流程阶段”比较。
        latest_dates = df_sub.groupby("项目名称", dropna=False)["更新日期"].transform("max")
        latest = df_sub[(df_sub["更新日期"] == latest_dates)
                        | (df_sub["更新日期"].isna() & latest_dates.isna())].copy()

        selected_rows = []
        # sort=False 保持项目第一次出现的原始顺序；后续仍会执行可选优先承销商排序。
        for project, rows_same in latest.groupby("项目名称", dropna=False, sort=False):
            chosen_row, audit, warning = _pick_nafmii_latest_stage(rows_same)
            if warning:
                chosen_state = resolutions.get((audit["project"], audit["date"]))
                if chosen_state is not None and chosen_state in audit["candidates"]:
                    exact_rows = rows_same[rows_same["项目状态"].astype(str).str.strip() == chosen_state]
                    chosen_row = exact_rows.sort_values("_orig_idx", kind="stable").iloc[-1]
                    audit.update(chosen=chosen_state, method="manual_status_confirmation",
                                 stage=NAFMII_STATUS_STAGE.get(chosen_state))
                    audit["unknown_statuses"] = warning["unknown_statuses"]
                    selected_rows.append(chosen_row)
                    applied.append(audit)
                    log_.info("交易商协会未知状态已人工确认 %s（%s）：%s",
                              project, audit["date"], chosen_state)
                    continue
                blocker = dict(warning)
                if chosen_state is not None:
                    blocker["invalid_resolution"] = str(chosen_state)
                blockers.append(blocker)
                warnings_out.append(warning)
                applied.append(audit)
                # 不能假选一条业务状态。预览逐条保留原候选字段，醒目标记且由 blocker 禁止最终版。
                # 用完整字段值排序让交换源行顺序不改变候选预览；_orig_idx 仅保留后续组内排序用途。
                pending = rows_same.copy()
                sort_columns = [c for c in pending.columns if c != "_orig_idx"]
                pending["_preview_sort"] = pending[sort_columns].astype(str).agg("\x1f".join, axis=1)
                pending = pending.sort_values("_preview_sort", kind="stable")
                anchor = int(rows_same["_orig_idx"].min())
                for position, (_, raw_row) in enumerate(pending.iterrows()):
                    preview_row = raw_row.drop(labels=["_preview_sort"]).copy()
                    preview_row["_status_confirmation_pending"] = True
                    preview_row["_orig_idx"] = anchor + position / max(len(pending), 1)
                    selected_rows.append(preview_row)
                log_.warning("交易商协会未知状态已阻断最终版 %s（%s）：%s；预览保留全部原状态",
                             project, audit["date"], " / ".join(audit["candidates"]))
                continue
            selected_rows.append(chosen_row)
            if len(rows_same) > 1:
                applied.append(audit)
                log_.info("交易商协会同日多状态 %s（%s：%s）：按流程阶段采用 %s",
                          project, audit["date"], " → ".join(audit["candidates"]), audit["chosen"])

        sel = pd.DataFrame(selected_rows).copy()
        if "_status_confirmation_pending" in sel.columns:
            sel["_status_confirmation_pending"] = sel["_status_confirmation_pending"].fillna(False)

        # 可选优先承销商排序；其余保持原顺序。
        sel = sel.assign(_priority=sel["管理人/主承销商"].astype(str).map(_is_priority_underwriter))
        sel = sel.sort_values(["_priority", "_orig_idx"], ascending=[False, True],
                              kind="stable").reset_index(drop=True)
        for name in sel["项目名称"].dropna().astype(str).unique():
            if name in unresolved_names and name not in warned_unresolved:
                warned_unresolved.add(name)
                warnings_out.append({"type": "nafmii_type_unresolved", "project": name})
                log_.warning("交易商协会品种仍为空，按源数据展示“/”：%s", name[:40])
        lines = [format_nafmii_item(row, i + 1, name_map=name_map,
                                    fallback_out=fallback_out, mapping_audit=mapping_audit)
                 for i, row in sel.iterrows()]
        results.append(f"{province}：\n" + "\n".join(lines))

    # R5：段间空行（“无”段后不加空行）
    return bond_rules.join_province_sections(results), blockers, applied, warnings_out


# ---------------------------------------------------------------- 简称表加载

NAME_MAP = {}   # 承销商全称 → 简称（运行时加载）


def load_name_map(map_path, logger=None):
    """三栏共用已确认的 full→short 表，并派生无冲突的精确法人名称词干别名。

    返回原始映射 dict 的兼容子类（原条数不含别名）及冲突列表。未知输入保持原文；
    别名只从表中已存在的机构生成，逐次调用会保留来源，不改写 input/简称.xlsx。
    """
    global NAME_MAP

    def _clean(v):
        if pd.isna(v):
            return ""
        text = str(v).strip()
        return "" if text.lower() in ("nan", "none") else text

    map_df = pd.read_excel(map_path)
    pairs = [(_clean(f), _clean(s)) for f, s in zip(map_df["full"], map_df["short"])
             if _clean(f) and _clean(s)]
    NAME_MAP = _ExactNameMap(pairs)
    conflicts = NAME_MAP.conflicts
    if logger is not None:
        for conflict in conflicts:
            logger.error("简称表冲突，冲突别名不自动映射：%s", conflict)
    return NAME_MAP, conflicts


def main():
    """停用旧直接生成入口，避免绕过统一引擎的关键业务检查。"""
    root = os.path.dirname(os.path.dirname(APP_DIR))
    raise SystemExit(
        "main_mac.py 的旧独立生成入口已停用，未读取数据或生成周报。"
        f"请使用 {os.path.join(root, 'weekly.py')}，"
        f"或双击 {os.path.join(root, '跑周报.command')} / 启动Web.command。"
        "旧 --provinces/--datadir 参数不再由本入口执行；"
        "使用 python3 weekly.py --help 查看当前参数。")


if __name__ == "__main__":
    main()
