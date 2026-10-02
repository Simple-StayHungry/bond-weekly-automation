#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Core Adapter：Web Job ↔ Core 的唯一转换层。
以后 Core v1.x 有变化，只改这一层，路由/页面不感知。"""

import json
import logging
import os

from app.core import engine, snapshot
from app.web import config


def scope_available(provinces, cutoff):
    """该省份范围在截止时点是否有完整快照批次（生成前的引导检查）。"""
    return scope_latest(provinces, cutoff) is not None


def scope_latest(provinces, cutoff=None):
    """该省份范围三所均完整覆盖时的最近共同时间（无则 None）。

    必须分别找到 SSE/SZSE/BSE 的覆盖批次，再取三者最早时间；不能因任一
    单所先写入成功，就把整张卡片误报为交易所数据已就绪。
    """
    cutoff = cutoff or "9999-12-31 23:59:59"
    timestamps = []
    for source in ("SSE", "SZSE", "BSE"):
        try:
            _, info = snapshot.replay(source, cutoff, provinces,
                                      snapshot._columns_for(source), db_path=config.SNAPSHOT_DB)
        except (snapshot.SnapshotCoverageError, snapshot.SnapshotIntegrityError):
            return None
        timestamps.append(info["captured_at"])
    return min(timestamps)


def scope_latest_status(provinces, cutoff=None):
    """首页专用的轻量新鲜度查询：只读已验证批次元数据。

    不读取整批明细；正式生成前仍由 ``scope_latest``/Core replay 做严格校验。
    """
    cutoff = cutoff or "9999-12-31 23:59:59"
    timestamps = []
    for source in ("SSE", "SZSE", "BSE"):
        try:
            info = snapshot.latest_covering_info(
                source, cutoff, provinces, db_path=config.SNAPSHOT_DB)
        except (snapshot.SnapshotCoverageError, snapshot.SnapshotIntegrityError):
            return None
        timestamps.append(info["captured_at"])
    return min(timestamps)


def latest_snapshot_cutoff():
    """取快照库中最新批次的 captured_at（生成任务用 replay 模式，不抓官网）。"""
    stats = snapshot.snapshot_stats(db_path=config.SNAPSHOT_DB)
    times = [r[1] for r in stats if r[1]]
    return max(times) if times else None


def snapshot_status():
    """三所最近权威批次状态（首页展示 + 生成前校验）。"""
    stats = snapshot.snapshot_stats(db_path=config.SNAPSHOT_DB)
    out = {}
    for source, latest, ok_rows in stats:
        out[source] = {"latest": latest, "ok_rows": ok_rows}
    return out


def build_request(part_id, period_start, period_end, template_path, newbond_path,
                  nafmii_path, map_path, cutoff, manual_resolutions,
                  force_first_all=False):
    return engine.ReportRequest(
        part_id=part_id,
        start=period_start,
        end=period_end,
        template_path=template_path,
        newbond_path=newbond_path,
        nafmii_path=nafmii_path,
        map_path=map_path,
        exchange_mode="replay",          # 生成只读快照库；交易所同步是独立公共任务
        replay_cutoff=cutoff,
        manual_resolutions=manual_resolutions,
        force_first_all=force_first_all,
    )


def build_context(job_id):
    return engine.RunContext(
        job_id=job_id,
        workspace=os.path.join(config.JOBS_DIR, job_id),
        snapshot_db=config.SNAPSHOT_DB,
        parts_path=config.PARTS_PATH,
        logger=logging.getLogger(f"web.job.{job_id}"),
    )


def run(request, job_id):
    """执行一次生成（阻塞调用，由 Job Manager 在线程中调用）。
    Core fail-closed 抛 SystemExit（非 Exception 子类）：必须在此转换为
    RuntimeError，否则 Job Manager 的 except Exception 接不住、任务永久 RUNNING。"""
    ctx = build_context(job_id)
    try:
        result = engine.generate_report(request, ctx)
    except SystemExit as e:
        raise RuntimeError(f"Core 终止：{e}") from e
    manifest_path = os.path.join(config.JOBS_DIR, job_id, "manifest.json")
    return result, manifest_path
