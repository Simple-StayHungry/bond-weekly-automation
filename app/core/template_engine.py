#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模板保留引擎（Template-preserving Excel Engine）
=================================================
原则：
  1. 只在上周周报主 sheet 的顶部插入本周 3 行（标题 / 表头 / 内容），历史区域不触碰；
  2. 新块样式 = 克隆上周块的 XML（与人工"复制上一周再改文字"完全一致），
     字体、边框、对齐、行高、列宽全部继承；
  3. 生成后强制 diff 校验：历史区域任何差异 → FAIL，不产出文件；
  4. 富文本：主承销商含高亮关键字（如配置的重点机构）的债券条目，整条标红加粗。

实现方式：直接操作 xlsx 的 OOXML（字符串级变换 + 追加 sharedStrings），
不使用 openpyxl 重写（openpyxl 会丢失它不认识的 XML 细节）。
"""

import math
import os
import re
import shutil
import zipfile

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

# 单元格列宽（半角字符单位），用于估算内容行高；来自模板的 <cols>
DEFAULT_COL_WIDTHS = {"A": 57.66, "B": 49.0, "C": 50.66, "D": 53.66}


class TemplateError(Exception):
    pass


class RichText:
    """富文本条目：runs = [{"text","font","size","color","bold"}, ...]"""

    def __init__(self, runs):
        self.runs = runs

    def to_xml(self):
        parts = ["<si>"]
        for r in self.runs:
            rpr = (f'<sz val="{r.get("size", 11)}"/>'
                   f'<rFont val="{r.get("font", "楷体")}"/>'
                   f'<charset val="134"/>')
            if r.get("color"):
                rpr += f'<color rgb="{r["color"]}"/>'
            if r.get("bold"):
                rpr += "<b/>"
            parts.append(
                f'<r><rPr>{rpr}</rPr><t xml:space="preserve">{_escape_xml(r["text"])}</t></r>')
        parts.append("</si>")
        return "".join(parts)


def _escape_xml(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "&#10;")


# ---------------------------------------------------------------- 字体交替与高亮
# 模板规律（实测历史周报）：
#   连续中文（含全角标点、换行）→ run 用楷体；连续 ASCII（数字/字母/半角符号）→ run 用 Times New Roman。
#   标题行所有 run 加粗（22 号），内容行 11 号；高亮条目额外红色 + 加粗。

def _char_class(ch):
    if ch == "\n":
        return "zh"
    o = ord(ch)
    if 0x4E00 <= o <= 0x9FFF or 0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFFEF:
        return "zh"
    return "en"


def font_runs(text, size=11, bold=False, color=None):
    """按中/西文切分为字体交替的 run 列表。"""
    runs, buf, cur = [], [], None

    def flush():
        if buf:
            runs.append({
                "text": "".join(buf),
                "font": "楷体" if cur == "zh" else "Times New Roman",
                "size": size, "bold": bold, "color": color,
            })
            buf.clear()

    for ch in text:
        c = _char_class(ch)
        if c != cur:
            flush()
            cur = c
        buf.append(ch)
    flush()
    return runs


_ENTRY_RE = re.compile(r"^\d+、[^\n]*\n[^\n]*", re.M)


def build_rich_text(text, size=11, highlight_rules=None):
    """生成与模板一致的富文本（中/西文字体交替 + 高亮规则）。
    highlight_rules: 见 rules.split_highlight_entries（如主承销商含“配置的重点机构”
                     的债券条目整条按 font 标红加粗）。"""
    import rules as bond_rules
    runs = []
    for seg in bond_rules.split_highlight_entries(text, highlight_rules):
        f = seg.get("font") or {}
        runs.extend(font_runs(seg["text"], size=size,
                              bold=bool(f.get("bold")),
                              color=f.get("color")))
    return RichText(runs)


# ---------------------------------------------------------------- zip 工具

def read_zip(path):
    with zipfile.ZipFile(path) as z:
        return {name: z.read(name) for name in z.namelist()}


def write_zip(path, entries):
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            z.writestr(name, data)
    shutil.move(tmp, path)


# ---------------------------------------------------------------- 主 sheet 定位

def find_main_sheet(entries):
    """在 workbook.xml + rels 中定位第一个 sheet 的名字与 xml 文件路径。"""
    wb = entries["xl/workbook.xml"].decode("utf-8")
    m = re.search(r'<sheet[^>]*name="([^"]+)"[^>]*r:id="(rId\d+)"', wb)
    if not m:
        raise TemplateError("无法从 workbook.xml 解析第一个 sheet")
    sheet_name, rid = m.group(1), m.group(2)
    rels = entries["xl/_rels/workbook.xml.rels"].decode("utf-8")
    m2 = re.search(r'<Relationship[^>]*Id="%s"[^>]*Target="([^"]+)"' % rid, rels)
    if not m2:
        raise TemplateError(f"workbook.xml.rels 中找不到 {rid}")
    target = m2.group(1).lstrip("/")
    if not target.startswith("xl/"):
        target = "xl/" + target
    return sheet_name, target


# ---------------------------------------------------------------- 行号变换（字符串级）

_ROW_R_RE = re.compile(r'(<row r=")(\d+)(")')
_CELL_REF_RE = re.compile(r'(<c r=")([A-Z]{1,3})(\d+)(")')
_MERGE_RE = re.compile(r'(<mergeCell ref=")([A-Z]{1,3})(\d+):([A-Z]{1,3})(\d+)(")')
_DIM_RE = re.compile(r'(<dimension ref=")([A-Z]{1,3})(\d+):([A-Z]{1,3})(\d+)(")')
_MERGE_COUNT_RE = re.compile(r'(<mergeCells[^>]*count=")(\d+)(")')

# 行切分：兼容普通行 <row>…</row> 与自闭合空行 <row …/>（模板里两种都有）
_ROW_SPLIT_RE = re.compile(r"<row\b[^>]*/>|<row\b.*?</row>", re.S)

# sharedStrings 条目切分：兼容 <si>…</si> 与自闭合 <si …/>
_SI_SPLIT_RE = re.compile(r"<si>.*?</si>|<si[^>]*/>", re.S)


def _shift(xml, n):
    """历史区域整体下移 n 行：row r 属性、单元格引用、mergeCell 引用都 +n；
    dimension 只动结束行（新增块插在顶部，起点仍是第 1 行）。"""
    xml = _ROW_R_RE.sub(lambda m: f'{m.group(1)}{int(m.group(2)) + n}{m.group(3)}', xml)
    xml = _CELL_REF_RE.sub(
        lambda m: f'{m.group(1)}{m.group(2)}{int(m.group(3)) + n}{m.group(4)}', xml)
    xml = _MERGE_RE.sub(
        lambda m: f'{m.group(1)}{m.group(2)}{int(m.group(3)) + n}:{m.group(4)}{int(m.group(5)) + n}{m.group(6)}',
        xml)
    xml = _DIM_RE.sub(
        lambda m: f'{m.group(1)}{m.group(2)}{m.group(3)}:{m.group(4)}{int(m.group(5)) + n}{m.group(6)}',
        xml)
    return xml


def _add_merge_cell(xml, ref="A1:D1"):
    """追加一条 mergeCell 并同步 mergeCells 的 count 属性（若模板带 count）。"""
    xml = _MERGE_COUNT_RE.sub(
        lambda m: f'{m.group(1)}{int(m.group(2)) + 1}{m.group(3)}', xml, count=1)
    return xml.replace("</mergeCells>", f'<mergeCell ref="{ref}"/></mergeCells>', 1)


# ---------------------------------------------------------------- sharedStrings


def append_shared_strings(entries, strings, extra_refs=0):
    """strings: 纯文本 str 或 RichText。返回这些新条目的起始索引列表。

    注意：
    - 起始索引必须按实际 si 元素个数计算（模板的 count 属性可能因历史编辑
      而与实际不符），否则新引用会越界；
    - count 是“si 被引用的总次数”：新块中复用旧 si 的表头等引用
      （extra_refs）也必须计入；uniqueCount 只按新 si 数量增加。"""
    xml = entries["xl/sharedStrings.xml"].decode("utf-8")
    start = len(re.findall(r"<si[ >]", xml))
    new_sis = []
    for s in strings:
        if isinstance(s, RichText):
            new_sis.append(s.to_xml())
        else:
            new_sis.append(f'<si><t xml:space="preserve">{_escape_xml(s)}</t></si>')
    if new_sis:
        insert_at = xml.rindex("</sst>")
        xml = xml[:insert_at] + "".join(new_sis) + xml[insert_at:]
        xml = re.sub(r'count="(\d+)"',
                     lambda mm: f'count="{int(mm.group(1)) + len(new_sis) + extra_refs}"',
                     xml, count=1)
        xml = re.sub(r'uniqueCount="(\d+)"',
                     lambda mm: f'uniqueCount="{int(mm.group(1)) + len(new_sis)}"', xml, count=1)
        entries["xl/sharedStrings.xml"] = xml.encode("utf-8")
    return list(range(start, start + len(new_sis)))


# ---------------------------------------------------------------- 行高估算

def estimate_content_height(texts_by_col, col_widths=None, base=409.5,
                            line_height=14.25, padding=6.0):
    """按各列文本长度估算内容行所需行高（wrap_text 显示）。"""
    widths = col_widths or DEFAULT_COL_WIDTHS
    max_lines = 1
    for col, text in texts_by_col.items():
        w = float(widths.get(col, 50)) - 2
        lines = 0
        for ln in text.split("\n"):
            units = sum(2 if ord(ch) > 127 else 1 for ch in ln)
            lines += max(1, math.ceil(units / max(w, 1)))
        max_lines = max(max_lines, lines)
    est = max_lines * line_height + padding
    return max(base, math.ceil(est * 2) / 2)


def content_overflows(texts_by_col, col_widths, actual_height, font_size=11):
    """Identify cells requiring visual review without changing teacher geometry.

    This is a conservative text-wrap estimate, not an Office rendering verdict.
    """
    overflows = []
    line_height = float(font_size) * (14.25 / 11)
    for column, value in texts_by_col.items():
        text = value or ""
        if not text.strip():
            continue
        needed = estimate_content_height(
            {column: text}, col_widths=col_widths, base=0,
            line_height=line_height)
        if needed > float(actual_height) + 0.1:
            overflows.append({
                "cell": f"{column}3", "explicit_lines": len(text.split("\n")),
                "estimated_height_pt": needed, "actual_height_pt": actual_height,
                "font_size_pt": font_size, "column_width": col_widths.get(column),
                "requires_visual_review": True,
            })
    return overflows


def add_content_height_margin(estimated, base, extra_lines=0, line_height=14.25):
    """对已经需要扩行的内容行增加来源模板习惯的底部余量。

    ``extra_lines`` 以 11 号正文的一行高度计，只在 ``estimated > base`` 时生效；
    若上一期模板本身已经足够高，则完全继承原行高。返回值按 Excel 常见的 0.5pt
    粒度向上取整。
    """
    extra = float(extra_lines or 0)
    if extra <= 0 or estimated <= base + 0.01:
        return estimated
    return math.ceil((estimated + extra * line_height) * 2) / 2


def cap_content_height(height, max_height=None):
    """限制本周内容行的最终行高。

    既有模板 回传的广东/陕西成品把长文本行统一收敛到 409.5pt，
    西北四省为 409.6pt；此前程序生成的 690/918/1217.5 属于过高。
    ``max_height`` 为空时不限制；有值时即使模板上一周本身更高，也按该上限收敛。
    """
    if max_height is None or str(max_height).strip() == "":
        return height
    try:
        cap = float(max_height)
    except (TypeError, ValueError):
        return height
    return min(float(height), cap)


# ---------------------------------------------------------------- 主流程：插入本周块

def build_weekly_report(template_path, out_path, block, col_widths=None):
    """在模板顶部插入本周块，另存为 out_path。

    block: dict {
        "title": str,                      # A1 标题
        "a3": str|RichText,                # 一周回顾
        "b3": str|RichText,                # 公司债券项目情况
        "c3": str|RichText,                # 交易商协会项目情况
        "d3": str,                         # 企业债券项目情况
        "height": float|None,              # 内容行行高（None=继承模板）
    }
    返回 (sheet_name, 诊断信息)。
    """
    entries = read_zip(template_path)
    sheet_name, sheet_file = find_main_sheet(entries)
    sheet_xml = entries[sheet_file].decode("utf-8")

    # ---- 1. 追加 shared strings，拿到新索引 ----
    strings = [block["title"], block["a3"], block["b3"], block["c3"]]
    if block.get("d3") is not None:
        strings.append(block["d3"])

    # ---- 2. 拆出 sheetData 的 row（兼容自闭合空行 <row …/>）----
    m = re.search(r"<sheetData>.*</sheetData>", sheet_xml, re.S)
    if not m:
        raise TemplateError("主 sheet 中找不到 sheetData")
    sd = m.group(0)
    rows = _ROW_SPLIT_RE.findall(sd)
    if len(rows) < 3:
        raise TemplateError("主 sheet 的行数不足 3，无法克隆本周块")

    # ---- 3. 克隆旧前 3 行作为新块，替换文本 ----
    new_rows = [rows[0], rows[1], rows[2]]

    def set_cell_v(row_xml, cell_ref, value):
        """精确替换指定单元格的 <v> 值（避免误伤其他单元格）。"""
        pattern = re.compile(r'(<c r="' + cell_ref + r'"[^>]*>)<v>\d+</v>')
        if not pattern.search(row_xml):
            raise TemplateError(f"克隆行中找不到单元格 {cell_ref}")
        return pattern.sub(lambda m: m.group(1) + f"<v>{value}</v>", row_xml, count=1)

    if block.get("height"):
        new_rows[2] = re.sub(r'ht="[\d.]+"', f'ht="{block["height"]}"', new_rows[2], count=1)

    # 新块的全部 shared-string 引用数：count 必须同步（含复用旧 si 的表头）
    total_v_refs = sum(len(re.findall(r"<v>\d+</v>", row)) for row in new_rows)
    extra_refs = total_v_refs - len(strings)
    idx = append_shared_strings(entries, strings, extra_refs=extra_refs)
    si_title, si_a3, si_b3, si_c3 = idx[0], idx[1], idx[2], idx[3]
    si_d3 = idx[4] if len(idx) > 4 else None
    # 拿到索引后再替换文本
    new_rows[0] = set_cell_v(new_rows[0], "A1", si_title)
    new_rows[2] = set_cell_v(new_rows[2], "A3", si_a3)
    new_rows[2] = set_cell_v(new_rows[2], "B3", si_b3)
    new_rows[2] = set_cell_v(new_rows[2], "C3", si_c3)
    if si_d3 is not None:
        new_rows[2] = set_cell_v(new_rows[2], "D3", si_d3)

    # ---- 4. 历史行整体下移 3 行 ----
    shifted_rows = [_shift(r, 3) for r in rows]

    # ---- 5. 重组 sheet XML：新块保持行号 1-3，只有历史部分和前后缀 +3 ----
    new_sd = "<sheetData>" + "".join(new_rows + shifted_rows) + "</sheetData>"
    prefix = _shift(sheet_xml[: m.start()], 3)
    suffix = _shift(sheet_xml[m.end():], 3)
    # 新块标题行合并 A1:D1（与历史块一致），同步 mergeCells count
    suffix = _add_merge_cell(suffix, "A1:D1")
    sheet_xml = prefix + new_sd + suffix

    entries[sheet_file] = sheet_xml.encode("utf-8")

    # ---- 6. 先写临时文件 → 校验通过 → 原子改名到正式路径 ----
    tmp_path = out_path + ".tmp"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    write_zip(tmp_path, entries)
    try:
        diag = verify(template_path, tmp_path, sheet_file=sheet_file, n_new_si=len(strings),
                      extra_refs=extra_refs, shift=3, new_block_rows=3)
    except Exception:
        if os.path.exists(tmp_path):      # QA 失败：清理临时文件，正式路径绝不出现
            os.remove(tmp_path)
        raise
    os.replace(tmp_path, out_path)
    return sheet_name, diag


# ---------------------------------------------------------------- diff 校验

def verify(template_path, out_path, sheet_file, n_new_si, extra_refs=0, shift=3, new_block_rows=3):
    """生成后双层校验：
    【第 1 层 · ZIP entry 白名单】除主 sheet 与 sharedStrings 外，
       styles/theme/workbook/printSettings/definedNames/其他 sheet 等所有条目
       必须与原模板逐字节一致（hash 白名单）；
    【第 2 层 · 主 sheet 节点白名单】主 sheet 内只允许：
      - sheetData 前新增 3 行（新块，克隆样式；自闭合空行也计入）
      - 历史行号整体 +3（内容字节不变）
      - dimension 结束行 +3（起点仍是 1）
      - mergeCells 引用 +3、count +1、仅追加 A1:D1 一条
      sharedStrings 仅允许末尾追加 n 个新 si，count 增量 = n + 复用引用数。
    任何越界变化抛 TemplateError，不产出文件。"""
    t = read_zip(template_path)
    o = read_zip(out_path)
    allowed_entries = {sheet_file, "xl/sharedStrings.xml"}
    qa = {"entries_total": len(t), "entries_unchanged": 0, "allowed_changed": sorted(allowed_entries)}

    # 1) ZIP entry 白名单
    for name in t:
        if name in allowed_entries:
            continue
        if t[name] != o.get(name):
            raise TemplateError(f"diff 失败：条目 {name} 不在白名单内却被修改")
        qa["entries_unchanged"] += 1
    for name in allowed_entries:
        if name not in o:
            raise TemplateError(f"diff 失败：输出缺少白名单条目 {name}")

    # 2) 主 sheet 历史行与前后缀
    def sheet_parts(entries):
        xml = entries[sheet_file].decode("utf-8")
        m = re.search(r"<sheetData>.*</sheetData>", xml, re.S)
        return xml[: m.start()], _ROW_SPLIT_RE.findall(m.group(0)), xml[m.end():]

    t_pre, t_rows, t_suf = sheet_parts(t)
    o_pre, o_rows, o_suf = sheet_parts(o)
    if len(o_rows) != len(t_rows) + new_block_rows:
        raise TemplateError(f"diff 失败：行数 {len(t_rows)} → {len(o_rows)} 与预期不符")
    for i, old_row in enumerate(t_rows):
        expected = _shift(old_row, shift)
        if expected != o_rows[i + new_block_rows]:
            raise TemplateError(f"diff 失败：历史行 {i + 1} 被改动")
    # 前缀只允许 dimension 结束行 +3
    if _shift(t_pre, shift) != o_pre:
        raise TemplateError("diff 失败：主 sheet 前缀（cols/sheetViews 等）被改动")
    # 后缀只允许 mergeCell 引用 +3、count +1、且仅追加 A1:D1 一条
    expected_suf = _add_merge_cell(_shift(t_suf, shift), "A1:D1")
    if expected_suf != o_suf:
        raise TemplateError("diff 失败：主 sheet 后缀（mergeCells/pageSetup 等）被改动")

    # 3) sharedStrings：仅允许末尾追加；count 增量 = 新 si + 复用旧 si 的引用
    old_ss = t["xl/sharedStrings.xml"].decode("utf-8")
    new_ss = o["xl/sharedStrings.xml"].decode("utf-8")
    old_sis = _SI_SPLIT_RE.findall(old_ss)
    new_sis = _SI_SPLIT_RE.findall(new_ss)
    if len(new_sis) != len(old_sis) + n_new_si:
        raise TemplateError(
            f"diff 失败：sharedStrings 数量 {len(old_sis)} → {len(new_sis)} 与预期不符")
    if new_sis[:len(old_sis)] != old_sis:
        raise TemplateError("diff 失败：sharedStrings 历史条目被改动")
    # count 校验（引用总次数）
    m_old = re.search(r'<sst[^>]*count="(\d+)"', old_ss)
    m_new = re.search(r'<sst[^>]*count="(\d+)"', new_ss)
    if m_old and m_new:
        expected_count = int(m_old.group(1)) + n_new_si + extra_refs
        if int(m_new.group(1)) != expected_count:
            raise TemplateError(
                f"diff 失败：sharedStrings count {m_old.group(1)} → {m_new.group(1)}，"
                f"应为 {expected_count}")

    return {
        "sheet": sheet_file,
        "rows_before": len(t_rows),
        "rows_after": len(o_rows),
        "shared_strings_added": n_new_si,
        "shared_strings_reused_refs": extra_refs,
        "history_ok": True,
        "qa_whitelist": qa,
    }
