#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
核心引擎（P2：无状态纯任务接口）
================================
唯一入口：generate_report(request, context) -> ReportResult。
核心不感知 CLI / 双击脚本 / Web：三端都只是壳。

ReportRequest  只描述“我要什么”：part_id、报告周期、三个输入文件、数据模式。
RunContext     只描述“这次运行在哪、用什么资源”：job_id、workspace、快照库、
               配置、logger。每次任务独立 workspace（jobs/<job_id>/），互不覆盖。
ReportResult   结构化结果：status(PASS/WARNING/BLOCKED)、output_file、metrics、
               warnings、blockers、qa、source_manifest。
"""

import dataclasses
import hashlib
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import snapshot
import template_engine as te
import capture_store
from main_mac import (build_nib_section, build_nafmii_section,
                      extract_ent_bond_section, fetch_bse, fetch_sse, fetch_szse,
                      generate_bond_summary_by_province, load_name_map,
                      split_provinces, to_data_province)


@dataclass
class ReportRequest:
    part_id: str
    start: str                       # yyyy-mm-dd
    end: str                         # yyyy-mm-dd
    template_path: str               # 上周周报（模板）
    newbond_path: str
    nafmii_path: str
    map_path: str                    # 承销商简称表
    exchange_mode: str = "live"      # live | replay
    replay_cutoff: str = None        # replay 模式的截止时点
    skip_bse: bool = False
    # 北交所反爬临时拦截会自行解除：失败后等待重试（0 = 不重试，保持旧行为）
    bse_retries: int = 0
    bse_retry_wait: int = 600        # 重试间隔秒数
    # 旧版 NAFMII 人工裁决参数。20260831 起同日多状态按审核流程自动取更靠后的状态；
    # 字段保留仅为 CLI/Web 向后兼容。
    manual_resolutions: list = field(default_factory=list)
    #   [{"project": str, "date": "yyyy-mm-dd"|None, "chosen": 状态}]
    force_first_all: bool = False    # legacy：已不参与状态选择


@dataclass
class RunContext:
    job_id: str
    workspace: str                   # 本任务的独立工作目录
    snapshot_db: str
    parts_path: str
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("engine"))


@dataclass
class ReportResult:
    status: str                      # PASS | WARNING | BLOCKED
    output_file: str
    part_name: str
    period: tuple
    metrics: dict
    warnings: list
    blockers: list
    manual_resolutions: list         # 人工裁决审计记录（project/date/candidates/chosen/method）
    qa: dict
    source_manifest: dict


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_entry(path):
    """source_manifest 的输入文件条目：文件名 + sha256（可证明用材版本）。"""
    return {"filename": os.path.basename(path), "path": path, "sha256": _sha256(path)}


def identify_part(fname, parts):
    """按关键词（长度降序）识别 Part；失败返回 None。"""
    best = None
    for p in parts:
        for kw in sorted(p.get("keywords", []), key=len, reverse=True):
            if kw in fname:
                if best is None or len(kw) > len(best[0]):
                    best = (kw, p)
    return best[1] if best else None


def load_parts(parts_path):
    with open(parts_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg["parts"], cfg.get("defaults", {})


def fetch_bse_with_retry(fetch, attempts=1, wait_s=600, logger=None):
    """北交所抓取重试。WAF 临时拦截（会话被断）会自行解除，失败后等待再试，
    重试耗尽才抛出原异常——fail-closed 语义不变，只是不再一次失败就降级预览版。"""
    attempts = max(1, int(attempts))
    for i in range(1, attempts + 1):
        try:
            return fetch()
        except Exception as e:
            if i >= attempts:
                raise
            wait = max(0, int(wait_s))
            if logger is not None:
                logger.warning("北交所抓取失败（第 %d/%d 次）：%s；%d 秒后自动重试",
                               i, attempts, e, wait)
            time.sleep(wait)


def part_rules(part, defaults):
    merged = dict(defaults.get("rules") or {})
    merged.update(part.get("rules") or {})
    return merged


def highlight_rules_for(part, defaults):
    rules = part.get("highlight_rules") or defaults.get("highlight_rules") or []
    color = part.get("highlight_color")
    if color and rules:
        # 高亮红色按“往期成品”逐 Part 对齐（如广东/河南模板版为深红 C00000），
        # 不改 contains 匹配语义，只覆盖 font.color。
        rules = [dict(r, font=dict(r.get("font") or {}, color=color)) for r in rules]
    return rules


def generate_report(req: ReportRequest, ctx: RunContext) -> ReportResult:
    """Keep a durable job log even when acquisition/replay fails before a manifest."""
    log_dir = os.path.join(ctx.workspace, "logs")
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger(f"report.{ctx.job_id}.{uuid.uuid4().hex[:8]}")
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(os.path.join(log_dir, "run.log"), encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    try:
        logger.info("开始生成 %s，周期 %s 至 %s", req.part_id, req.start, req.end)
        result = _generate_report(req, dataclasses.replace(ctx, logger=logger))
        logger.info("生成结束：%s", result.status)
        return result
    except (Exception, SystemExit) as error:
        logger.exception("生成失败：%s", error)
        capture_store.persist_failure(error, log_dir)
        raise
    finally:
        logger.removeHandler(handler)
        handler.close()


def _generate_report(req: ReportRequest, ctx: RunContext) -> ReportResult:
    log = ctx.logger
    ws = ctx.workspace
    for d in ("input", "raw", "output", "logs"):
        os.makedirs(os.path.join(ws, d), exist_ok=True)
    warnings, blockers = [], []

    # ---- 1. Part Profile ----
    parts, defaults = load_parts(ctx.parts_path)
    part = next((p for p in parts if p.get("id") == req.part_id), None)
    if part is None:
        raise SystemExit(f"parts.yaml 中不存在 part_id={req.part_id}")
    rules = part_rules(part, defaults)
    highlight_rules = highlight_rules_for(part, defaults)
    formatter = part.get("formatter", "default")
    if formatter != "default":
        raise SystemExit(f"Part「{part['name']}」配置了未实现的 formatter：{formatter}")

    province_list = split_provinces(",".join(part["provinces"]))
    data_province_list = [to_data_province(p) for p in province_list]
    start = datetime.strptime(req.start, "%Y-%m-%d")
    end = datetime.strptime(req.end, "%Y-%m-%d")
    if end <= start:
        raise SystemExit(f"日期区间非法：{start} ~ {end}")

    # ---- 2. 交易所数据（live 抓取落库 / replay 重放）----
    snapshot.init(ctx.snapshot_db)
    qa = {"sse": "PASS", "szse": "PASS", "bse": "PASS", "nafmii": "PASS", "ooxml": "PASS"}
    source_manifest = {
        "inputs": {
            "template": _file_entry(req.template_path),
            "newbond": _file_entry(req.newbond_path),
            "nafmii": _file_entry(req.nafmii_path),
            "map": _file_entry(req.map_path),
        },
        "exchange": {},
        "exchange_mode": req.exchange_mode,
        "snapshot_db": ctx.snapshot_db,
        "coverage_start": snapshot.coverage_start(ctx.snapshot_db),
    }
    if req.exchange_mode == "replay":
        cutoff = req.replay_cutoff
        if not cutoff:
            raise RuntimeError("未找到可用的已验证快照，请先完成交易所同步")
        if len(cutoff) == 10:
            cutoff += " 23:59:59"
        log.info("快照重放模式，截止时点: %s", cutoff)
        frames = []
        for tag in ("SSE", "SZSE", "BSE"):
            try:
                df, info = snapshot.replay(tag, cutoff, province_list,
                                           snapshot._columns_for(tag),
                                           db_path=ctx.snapshot_db)
            except snapshot.SnapshotCoverageError as e:
                raise SystemExit(str(e)) from e
            frames.append(df)
            source_manifest["exchange"][tag] = info
            log.info("快照 %s: %s 条（run_id=%s, %s）",
                     tag, len(df), info["run_id"], info["captured_at"])
        SH_RAW_df, SZ_RAW_df, BJ_RAW_df = frames
        source_manifest["replay_cutoff"] = cutoff
    else:
        SH_RAW_df = fetch_sse(province_list, logger=log)
        SZ_RAW_df = fetch_szse(province_list, logger=log)
        if req.skip_bse:
            BJ_RAW_df = pd.DataFrame(columns=snapshot._columns_for("BSE"))
            qa["bse"] = "SKIPPED"
            log.warning("按参数跳过北交所")
            # 跳过 ≠ 抓过：只写 complete=0 的审计批次，replay 永远忽略；
            # 绝不产生“当时 0 项目”的权威空批次
            run_id, captured = snapshot.save(BJ_RAW_df, "BSE", scope=province_list,
                                             complete=False, db_path=ctx.snapshot_db)
            if run_id is not None:
                info = snapshot.run_info(run_id, db_path=ctx.snapshot_db) or {"run_id": run_id, "captured_at": captured}
                info["status"] = "SKIPPED"
                source_manifest["exchange"]["BSE"] = info
        else:
            BJ_RAW_df = fetch_bse_with_retry(
                lambda: fetch_bse(data_province_list, logger=log),
                attempts=req.bse_retries + 1, wait_s=req.bse_retry_wait, logger=log)
        acquired = [("SSE", SH_RAW_df), ("SZSE", SZ_RAW_df)]
        if qa["bse"] != "SKIPPED":
            acquired.append(("BSE", BJ_RAW_df))
        evidence_paths = {}
        for tag, df in acquired:
            evidence_paths[tag] = capture_store.persist_frame(df, tag, os.path.join(ws, "raw", "captures", tag))
            snapshot.validate_capture(df, tag, province_list)
        for tag, df in acquired:
            run_id, captured = snapshot.save(df, tag, scope=province_list, complete=True,
                                             evidence_path=evidence_paths[tag], db_path=ctx.snapshot_db)
            if run_id is not None:
                source_manifest["exchange"][tag] = snapshot.run_info(run_id, db_path=ctx.snapshot_db) or {
                    "run_id": run_id, "captured_at": captured}
        log.info("快照批次已写入 %s", ctx.snapshot_db)

    for df in (SH_RAW_df, SZ_RAW_df, BJ_RAW_df):
        if "更新日期" in df.columns:
            df["更新日期"] = pd.to_datetime(df["更新日期"], errors="coerce")
    for tag, df in (("SSE", SH_RAW_df), ("SZSE", SZ_RAW_df), ("BSE", BJ_RAW_df)):
        conflicts = _report_identity_conflicts(df, tag, start, end)
        if conflicts:
            qa[tag.lower()] = "BLOCKED"
            blockers.extend(conflicts)
            warnings.append(f"{tag} 本期涉及重复官方项目 ID，保留全部原始记录，需核实后再生成最终版")
    for tag, df in (("上交所", SH_RAW_df), ("深交所", SZ_RAW_df), ("北交所", BJ_RAW_df)):
        df.to_excel(os.path.join(ws, "raw", f"{tag}_数据.xlsx"), index=False)
    province_str = "、".join(province_list)
    with pd.ExcelWriter(os.path.join(ws, "raw", f"{province_str}交易所原始数据.xlsx"),
                        engine="openpyxl") as writer:
        SH_RAW_df.to_excel(writer, sheet_name="上交所", index=False)
        SZ_RAW_df.to_excel(writer, sheet_name="深交所", index=False)
        BJ_RAW_df.to_excel(writer, sheet_name="北交所", index=False)

    # ---- 3. 四栏文本 ----
    name_map, map_conflicts = load_name_map(req.map_path, logger=log)
    qa["names"] = "BLOCKED" if map_conflicts else "PASS"
    for c in map_conflicts:
        warnings.append(
            f"简称表冲突：简称「{c['short']}」同时映射自 {' / '.join(c['fulls'])}，"
            f"必须人工修正 input/简称.xlsx（会导致承销商简称张冠李戴）")
    nib_df = pd.read_excel(req.newbond_path)
    nafmii_df = pd.read_excel(req.nafmii_path)
    source_manifest["report_period"] = [req.start, req.end]
    source_manifest["manual_source_coverage"] = {
        "newbond": _observed_date_range(nib_df, "发行起始日"),
        "nafmii": _observed_date_range(nafmii_df, "更新日期"),
    }
    source_manifest["coverage_note"] = (
        "人工源表日期为文件中实际记录日期范围，不证明该日期之前已抓全；"
        "交易所 captured_at 为本次查询时点，报告标题日期不等于人工源表覆盖日期。")
    qa["coverage"] = "PASS"
    for source, info in source_manifest["manual_source_coverage"].items():
        if info["max_record_date"] and info["max_record_date"] < req.end:
            qa["coverage"] = "WARNING"
            warnings.append(f"{source} 源表最晚记录 {info['max_record_date']}，早于标题截至 {req.end}；后续日期未由该源表覆盖")

    # 新发行输入文件的周期 guard：发行起始日整体落在本期之外 = 疑似错周文件
    qa["newbond"] = "PASS"
    if "发行起始日" in nib_df.columns and len(nib_df):
        nib_dates = pd.to_datetime(nib_df["发行起始日"], errors="coerce").dropna()
        if len(nib_dates):
            dmax, dmin = nib_dates.max(), nib_dates.min()
            if dmax < start:
                qa["newbond"] = "BLOCKED"
                warnings.append(
                    f"新发行输入文件发行起始日最晚 {dmax:%Y-%m-%d}，早于本期开始 "
                    f"{req.start}：疑似误传上一周文件，已阻断")
            elif dmin > end:
                qa["newbond"] = "BLOCKED"
                warnings.append(
                    f"新发行输入文件发行起始日最早 {dmin:%Y-%m-%d}，晚于本期结束 "
                    f"{req.end}：疑似误传未来周文件，已阻断")
        else:
            warnings.append("新发行输入文件“发行起始日”全为空，无法校验报告周期")

    nib_stats = {}
    mapping_audit, unmapped_names = [], []
    nib_text = build_nib_section(nib_df, data_province_list, name_map=name_map,
                                 start_date=req.start, end_date=req.end, logger=log,
                                 stats=nib_stats, mapping_audit=mapping_audit)
    fb = nib_stats.get("fallback_underwriters") or []
    unmapped_names.extend(fb)
    if fb:
        warnings.append(
            "承销商简称表未命中（保留源名称），建议补充 input/简称.xlsx："
            + "、".join(fb[:10]) + ("…" if len(fb) > 10 else ""))
    cb_text = generate_bond_summary_by_province(
        SH_RAW_df, SZ_RAW_df, BJ_RAW_df, req.start, req.end, business_rules=rules,
        name_map=name_map, fallback_out=unmapped_names, mapping_audit=mapping_audit)
    eb_text = extract_ent_bond_section(cb_text)
    nafmii_mode = rules.get("nafmii_mode", "all")
    empty_break = bool(rules.get("empty_province_line", True))
    # 已知状态按流程；未知状态必须有明确人工选择，旧 force_first_all 无效。
    resolutions = {(r.get("project"), r.get("date")): r.get("chosen")
                   for r in req.manual_resolutions}
    nafmii_kwargs = dict(empty_province_break=empty_break, resolutions=resolutions,
                         force_first_all=req.force_first_all, logger=log,
                         name_map=name_map, fallback_out=unmapped_names,
                         mapping_audit=mapping_audit)
    if nafmii_mode == "weekly":
        nafmii_text, nafmii_blockers, nafmii_applied, nafmii_warnings = build_nafmii_section(
            nafmii_df, data_province_list, req.start, req.end, **nafmii_kwargs)
    else:
        nafmii_text, nafmii_blockers, nafmii_applied, nafmii_warnings = build_nafmii_section(
            nafmii_df, data_province_list, None, None, **nafmii_kwargs)
    blockers.extend(nafmii_blockers)
    if nafmii_blockers:
        qa["nafmii"] = "BLOCKED"
        warnings.append(f"交易商协会存在 {len(nafmii_blockers)} 个未处理异常，需人工检查")
    for w in nafmii_warnings:
        if w.get("type") == "nafmii_type_unresolved":
            warnings.append(f"交易商协会品种为空，已按源数据展示“/”，建议核实：{w.get('project', '')[:50]}")
        elif w.get("type") == "nafmii_unknown_status_flow":
            warnings.append(
                "交易商协会出现流程表未收录的新状态，需要确认，未按行号选择业务状态："
                f"{w.get('project', '')[:40]}（{' / '.join(w.get('unknown_statuses') or [])}）")

    # ---- 生成后自检：产出文本残留异常占位符则提示（不阻断，人工口径允许 nan 金额）----
    import re as _re
    for tag, text in (("A3", nib_text), ("B3", cb_text), ("C3", nafmii_text)):
        if _re.search(r"品种：/", text or ""):
            warnings.append(f"{tag} 残留未填品种“/”，需人工核实")

    # ---- 4. 富文本 + 模板引擎 ----
    title = f"债券市场一周回顾({start:%m.%d}-{end:%m.%d})"
    has_ent = bool(rules.get("has_ent_col", True))
    rt = {
        "title": te.RichText(te.font_runs(title, size=22, bold=True)),
        "a3": te.build_rich_text(nib_text or "最近一周无债券发行", size=11,
                                 highlight_rules=highlight_rules),
        "b3": te.build_rich_text(cb_text or "无", size=11, highlight_rules=highlight_rules),
        "c3": te.build_rich_text(nafmii_text or "无", size=11, highlight_rules=highlight_rules),
        "d3": (te.build_rich_text(eb_text or "无", size=11, highlight_rules=None)
               if has_ent else None),
    }
    base_height, col_widths = _read_template_layout(req.template_path)
    estimated_height = te.estimate_content_height(
        {"A": nib_text or "", "B": cb_text or "", "C": nafmii_text or "",
         "D": eb_text if has_ent else ""},
        col_widths=col_widths or te.DEFAULT_COL_WIDTHS, base=base_height)
    # 历史版本曾按 20260828 人工成品给广东/河南额外加底部余量；
    # 20260905 参考模板明确把长文本内容行压回 Excel/WPS 的约 409.5pt 上限。
    # 因此先保留兼容的 margin 逻辑，再以 Part 配置的 max_content_row_height 最终封顶。
    extra_lines = float(rules.get("row_height_extra_lines", 0) or 0)
    estimated_height = te.add_content_height_margin(
        estimated_height, base_height, extra_lines=extra_lines)
    estimated_height = te.cap_content_height(
        estimated_height, rules.get("max_content_row_height"))
    overflows = te.content_overflows(
        {"A": nib_text, "B": cb_text, "C": nafmii_text,
         "D": eb_text if has_ent else ""},
        col_widths or te.DEFAULT_COL_WIDTHS, estimated_height, font_size=11)
    qa["layout"] = "WARNING" if overflows else "PASS"
    for item in overflows:
        warnings.append(
            f"{item['cell']} 长文本可能显示不全：估算需要 {item['estimated_height_pt']:g} 磅，"
            f"来源模板行高 {item['actual_height_pt']:g} 磅；已保留字号、列宽及行高，需在 Office 查看。")
    if unmapped_names:
        warnings.append("简称表未命中，已保留源机构名称：" + "、".join(sorted(set(unmapped_names))))
    block = {
        "title": rt["title"], "a3": rt["a3"], "b3": rt["b3"], "c3": rt["c3"],
        "d3": rt["d3"],
        "height": estimated_height,
    }

    # ---- 5. 状态判定与输出命名（非 PASS 一律 preview，不标最终版）----
    requires_bse = bool(rules.get("requires_bse", True))
    status = "PASS"
    if blockers or any(value == "BLOCKED" for value in qa.values()):
        status = "BLOCKED"
    # 配置限定的行高及源表日期提示只作提醒，不改变正常成品资格。
    # 数据取得不完整才降级预览；关键业务异常仍在上方 BLOCKED。
    elif qa["bse"] == "SKIPPED" and requires_bse:
        status = "WARNING"
        if qa["bse"] == "SKIPPED":
            warnings.append("北交所数据被跳过且本 Part requires_bse=true：本次为预览版")
    suffix = "" if status == "PASS" else "_preview"
    out_name = f"{part['name']}{end.strftime('%Y%m%d')}{suffix}.xlsx"
    out_path = os.path.join(ws, "output", out_name)

    sheet_name, diag = te.build_weekly_report(req.template_path, out_path, block)
    qa["ooxml"] = "PASS" if diag.get("history_ok") else "BLOCKED"
    if qa["ooxml"] == "BLOCKED":
        status = "BLOCKED"
        warnings.append("历史模板区域一致性校验失败，禁止作为最终版")
        if not out_name.endswith("_preview.xlsx"):
            out_name = f"{part['name']}{end.strftime('%Y%m%d')}_preview.xlsx"
            preview_path = os.path.join(ws, "output", out_name)
            os.replace(out_path, preview_path)
            out_path = preview_path
    log.info("模板引擎完成：主 sheet「%s」，%s", sheet_name, diag)

    # ---- 6. manifest 与结果 ----
    manifest = {
        "job_id": ctx.job_id,
        "part_id": part.get("id"),
        "part_name": part.get("name"),
        "period": [req.start, req.end],
        "status": status,
        "output_file": os.path.basename(out_path),
        "qa": qa,
        "warnings": warnings,
        "blockers": blockers,
        "manual_resolutions": nafmii_applied,
        "source_manifest": source_manifest,
        "layout_overflows": overflows,
        "name_mapping_audit": mapping_audit,
        "unmapped_underwriters": sorted(set(unmapped_names)),
        # 生成文本存档：供 weekly.py --verify 回读比对（防生成后人工改动引入手误）
        "generated_texts": {
            "title": title,
            "a3": nib_text or "",
            "b3": cb_text or "",
            "c3": nafmii_text or "",
            "d3": (eb_text or "") if has_ent else None,
        },
        "metrics": {
            "新发行债券": len(_entries(nib_text)),
            "公司债项目": len(_entries(cb_text)),
            "交易商协会": len(_entries(nafmii_text)),
            "企业债检查": eb_text.replace("\n", " / "),
        },
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(os.path.join(ws, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    return ReportResult(
        status=status, output_file=out_path, part_name=part["name"],
        period=(req.start, req.end), metrics=manifest["metrics"],
        warnings=warnings, blockers=blockers,
        manual_resolutions=nafmii_applied, qa=qa,
        source_manifest=source_manifest,
    )


def _entries(text):
    import re
    return re.findall(r"(?m)^\d+、", text or "")


def _observed_date_range(frame, column):
    dates = pd.to_datetime(frame[column], errors="coerce") if column in frame else pd.Series(dtype="datetime64[ns]")
    valid = dates.dropna()
    return {"date_column": column, "row_count": len(frame),
            "dated_row_count": len(valid), "missing_or_invalid_date_count": len(frame) - len(valid),
            "min_record_date": valid.min().strftime("%Y-%m-%d") if len(valid) else None,
            "max_record_date": valid.max().strftime("%Y-%m-%d") if len(valid) else None}


def _report_identity_conflicts(frame, source, start, end):
    """Historical capture exceptions never authorize choosing a report-period row."""
    if frame.empty or "project_id" not in frame or "省份" not in frame:
        return []
    duplicated = frame[frame.duplicated(["省份", "project_id"], keep=False)]
    conflicts = []
    for (province, project_id), group in duplicated.groupby(["省份", "project_id"]):
        dates = pd.to_datetime(group["更新日期"], errors="coerce")
        if dates.between(start, end).any():
            conflicts.append({"type": "exchange_identity_conflict", "source": source,
                              "province": province, "project_id": str(project_id),
                              "dates": sorted(set(dates.dropna().dt.strftime("%Y-%m-%d"))),
                              "row_count": len(group), "severity": "BLOCKED"})
    return conflicts


def _read_template_layout(template_path):
    """模板上一周内容行行高与 A-D 列宽（引擎行高估算用）。"""
    import re
    entries = te.read_zip(template_path)
    sheet_file = te.find_main_sheet(entries)[1]
    xml = entries[sheet_file].decode("utf-8")
    m = re.search(r"<sheetData>.*?</sheetData>", xml, re.S)
    rows = te._ROW_SPLIT_RE.findall(m.group(0))
    if len(rows) < 3:
        raise te.TemplateError("模板主 sheet 行数不足，无法读取布局")
    base = float(re.search(r'ht="([\d.]+)"', rows[2]).group(1))
    widths = {}
    cm = re.search(r"<cols>.*?</cols>", xml, re.S)
    if cm:
        for m2 in re.finditer(r'<col min="(\d+)" max="(\d+)" width="([\d.]+)"', cm.group(0)):
            for c in range(int(m2.group(1)), min(int(m2.group(2)), 4) + 1):
                widths[chr(64 + c)] = float(m2.group(3))
    return base, widths


def new_job_id():
    return f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}"
