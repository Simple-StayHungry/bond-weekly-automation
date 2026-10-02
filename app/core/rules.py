#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
业务规则模块
=============
周报生成的通用业务规则集中在此。生产数据与真实模板不随公开版发布。
修改任何一条规则前先跑测试，防止影响其他 Part。

规则清单：
  R1 上交所金额格式：整数补“.0”（95→95.0），小数原样（23.28→23.28）
  R2 深交所/北交所金额：保留接口原值（20.00）
  R3 公司债板块排除 ABS 与公募 REITs
  R4 交易商协会金额保留原始精度；品种空值在无法由可靠注册结果识别时显示“/”
  R5 交易商协会空省写“省份：\n无”，且“无”段后不加空行
  R6 承销商简称：简称表/特殊映射优先；未命中且严格以“股份有限公司”
     结尾时仅对承销商显示做安全截尾，其余未命中保留原文并提醒；
     表内冲突仍 BLOCK，列表去重去空段，原名和映射来源可审计
  R7 高亮规则：主承销商含配置关键字的债券条目整条标红加粗
  R8 债项评级：数据源“债券评级”列有值即列示
  R9 交易商协会品种回填：优先用同项目其他行的明确品种；若仍为空且项目已
     完成注册，可从“交易所确认文件号”中的标准注册代码可靠识别。不得仅凭
     项目名称猜测品种。其余保持空并列入 unresolved 供上层 warning。
