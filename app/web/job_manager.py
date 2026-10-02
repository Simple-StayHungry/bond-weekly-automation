#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Job Manager：任务的创建、异步执行、状态、裁决重跑、下载判定。
所有业务判断（PASS/裁决/下载资格）读 Core 的 ReportResult/manifest，不在本层重算。
"""

import json
import os
import threading
import uuid
from datetime import datetime, date as _date

from fastapi import HTTPException

from app.core import engine, snapshot
from app.web import config, core_adapter, db, period


_CREATE_LOCK = threading.Lock()


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _latest_template_for(part_id):
    """只使用用户显式上传的确认周报作为下一轮模板。

    本机生成的 PASS 成品不能自动冒充人工确认版：人工复核可能修改过数据、文字或格式，
    因此每轮完成后必须上传人工确认后的周报，才能推进下一期。
    """
    con = db.connect()
    tpl = con.execute(
        "SELECT template_path, uploaded_at FROM part_templates WHERE part_id=? "
        "ORDER BY id DESC LIMIT 1", (part_id,)).fetchone()
    con.close()
    return tpl["template_path"] if tpl else None


def infer_next_period(part_id, template_path):
    """用当前生效模板推断应生成的下一周期（只推断，不做数据集校验）。
    模板缺失/无法解析时返回 None。"""
    try:
        parts, defaults = engine.load_parts(config.PARTS_PATH)
        part = next((p for p in parts if p.get("id") == part_id), None)
        if part is None:
            return None
        rules = dict((defaults.get("rules") or {}))
        rules.update(part.get("rules") or {})
        prev_start, prev_end = period.parse_prev_week(template_path)
        week_end = period.resolve_week_end(rules, prev_end)
        start, end = period.next_week_range(prev_start, prev_end, week_end=week_end)
        return [start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")]
    except Exception:
        return None


def _validate_dataset_window(part_id, start, end, dataset):
    """数据集日期窗校验：允许窗口 [期结束-1, 期结束+lag]。"""
    import datetime as _dt
    ds_date = _dt.datetime.strptime(dataset["period_end"], "%Y-%m-%d").date()
    lower = end - _dt.timedelta(days=1)
    upper = end + _dt.timedelta(days=lag_for(part_id))
    if ds_date < lower:
        raise HTTPException(
            400, f"数据文件截至 {ds_date:%Y-%m-%d}，本轮报告周期为 "
                 f"{start:%m.%d}–{end:%m.%d}，本周来源数据还没到。"
                 f"请上传用户提供的最新“新发行债券”和“NAFMII”文件")
    if ds_date > upper:
        raise HTTPException(
            400, f"人工确认模板仍只推到 {end:%Y-%m-%d} 这一轮，但数据文件已经到 "
                 f"{ds_date:%Y-%m-%d}。请先上传人工确认后的最新周报作为模板，再生成下一轮")


def lag_for(part_id):
    parts, defaults = engine.load_parts(config.PARTS_PATH)
    part = next((p for p in parts if p.get("id") == part_id), None)
    rules = dict((defaults.get("rules") or {}))
    rules.update(part.get("rules") or {})
    return int(rules.get("source_date_lag_days", 0))


def _validate_exchange_freshness(part, start, end):
    """创建任务前只做已验证批次的轻量新鲜度检查。

    真正生成时 Core 的 replay 会再次读取完整快照并执行严格覆盖/完整性校验；
    因此这里不重复扫描整批明细，避免用户点“生成”后接口先卡住。
    """
    exch_at = core_adapter.scope_latest_status(part["provinces"])
    if not exch_at:
        raise HTTPException(
            400, f"{part['name']} 还没有交易所数据，请先点这张卡片上的「更新交易所数据」")
    if exch_at[:10] < end.strftime("%Y-%m-%d"):
        raise HTTPException(
            400, f"{part['name']} 的交易所数据停留在 {exch_at[:10]}（上一期），"
                 f"本期报告 {start:%m.%d}–{end:%m.%d} 需要本周快照。"
                 f"请先点这张卡片上的「更新交易所数据」，再生成周报")


def validate_ready(part_id, dataset_id):
    """生成前的完整前置检查（不创建任务）：模板 → 周期 → 数据集窗口 → 交易所快照新鲜度。
    任一不满足即 HTTPException；全部通过返回 (part, start, end, template_path)。"""
    con = db.connect()
    dataset = con.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
    con.close()
    if not dataset:
        raise HTTPException(404, "数据集不存在，请先上传本周 Wind 文件")
    dataset = dict(dataset)
    template_path = _latest_template_for(part_id)
    if not template_path or not os.path.exists(template_path):
        raise HTTPException(400, f"Part「{part_id}」还没有可用模板：请先生成过一期，或上传上周周报模板")
    part, start, end = _infer_period(part_id, template_path, dataset)
    _validate_exchange_freshness(part, start, end)
    return part, start, end, template_path


def _infer_period(part_id, template_path, dataset):
    """报告周期由“上期模板 + Part week_end 规则”确定；
    week_end 可固定，也可继承确认模板上一期实际截止星期；数据集 period_end
    只做一致性校验（上传错周文件在创建 Job 前直接拒绝）。"""
    parts, defaults = engine.load_parts(config.PARTS_PATH)
    part = next((p for p in parts if p.get("id") == part_id), None)
    if part is None:
        raise HTTPException(404, f"未知 Part：{part_id}")
    rules = dict((defaults.get("rules") or {}))
    rules.update(part.get("rules") or {})
    prev_start, prev_end = period.parse_prev_week(template_path)
    week_end = period.resolve_week_end(rules, prev_end)
    start, end = period.next_week_range(prev_start, prev_end, week_end=week_end)
    if (end - start).days < 2:
        raise HTTPException(
            400, f"上传的模板上一期只到 {prev_end:%m.%d}（推断本期 {start:%m.%d}—{end:%m.%d} 不足两天），"
                 f"可能是不完整版本。请上传完整版的上一期周报（应到该周最后一天，如 08.08 版）")
    _validate_dataset_window(part_id, start, end, dataset)
    return part, start, end


def _exchange_capture_time(manifest):
    """三所来源时间完整且有效时返回共同时间；未知不能冒充新鲜。"""
    try:
        exchange = manifest["source_manifest"]["exchange"]
        times = []
        for tag in ("SSE", "SZSE", "BSE"):
            info = exchange[tag]
            if info.get("complete") is False or info.get("status") == "SKIPPED":
                return None
            if info.get("verification_status", "verified") != "verified":
                return None
            stamp = info["captured_at"]
            datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
            times.append(stamp)
        return min(times)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def next_status(part_id, status_only=False):
    """status 接口用的「本期就绪状态」完整判定（只读，不创建任务）：
    - next_period  模板推断的应生成周期
    - next_ok      模板 + 数据集窗口 + 交易所快照 全部满足（可点生成）
    - next_block   next_ok=False 时的原因（供前端直接展示）
    - next_action  前端主动作枚举；禁止再靠 next_block 文案猜动作
    - done         最近 job 已覆盖数据集日期（本周已完成/或已完成但快照旧）"""
    try:
        parts, defaults = engine.load_parts(config.PARTS_PATH)
        part = next((p for p in parts if p.get("id") == part_id), None)
        if part is None:
            return {"next_period": None, "next_ok": False,
                    "next_block": "未知板块", "next_action": "none",
                    "done": False, "stale": False}
        rules = dict((defaults.get("rules") or {}))
        rules.update(part.get("rules") or {})
        con = db.connect()
        ds_row = con.execute("SELECT * FROM datasets ORDER BY id DESC LIMIT 1").fetchone()
        job_row = con.execute(
            "SELECT * FROM jobs WHERE part_id=? ORDER BY created_at DESC LIMIT 1", (part_id,)).fetchone()
        con.close()

        tpl = _latest_template_for(part_id)
        if not tpl or not os.path.exists(tpl):
            return {"next_period": None, "next_ok": False,
                    "next_block": "还没有上周周报，先上传一次", "next_action": "upload_template",
                    "done": False, "stale": False}

        try:
            prev_start, prev_end = period.parse_prev_week(tpl)
        except Exception:
            return {"next_period": None, "next_ok": False,
                    "next_block": "模板无法识别上周周期，请重新上传完整版上周周报",
                    "next_action": "upload_template", "done": False, "stale": False}
        week_end = period.resolve_week_end(rules, prev_end)
        start, end = period.next_week_range(prev_start, prev_end, week_end=week_end)
        next_period = [start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")]

        ds_date = None
        if ds_row:
            import datetime as _dt
            ds_date = _dt.datetime.strptime(ds_row["period_end"], "%Y-%m-%d").date()

        # done：最近 job 覆盖数据集日期，且其交易所快照覆盖该 job 周期（旧快照不算完成，仍须重生成）
        done = False      # 本周报告已生成（快照新鲜）
        stale = False     # 本周报告已生成，但交易所快照是上一期的 → 需重新生成本周
        job_id = None
        scope_latest = core_adapter.scope_latest_status if status_only else core_adapter.scope_latest
        exch_at_now = scope_latest(part["provinces"])   # 当前快照库最新批次
        if job_row and ds_date:
            exch_cap = None
            if job_row["manifest_path"] and os.path.exists(job_row["manifest_path"]):
                try:
                    with open(job_row["manifest_path"], encoding="utf-8") as f:
                        mj = json.load(f)
                    exch_cap = _exchange_capture_time(mj)
                except Exception:
                    pass
            fresh = bool(exch_cap and exch_cap[:10] >= job_row["period_end"][:10])
            # 完成状态必须绑定到“这一次上传的数据版本”，不能只比较日期。
            # 广东/河南等周五截止报告允许使用周六数据文件，旧的 period_end>=dataset
            # 判断会把成功生成误判成未完成；反过来，换了新一周数据后也不能沿用旧完成态。
            same_dataset = job_row["dataset_id"] == ds_row["id"]
            # 这一轮必须确实以用户显式上传的确认模板生成。旧版曾自动把本机生成成品
            # 链成下一轮模板；这会导致人工确认周报尚未上传时也被误判为完成。
            same_template = os.path.abspath(job_row["template_path"]) == os.path.abspath(tpl)
            final_status = job_row["status"] in ("PASS", "WARNING", "BLOCKED")
            done = same_dataset and same_template and fresh and final_status
            stale = same_dataset and same_template and not fresh and final_status
            job_id = job_row["id"]

        # stale 优先：本周已生成但快照旧 → 提示重新生成本周
        if stale:
            now_fresh = bool(exch_at_now and exch_at_now[:10] >= job_row["period_end"][:10])
            if now_fresh:
                return {"next_period": next_period, "next_ok": True, "next_block": None,
                        "next_action": "rebuild_report",
                        "done": False, "stale": True, "job_id": job_id}
            return {"next_period": next_period, "next_ok": False, "next_block":
                    f"本周报告已生成，但交易所数据为 {exch_cap[:10] if exch_cap else '未知'}（上一期）快照；"
                    f"请先点「更新交易所数据」，再重新生成本周报告",
                    "next_action": "sync_exchange",
                    "done": False, "stale": True, "job_id": job_id}

        # done 仅表示：最近任务确实使用当前确认模板 + 当前数据版本 + 新鲜交易所快照。
        # 确认模板一旦换新，旧任务自动失效，必须按确认版重新生成。

        # 数据集窗口
        if not ds_row:
            return {"next_period": next_period, "next_ok": False,
                    "next_block": "还没有本周数据，先上传用户提供的两份 Excel",
                    "next_action": "upload_data",
                    "done": done, "stale": False, "job_id": job_id}
        lower = end - _dt.timedelta(days=1)
        upper = end + _dt.timedelta(days=lag_for(part_id))
        if ds_date < lower:
            return {"next_period": next_period, "next_ok": False,
                    "next_block": f"来源数据截至 {ds_row['period_end']}，本轮 {start:%m.%d}–{end:%m.%d} 的数据还没到",
                    "next_action": "upload_data",
                    "done": done, "stale": False, "job_id": job_id}
        if ds_date > upper:
            return {"next_period": next_period, "next_ok": False,
                    "next_block": f"数据已经到 {ds_row['period_end']}，但人工确认模板仍只推到 {end:%m.%d}；请先上传人工确认后的最新周报",
                    "next_action": "upload_template",
                    "done": done, "stale": False, "job_id": job_id}

        # 交易所快照新鲜度
        exch_at = exch_at_now
        if not exch_at:
            return {"next_period": next_period, "next_ok": False,
                    "next_block": f"{part['name']} 还没有交易所数据，先点「更新交易所数据」",
                    "next_action": "sync_exchange",
                    "done": done, "stale": False, "job_id": job_id}
        if exch_at[:10] < end.strftime("%Y-%m-%d"):
            return {"next_period": next_period, "next_ok": False,
                    "next_block": f"交易所数据停留在 {exch_at[:10]}（上一期），本轮 {start:%m.%d}–{end:%m.%d} 需要先更新",
                    "next_action": "sync_exchange",
                    "done": done, "stale": False, "job_id": job_id}

        return {"next_period": next_period, "next_ok": True, "next_block": None,
                "next_action": "generate_report",
                "done": done, "stale": False, "job_id": job_id}
    except Exception as e:
        return {"next_period": None, "next_ok": False,
                "next_block": f"状态判定异常：{str(e)[:120]}", "next_action": "none",
                "done": False, "stale": False}


def active_job_id(part_id):
    """返回当前进程中该板块正在生成的任务；无则 None。

    db.init() 会在服务重启时清理旧 RUNNING/PENDING，因此这里命中的都是本次
    进程仍有效的任务，可安全复用，防止双击/连点重复创建。
    """
    con = db.connect()
    row = con.execute(
        "SELECT id FROM jobs WHERE part_id=? AND status IN ('PENDING','RUNNING') "
        "ORDER BY created_at DESC LIMIT 1", (part_id,)).fetchone()
    con.close()
    return row["id"] if row else None


def create_or_reuse_job(part_id, dataset_id, user="web", rebuild=False):
    """原子地复用正在运行的任务或创建新任务，防止前端连点产生重复任务。"""
    with _CREATE_LOCK:
        active = active_job_id(part_id)
        if active:
            return active, True
        if rebuild:
            return rebuild_current_job(part_id, user=user), False
        return create_job(part_id, dataset_id, user=user), False


def rebuild_current_job(part_id, user="web"):
    """以「原周期 + 最新模板 + 最新数据集 + 最新快照」重新生成本周报告。
    用于：本周报告已生成但交易所快照是上一期（旧快照）的情况；
    或用户更新快照后想用新数据重跑本周。同样执行完整前置校验。"""
    con = db.connect()
    ds_row = con.execute("SELECT * FROM datasets ORDER BY id DESC LIMIT 1").fetchone()
    job_row = con.execute(
        "SELECT * FROM jobs WHERE part_id=? ORDER BY created_at DESC LIMIT 1", (part_id,)).fetchone()
    con.close()
    if not ds_row:
        raise HTTPException(400, "请先上传本周 Wind 数据")
    if not job_row or not job_row["period_start"]:
        raise HTTPException(400, "还没有本周生成记录，请直接点「生成周报」")

    # 用原 job 的周期，但模板/数据集取最新（模板=最近上传或自动链成品；数据集=最新版本）
    ds = dict(ds_row)
    template_path = _latest_template_for(part_id)
    if not template_path or not os.path.exists(template_path):
        raise HTTPException(400, "Part 没有可用模板")

    # 周期校验：数据集窗口必须与原 job 周期匹配（防止数据集已前进到下周）
    import datetime as _dt
    s = _dt.datetime.strptime(job_row["period_start"], "%Y-%m-%d").date()
    e = _dt.datetime.strptime(job_row["period_end"], "%Y-%m-%d").date()
    _validate_dataset_window(part_id, s, e, ds)

    parts, _ = engine.load_parts(config.PARTS_PATH)
    part = next((p for p in parts if p.get("id") == part_id), None)
    if part is None:
        raise HTTPException(404, f"未知 Part：{part_id}")
    _validate_exchange_freshness(part, s, e)

    job_id = f"{e.strftime('%Y%m%d')}_{uuid.uuid4().hex[:6]}"
    con = db.connect()
    con.execute(
        "INSERT INTO jobs (id, part_id, dataset_id, template_path, period_start, period_end, "
        "status, created_by, created_at) VALUES (?,?,?,?,?,?,'PENDING',?,?)",
        (job_id, part_id, ds_row["id"], template_path, job_row["period_start"],
         job_row["period_end"], user, _now()))
    con.commit()
    con.close()
    threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()
    return job_id


def create_job(part_id, dataset_id, user="web"):
    # 唯一前置入口：模板 → 周期 → 数据集窗口 → 交易所快照新鲜度，全部通过才建任务
    part, start, end, template_path = validate_ready(part_id, dataset_id)

    job_id = f"{end.strftime('%Y%m%d')}_{uuid.uuid4().hex[:6]}"
    con = db.connect()
    con.execute(
        "INSERT INTO jobs (id, part_id, dataset_id, template_path, period_start, period_end, "
        "status, created_by, created_at) VALUES (?,?,?,?,?,?,'PENDING',?,?)",
        (job_id, part_id, dataset_id, template_path, start.strftime("%Y-%m-%d"),
         end.strftime("%Y-%m-%d"), user, _now()))
    con.commit()
    con.close()

    threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()
    return job_id


def _run_job(job_id):
    con = db.connect()
    job = dict(con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
    dataset = dict(con.execute("SELECT * FROM datasets WHERE id=?", (job["dataset_id"],)).fetchone())
    con.execute("UPDATE jobs SET status='RUNNING' WHERE id=?", (job_id,))
    con.commit()
    con.close()

    try:
        req = core_adapter.build_request(
            part_id=job["part_id"],
            period_start=job["period_start"],
            period_end=job["period_end"],
            template_path=job["template_path"],
            newbond_path=dataset["newbond_path"],
            nafmii_path=dataset["nafmii_path"],
            map_path=config.MAP_PATH,
            cutoff=core_adapter.latest_snapshot_cutoff(),
            manual_resolutions=[],
            force_first_all=False,  # 20260831 起 Core 固定采用审核流程中更靠后的状态
                                    # （按已配置的状态流程口径），
                                    # 每条自动处理都会记录在 manifest 与 warnings，可追溯
        )
        result, manifest_path = core_adapter.run(req, job_id)
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        con = db.connect()
        con.execute(
            "UPDATE jobs SET status=?, output_file=?, manifest_path=?, blockers_json=?, "
            "finished_at=? WHERE id=?",
            (result.status, result.output_file, manifest_path,
             json.dumps(result.blockers, ensure_ascii=False), _now(), job_id))
        con.commit()
        con.close()
    except Exception as e:
        con = db.connect()
        con.execute("UPDATE jobs SET status='FAILED', error=?, finished_at=? WHERE id=?",
                    (str(e)[:500], _now(), job_id))
        con.commit()
        con.close()


def get_job(job_id):
    con = db.connect()
    row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    con.close()
    if not row:
        raise HTTPException(404, "任务不存在")
    job = dict(row)
    manifest = {}
    if job.get("manifest_path") and os.path.exists(job["manifest_path"]):
        with open(job["manifest_path"], encoding="utf-8") as f:
            manifest = json.load(f)
    # 该 job 实际使用的交易所快照批次时间（审计：报告数据来源）
    exch_captured = _exchange_capture_time(manifest)
    # 快照是否覆盖该报告周期结束日；不覆盖 = 报告交易所部分是上一期的（需警示重生成）
    exch_stale = bool(job["status"] in ("PASS", "WARNING", "BLOCKED") and
                      (not exch_captured or exch_captured[:10] < job["period_end"][:10]))
    out = {
        "id": job["id"], "part_id": job["part_id"], "status": job["status"],
        "period": [job["period_start"], job["period_end"]],
        "output_file": job["output_file"],
        "error": job["error"],
        "created_by": job["created_by"], "created_at": job["created_at"],
        "finished_at": job["finished_at"],
        "qa": manifest.get("qa", {}),
        "metrics": manifest.get("metrics", {}),
        "warnings": manifest.get("warnings", []),
        "blockers": manifest.get("blockers", []),
        "manual_resolutions": manifest.get("manual_resolutions", []),
        "exchange_captured": exch_captured,
        "exchange_stale": exch_stale,
        "download": _download_policy(job),
    }
    return out


def _download_policy(job):
    """下载资格唯一来源 = Core 的 status；本层不做任何重判。"""
    if job["status"] == "PASS":
        return {"allowed": True, "kind": "final",
                "filename": os.path.basename(job["output_file"])}
    return {"allowed": False, "kind": "preview",
            "filename": os.path.basename(job["output_file"]) if job.get("output_file") else None,
            "reason": job["status"]}


def resolve_and_rerun(job_id, resolutions, user="web"):
    """人工裁决：记录 resolved_by/resolved_at（Web 审计），带裁决重跑生成新 job。"""
    old = get_job(job_id)
    if old["status"] != "BLOCKED":
        raise HTTPException(400, f"只有 BLOCKED 的任务需要裁决（当前 {old['status']}）")
    # 入库前完整校验；不创建带无效、缺省或部分裁决的新任务。
    if not isinstance(resolutions, list) or not resolutions:
        raise HTTPException(400, "请逐项明确选择全部待确认状态")
    blocker_by_key = {}
    for blocker in old.get("blockers", []):
        key = (blocker.get("project"), blocker.get("date"))
        candidates = blocker.get("candidates")
        if not key[0] or not isinstance(candidates, list) or not candidates:
            raise HTTPException(400, "该阻断项不能通过状态裁决解除，请修正源数据后重新生成")
        if key in blocker_by_key:
            raise HTTPException(400, "阻断清单存在重复项目与日期，请重新生成后确认")
        blocker_by_key[key] = blocker
    if not blocker_by_key:
        raise HTTPException(400, "此任务没有可人工确认的状态候选，请先处理其他阻断原因")
    chosen_by_key = {}
    for resolution in resolutions:
        if not isinstance(resolution, dict):
            raise HTTPException(400, "裁决格式错误")
        project, resolved_date = resolution.get("project"), resolution.get("date")
        if not isinstance(project, str) or not project or (resolved_date is not None and not isinstance(resolved_date, str)):
            raise HTTPException(400, "裁决项目和日期格式错误")
        key = (project, resolved_date)
        if key not in blocker_by_key:
            raise HTTPException(400, f"裁决项目或日期不在阻断清单中：{resolution.get('project')}")
        if key in chosen_by_key:
            raise HTTPException(400, f"同一项目和日期不能重复裁决：{key[0]}")
        chosen = resolution.get("chosen")
        if not isinstance(chosen, str) or chosen not in blocker_by_key[key]["candidates"]:
            raise HTTPException(400, f"必须从原始候选中明确选择状态：{key[0]}")
        chosen_by_key[key] = chosen
    if set(chosen_by_key) != set(blocker_by_key):
        missing = [key[0] for key in blocker_by_key if key not in chosen_by_key]
        raise HTTPException(400, "仍有状态未明确选择：" + "、".join(missing))
    cleaned = [{"project": project, "date": date, "chosen": chosen_by_key[(project, date)]}
               for project, date in blocker_by_key]

    con = db.connect()
    job = dict(con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
    for r in cleaned:
        con.execute(
            "INSERT INTO manual_resolutions (job_id, project, date, chosen, resolved_by, "
            "resolved_at) VALUES (?,?,?,?,?,?)",
            (job_id, r["project"], r.get("date"), r["chosen"], user, _now()))
    con.commit()
    con.close()

    new_id = f"{job['period_end'].replace('-', '')}_{uuid.uuid4().hex[:6]}"
    con = db.connect()
    con.execute(
        "INSERT INTO jobs (id, part_id, dataset_id, template_path, period_start, period_end, "
        "status, created_by, created_at) VALUES (?,?,?,?,?,?,'PENDING',?,?)",
        (new_id, job["part_id"], job["dataset_id"], job["template_path"],
         job["period_start"], job["period_end"], user, _now()))
    con.commit()
    con.close()

    threading.Thread(target=_run_resolved_job,
                     args=(new_id, cleaned), daemon=True).start()
    return new_id


def _run_resolved_job(job_id, resolutions):
    con = db.connect()
    job = dict(con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
    dataset = dict(con.execute("SELECT * FROM datasets WHERE id=?", (job["dataset_id"],)).fetchone())
    con.execute("UPDATE jobs SET status='RUNNING' WHERE id=?", (job_id,))
    con.commit()
    con.close()
    try:
        req = core_adapter.build_request(
            part_id=job["part_id"],
            period_start=job["period_start"],
            period_end=job["period_end"],
            template_path=job["template_path"],
            newbond_path=dataset["newbond_path"],
            nafmii_path=dataset["nafmii_path"],
            map_path=config.MAP_PATH,
            cutoff=core_adapter.latest_snapshot_cutoff(),
            manual_resolutions=resolutions,
        )
        result, manifest_path = core_adapter.run(req, job_id)
        con = db.connect()
        con.execute(
            "UPDATE jobs SET status=?, output_file=?, manifest_path=?, blockers_json=?, "
            "finished_at=? WHERE id=?",
            (result.status, result.output_file, manifest_path,
             json.dumps(result.blockers, ensure_ascii=False), _now(), job_id))
        con.commit()
        con.close()
    except Exception as e:
        con = db.connect()
        con.execute("UPDATE jobs SET status='FAILED', error=?, finished_at=? WHERE id=?",
                    (str(e)[:500], _now(), job_id))
        con.commit()
        con.close()


def list_recent(part_id=None, limit=5):
    con = db.connect()
    if part_id:
        rows = con.execute(
            "SELECT id, part_id, status, period_start, period_end, created_by, created_at, "
            "finished_at FROM jobs WHERE part_id=? ORDER BY created_at DESC LIMIT ?",
            (part_id, limit)).fetchall()
    else:
        rows = con.execute(
            "SELECT id, part_id, status, period_start, period_end, created_by, created_at, "
            "finished_at FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    con.close()
    return [dict(r) for r in rows]
