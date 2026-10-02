#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地 FastAPI 入口。
启动：uvicorn app.web.app:app --host 127.0.0.1 --port 8000
公开版默认仅供本机访问，不包含局域网认证；所有业务判断读 Core 的 ReportResult/manifest；
下载资格只看 status == PASS。"""

import os
import shutil
import tempfile
import uuid

from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from app.core import engine, snapshot
from app.web import (config, core_adapter, datasets, db, job_manager, period,
                     sync_service)

@asynccontextmanager
async def lifespan(_app):
    db.init()
    snapshot.init(config.SNAPSHOT_DB)
    yield

app = FastAPI(title="债市周报 Web v0.1", lifespan=lifespan)


@app.get("/healthz")
def healthz():
    """启动探活只确认 Web 进程可响应，不触发数据库/模板/快照状态扫描。"""
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index():
    from fastapi.responses import Response
    with open(os.path.join(os.path.dirname(__file__), "templates", "index.html"),
              encoding="utf-8") as f:
        html = f.read()
    return Response(html, media_type="text/html",
                    headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- 公共数据

@app.post("/api/datasets")
async def api_upload_datasets(file1: UploadFile = File(...),
                              file2: UploadFile = File(...)):
    """一次接收两份文件，自动判断哪份是新发行、哪份是 NAFMII；
    保存为不可变版本（重复上传 = 新版本，不覆盖历史）；随后自动触发交易所同步。"""
    raw1 = await file1.read()
    raw2 = await file2.read()
    t1 = datasets.classify(file1.filename, raw1)
    t2 = datasets.classify(file2.filename, raw2)
    if {t1, t2} != {"newbond", "nafmii"}:
        raise HTTPException(400, "需要“新发行债券”和“NAFMII”各一份文件")
    with tempfile.TemporaryDirectory() as tmp:
        paths = {}
        for typ, fname, raw in ((t1, file1.filename, raw1), (t2, file2.filename, raw2)):
            p = os.path.join(tmp, f"{typ}_{fname or typ}.xlsx")
            with open(p, "wb") as f:
                f.write(raw)
            paths[typ] = (p, fname)
        nb, nb_orig = paths["newbond"]
        nf, nf_orig = paths["nafmii"]
        dataset_id = datasets.save_dataset(nb, nf, nb_orig, nf_orig, "web")
    # 自动触发全量交易所同步（所有 Part 省份）；若有局部同步在跑则排队补跑
    synced = sync_service.start_sync(None)
    if synced["started"]:
        message = "本周数据已更新，交易所数据正在自动更新"
    elif synced.get("queued"):
        message = "本周数据已更新，交易所数据将在当前任务完成后自动更新"
    else:
        message = "本周数据已更新，正在运行的全量交易所同步已覆盖本次更新"
    return {"ok": True, "dataset_id": dataset_id,
            "message": message}


@app.post("/api/templates")
async def api_upload_template(file: UploadFile = File(...)):
    """首次使用某 Part 时上传上周周报；自动识别 Part，保存为不可变版本。"""
    parts, _ = engine.load_parts(config.PARTS_PATH)
    part = engine.identify_part(file.filename or "", parts)
    if part is None:
        raise HTTPException(400, "无法从文件名识别 Part，请按「西北四省债市周报20260808.xlsx」形式命名")
    raw = await file.read()
    target_dir = os.path.join(config.TEMPLATES_DIR, part["id"], uuid.uuid4().hex[:8])
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, file.filename or "template.xlsx")
    with open(target, "wb") as f:
        f.write(raw)
    con = db.connect()
    con.execute(
        "INSERT INTO part_templates (part_id, template_path, uploaded_by, uploaded_at) "
        "VALUES (?,?,?,?)", (part["id"], target, "web", job_manager._now()))
    con.commit()
    con.close()
    return {"ok": True, "part_id": part["id"], "part_name": part["name"]}


@app.post("/api/sync")
def api_sync(payload: dict = None):
    """按 Part 或省份范围更新交易所数据；不带参数 = 全部。"""
    payload = payload or {}
    provinces = None
    if payload.get("part_id"):
        parts, _ = engine.load_parts(config.PARTS_PATH)
        part = next((p for p in parts if p.get("id") == payload["part_id"]), None)
        if part is None:
            raise HTTPException(404, "未知 Part")
        provinces = list(part.get("provinces", []))
    elif payload.get("provinces"):
        provinces = list(payload["provinces"])
    res = sync_service.start_sync(provinces)
    if not res["started"]:
        return {"ok": True, "queued": res.get("queued", False),
                "message": res["reason"]}
    return {"ok": True, "queued": False, "message": "同步任务已启动"}


# ---------------------------------------------------------------- 任务

@app.post("/api/jobs")
def api_create_job(payload: dict):
    part_id = payload.get("part_id")
    if not part_id:
        raise HTTPException(400, "缺少 part_id")
    # 若同板块已经在生成，直接复用当前任务，防止双击/连点制造重复 job。
    # stale 时仍重建同周期；否则正常推断下一周期。
    st = job_manager.next_status(part_id, status_only=True)
    latest_ds = datasets.latest()
    if not latest_ds:
        raise HTTPException(400, "请先上传本周来源数据")
    job_id, reused = job_manager.create_or_reuse_job(
        part_id, latest_ds["id"], rebuild=bool(st.get("stale")))
    return {"ok": True, "job_id": job_id, "reused": reused}


@app.get("/api/jobs")
def api_list_jobs():
    return {"jobs": job_manager.list_recent(limit=20)}


@app.get("/api/jobs/{job_id}")
def api_get_job(job_id: str):
    return job_manager.get_job(job_id)


@app.post("/api/jobs/{job_id}/resolve")
def api_resolve(job_id: str, payload: dict):
    new_id = job_manager.resolve_and_rerun(job_id, payload.get("resolutions", []))
    return {"ok": True, "new_job_id": new_id}


@app.get("/api/jobs/{job_id}/download")
def api_download(job_id: str):
    job = job_manager.get_job(job_id)
    if not job.get("output_file") or not os.path.exists(job["output_file"]):
        raise HTTPException(404, "输出文件不存在")
    if job["status"] == "PASS":
        return FileResponse(job["output_file"], filename=job["download"]["filename"],
                            media_type="application/vnd.openxmlformats-officedocument"
                                       ".spreadsheetml.sheet")
    resp = FileResponse(job["output_file"], filename=job["download"]["filename"],
                        media_type="application/vnd.openxmlformats-officedocument"
                                   ".spreadsheetml.sheet")
    resp.headers["X-Report-Status"] = job["status"]
    return resp


# ---------------------------------------------------------------- 首页状态

def _exchange_needs_update(st, exch):
    """首页卡片只在“已知当前所需周期且现有快照确实落后”时提示需更新。

    交易所是否同步成功，与模板/下一期周报是否可生成是两个不同状态。
    模板缺失或周期无法推断时，不能因为 next_period=None 就把刚同步成功的
    数据误报为“需更新”。已完成本周报告时，也不能拿尚未开始的下一期截止日
    去要求再次同步。
    """
    if not exch:
        return False
    if st.get("done"):
        return False
    if st.get("stale"):
        # stale 分支中 next_ok=True 表示当前快照已经足以重新生成本周。
        return not bool(st.get("next_ok"))
    next_period = st.get("next_period")
    if not next_period:
        return False
    return exch[:10] < next_period[1]


@app.get("/api/status")
def api_status():
    ds = datasets.latest()
    parts, defaults = engine.load_parts(config.PARTS_PATH)
    snap = core_adapter.snapshot_status()
    synced = all(snap.get(k, {}).get("latest") for k in ("SSE", "SZSE", "BSE"))
    latest_sync = max((snap[k]["latest"] for k in ("SSE", "SZSE", "BSE")
                       if snap.get(k, {}).get("latest")), default=None)
    cards = []
    latest_scope_name = None
    recent_jobs = job_manager.list_recent(limit=20)
    recent_by_part = {}
    for item in recent_jobs:
        recent_by_part.setdefault(item["part_id"], item)
    for p in parts:
        tpl = job_manager._latest_template_for(p["id"])
        prev = None
        if tpl and os.path.exists(tpl):
            try:
                prev = period.parse_prev_week(tpl)
            except Exception:
                prev = None
        st = job_manager.next_status(p["id"], status_only=True)
        exch = core_adapter.scope_latest_status(p["provinces"])
        exchange_needs_update = _exchange_needs_update(st, exch)
        cards.append({
            "id": p["id"], "name": p["name"], "provinces": p["provinces"],
            "template_ready": bool(tpl),
            "prev_period": [prev[0].strftime("%m.%d"), prev[1].strftime("%m.%d")] if prev else None,
            "next_period": st["next_period"],
            "next_ok": st["next_ok"],
            "next_block": st["next_block"],
            "next_action": st.get("next_action", "none"),
            "done": st["done"],
            "stale": st.get("stale", False),
            "job_id": st.get("job_id"),
            "exchange_latest": exch,
            # “已同步”与“满足生成条件”分开。只有明确落后于当前所需周期才提示需更新。
            "exchange_needs_update": exchange_needs_update,
            "exchange_ready": bool(exch and not exchange_needs_update),
            "recent": ([recent_by_part[p["id"]]] if p["id"] in recent_by_part else []),
        })
        if exch == latest_sync:
            latest_scope_name = p["name"]
    return {
        "dataset": ds,
        "synced": synced,
        "latest_sync": latest_sync,
        "latest_scope_name": latest_scope_name,
        "sync_running": sync_service.state()["running"],
        "sync_progress": sync_service.state()["progress"],
        "sync_error": sync_service.state()["last_error"],
        "parts": cards,
        "recent_jobs": recent_jobs,
    }