"""

import logging
import re

import pandas as pd


# ---------------------------------------------------------------- R1/R2 金额格式

def fmt_amount_sse(v):
    """R1：上交所金额。整数补“.0”，小数原样。"""
    try:
        f = float(v)
        if f == int(f):
            return f"{f:.1f}"
        return str(v).strip()
    except (TypeError, ValueError):
        return v


def fmt_amount_raw(v):
    """R2：深交所/北交所金额。保留原值字符串。"""
    return v


def fmt_amount_nafmii(v):
    """R4：交易商协会金额。

    空值仍显示“nan亿元”（兼容历史数据）；非空值保留数据源实际精度，
    例如 16.25→16.25亿元、14.8→14.8亿元、13.4→13.4亿元，整数不补 .0。
    不能再使用 ``:.0f``，否则会把 16.25/14.8 等金额错误四舍五入。
    """
    if pd.isna(v):
        return "nan亿元"
    try:
        # 用固定小数格式后去尾零，避免 float 的科学计数法和二进制尾差。
        f = float(v)
        s = f"{f:.12f}".rstrip("0").rstrip(".")
        return f"{s}亿元"
    except (TypeError, ValueError):
        return "nan亿元"


def fmt_nafmii_bond_type(v):
    """R4：交易商协会品种。空值显示“/”，不自行猜测品种。"""
    if pd.isna(v) or str(v).strip() == "" or str(v).strip().lower() in ("nan", "none"):
        return "/"
    return str(v).strip()


# ---------------------------------------------------------------- R3 排除规则

_EXCLUDE_TYPE_RE = re.compile(r"ABS|REIT", re.IGNORECASE)


def exclude_non_company(df, type_col):
    """R3：公司债板块剔除品种含 ABS 或 REIT 的项目。"""
    return df[~df[type_col].astype(str).str.contains(_EXCLUDE_TYPE_RE, na=False)]


# ---------------------------------------------------------------- R5 段落拼接

def join_province_sections(results):
    """R5：省份段拼接。“省份：\n无”段后不加空行，有内容段后一个空行。"""
    out_parts = []
    for i, seg in enumerate(results):
        if i > 0:
            sep = "\n" if results[i - 1].rstrip().endswith("无") else "\n\n"
            out_parts.append(sep + seg)
        else:
            out_parts.append(seg)
    return "".join(out_parts)


def empty_province_section(province, break_line=True):
    """R5：空省段落。break_line=True 时“省份：\n无”（西北口径），
    False 时“省份：无”（旧 V4 口径）。"""
    return f"{province}：\n无" if break_line else f"{province}：无"


# ---------------------------------------------------------------- R6 承销商简称

class ExactNameMap(dict):
    """原始 full→short 表及其可追溯的精确别名；不泛化改写输入机构名。"""

    def __init__(self, pairs):
        targets = {}
        original_targets = {}
        reverse = {}
        for full, short in pairs:
            full, short = str(full).strip(), str(short).strip()
            if not full or not short:
                continue
            original_targets.setdefault(full, set()).add(short)
            reverse.setdefault(short, set()).add(full)
            stem = re.sub(r"(股份有限公司|有限责任公司|有限公司)$", "", full)
            for alias, rule in [(full, "table_full"), (short, "table_canonical"),
                                (stem, "table_full_company_stem")]:
                targets.setdefault(alias, {}).setdefault(short, []).append(
                    {"source_full": full, "alias_rule": rule})
        super().__init__({full: next(iter(shorts)) for full, shorts in original_targets.items()
                          if len(shorts) == 1})
        self.lookup, self.origins, self.conflicts = {}, {}, []
        for alias, options in targets.items():
            if len(options) != 1:
                self.conflicts.append({
                    "type": "underwriter_alias_conflict", "alias": alias, "short": alias,
                    "fulls": sorted({o["source_full"] for os_ in options.values() for o in os_}),
                    "candidates": sorted(options),
                })
                continue  # 模糊别名不进入查找表；展示原文并报告未命中。
            short, origins = next(iter(options.items()))
            self.lookup[alias] = short
            priority = {"table_full": 0, "table_canonical": 1, "table_full_company_stem": 2}
            self.origins[alias] = sorted(origins, key=lambda o: (
                priority[o["alias_rule"]], o["source_full"]))[0]
        # 多个法人共用同一简称时公开版一律视为冲突，交由用户明确配置。
        for short, fulls in reverse.items():
            fulls_set = set(fulls)
            if len(fulls_set) > 1:
                self.conflicts.append({"type": "underwriter_shared_short", "short": short,
                                       "fulls": sorted(fulls_set)})


def abbreviate(raw_text, name_map, fallback_out=None, mapping_audit=None,
               delimiter="，", audit_context=None, split_whitespace=False):
    """承销商简称：简称表优先；未命中时仅兜底删除末尾“股份有限公司”。

    “有限责任公司/有限公司”不自动截尾。该函数只用于承销商/管理人显示，
    发行人、NAFMII 项目主体等正式名称不经过此处。原名、映射来源和规则均可审计。
    """
    if pd.isna(raw_text) or not str(raw_text).strip():
        return ""
    exact = name_map if isinstance(name_map, ExactNameMap) else ExactNameMap((name_map or {}).items())
    out, seen = [], set()
    # 交易所接口沿用已有空白分隔口径；人工源表只用明确标点分隔机构名称。
    separator = r"[\s、,，;；]+" if split_whitespace else r"[、,，;；\r\n]+"
    for word in re.split(separator, str(raw_text)):
        word = word.strip()
        if not word or word.lower() in ("nan", "none"):
            continue
        matched = word in exact.lookup
        conflicted_alias = any(c.get("type") == "underwriter_alias_conflict" and c.get("alias") == word
                               for c in exact.conflicts)
        if matched:
            short = exact.lookup[word]
            origin = exact.origins.get(word, {"source_full": None, "alias_rule": "table_match"})
        elif conflicted_alias:
            # 简称表本身出现冲突时不能用兜底规则掩盖问题；保留原文，交由既有 BLOCK 机制处理。
            short = word
            origin = {"source_full": None, "alias_rule": "ambiguous_table_alias_preserved"}
            if fallback_out is not None and word not in fallback_out:
                fallback_out.append(word)
            logging.getLogger("bond").warning("承销商简称表存在冲突，保留原文：%s", word)
        elif word.endswith("股份有限公司"):
            short = re.sub(r"股份有限公司$", "", word).strip()
            origin = {"source_full": None, "alias_rule": "fallback_strip_joint_stock_suffix"}
            logging.getLogger("bond").info("承销商简称表未命中，按兜底规则去除‘股份有限公司’：%s -> %s", word, short)
        else:
            short = word
            origin = {"source_full": None, "alias_rule": "unmapped_preserved"}
            if fallback_out is not None and word not in fallback_out:
                fallback_out.append(word)
            logging.getLogger("bond").warning("承销商简称表未命中，保留原文：%s", word)
        if mapping_audit is not None:
            mapping_audit.append({
                **(audit_context or {}), "raw_text": str(raw_text), "raw": word,
                "canonical": short, "matched": matched, **origin,
            })
        if short and short not in seen:
            seen.add(short)
            out.append(short)
    return delimiter.join(out)



# ---------------------------------------------------------------- R8 债项评级

def fmt_debt_rating(v):
    """R8：债项评级。数据源“债券评级”列有值即返回清洗后的字符串（配置口径：
    有值就列示，与主体评级是否相同无关），空值返回 ""。"""
    if pd.isna(v) or str(v).strip() == "" or str(v).strip().lower() in ("nan", "none"):
        return ""
    return str(v).strip()


# ---------------------------------------------------------------- R9 品种回填

# 确认文件号到展示品种的映射。公开版默认留空，避免固化任何机构的内部口径。
# 如你的业务口径已经验证，可在这里显式增加，例如 {"CODE": "TYPE"}。
_NAFMII_CONFIRM_CODE_MAP = {}


def infer_nafmii_type_from_confirmation(confirm_no):
    """从交易商协会完成注册后的确认文件号识别品种。

    仅使用确认文件号中的标准英文代码，不读取项目名称，避免主观猜测。
    公开版默认不预置任何代码映射。只有在 ``_NAFMII_CONFIRM_CODE_MAP`` 中显式配置的代码才会自动识别；其他代码返回空字符串。
    """
    if pd.isna(confirm_no) or str(confirm_no).strip() == "":
        return ""
    text = str(confirm_no).strip().upper()
    # 常见格式：中市协注[2026]PDFI51号。优先抓年份方括号后的代码；
    # 兼容少量无方括号的历史导出时，再退回抓连续大写字母。
    m = re.search(r"\]([A-Z]+)\d*", text)
    if not m:
        m = re.search(r"(?:^|[^A-Z])([A-Z]{2,6})\d+", text)
    if not m:
        return ""
    return _NAFMII_CONFIRM_CODE_MAP.get(m.group(1), "")


def backfill_nafmii_types(df, name_col="项目名称", type_col="品种",
                          status_col="项目状态", confirm_col="交易所确认文件号"):
    """R9：交易商协会品种回填。返回 (df, unresolved)。
      1) 同项目其他行有明确品种 → 用明确品种回填；
      2) 仍为空时，仅对“完成注册/完成”的行，根据交易所确认文件号中的标准注册
         代码识别；仅使用用户显式配置的确认文件号映射；
      3) 不根据项目名称推断 PDFI/MTN/PPN；
      4) 其余保持空，项目名记入 unresolved（最终展示“/”，并供上层 warning）。

    显式源值优先级最高：即使确认文件号为 PDFI，只要源表品种已有 MTN 等明确值，
    就绝不覆盖。
    unresolved: [{"project": str}]
    """
    df = df.copy()
    unresolved = []
    if name_col not in df.columns or type_col not in df.columns:
        return df, unresolved

    def _is_empty(v):
        return pd.isna(v) or str(v).strip() == "" or str(v).strip().lower() in ("nan", "none")

    # 1) 同项目明确品种回填。
    filled = df.groupby(name_col)[type_col].transform(
        lambda s: s.where(~s.map(_is_empty)).ffill().bfill()
        if (~s.map(_is_empty)).any() else s)
    still_empty = df[type_col].map(_is_empty)
    df.loc[still_empty, type_col] = filled[still_empty]

    # 2) 若仍为空，完成注册后的确认文件号属于强证据，可可靠识别标准品种。
    if confirm_col in df.columns:
        for idx in df.index[df[type_col].map(_is_empty)]:
            status = str(df.at[idx, status_col]).strip() if status_col in df.columns else ""
            if status not in ("完成注册", "完成"):
                continue
            inferred = infer_nafmii_type_from_confirmation(df.at[idx, confirm_col])
            if inferred:
                df.at[idx, type_col] = inferred

    # 确认文件号识别出的品种属于项目级强证据；识别后再做一次同项目回填，
    # 这样同一项目早期“已上会”等空品种行也同步得到一致品种，且不会产生虚假 unresolved warning。
    if (~df[type_col].map(_is_empty)).any():
        filled_after_confirm = df.groupby(name_col)[type_col].transform(
            lambda s: s.where(~s.map(_is_empty)).ffill().bfill()
            if (~s.map(_is_empty)).any() else s)
        still_empty = df[type_col].map(_is_empty)
        df.loc[still_empty, type_col] = filled_after_confirm[still_empty]

    # 3) 剩余未解决：严禁仅凭项目名称猜品种。
    for name in df.loc[df[type_col].map(_is_empty), name_col].dropna().unique():
        unresolved.append({"project": str(name)})
    return df, unresolved


# ---------------------------------------------------------------- R7 高亮

_ENTRY_RE = re.compile(r"^\d+、[^\n]*\n[^\n]*", re.M)


def match_highlight(info_line, highlight_rules):
    """R7：信息行匹配高亮规则。返回命中的字体配置 dict（或 None）。
    highlight_rules: [{"field": "underwriter", "contains": "配置的重点机构",
                       "font": {"bold": true, "color": "FFFF0000"}}]
    当前 field 仅支持 underwriter（= 债券条目信息行）。"""
    for rule in highlight_rules or []:
        if rule.get("field", "underwriter") != "underwriter":
            continue
        if rule.get("contains") and rule["contains"] in info_line:
            return rule.get("font", {"bold": True, "color": "FFFF0000"})
    return None


def split_highlight_entries(text, highlight_rules):
    """R7：把文本按债券条目切分，命中规则的条目整条标出。
    返回 [{"text", "font"}]，font 为 None 表示普通段。"""
    segs = []
    pos = 0
    for m in _ENTRY_RE.finditer(text):
        start, end = m.start(), m.end()
        entry = m.group(0)
        info_line = entry.split("\n", 1)[1] if "\n" in entry else entry
        font = match_highlight(info_line, highlight_rules)
        if font is not None:
            if start > pos:
                segs.append({"text": text[pos:start], "font": None})
            segs.append({"text": entry, "font": font})
            pos = end
    if pos < len(text):
        segs.append({"text": text[pos:], "font": None})
    return segs
