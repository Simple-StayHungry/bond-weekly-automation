#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""交易所同步（公共数据层）：按省份范围抓三所数据，写入权威快照库。
生成 Job 用 replay 模式只读快照库，因此本任务与 Job 完全解耦。
并发防护：同一时间只运行一个 worker；占用期间的新范围合并后顺序补跑。"""

import logging
import json
import os
import threading
import uuid

from app.core import main_mac, snapshot, capture_store
from app.web import config

_lock = threading.Lock()
_state = {"running": False, "last_error": None, "finished_at": None,
          "progress": {"percent": 0, "stage": ""},
          "active_provinces": None,
          "pending_all": False,
          "pending_provinces": []}

log = logging.getLogger("web.sync")


def all_provinces():
    """四 Part 省份并集（短名，去重保序）。"""
    from app.core import engine
    parts, _ = engine.load_parts(config.PARTS_PATH)
    seen, out = set(), []
    for p in parts:
        for prov in p.get("provinces", []):
            if prov not in seen:
                seen.add(prov)
                out.append(prov)
    return out


def full_province_names(short_list):
    from app.core.main_mac import to_data_province
    return [to_data_province(p) for p in short_list]


def _now():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _sync_body(provinces):
    capture_id = _now().replace("-", "").replace(":", "").replace(" ", "_") + "_" + uuid.uuid4().hex[:6]
    capture_dir = os.path.join(os.path.dirname(config.SNAPSHOT_DB), "captures", capture_id)
    os.makedirs(capture_dir, exist_ok=True)
    logger = log.getChild(capture_id)
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(os.path.join(capture_dir, "sync.log"), encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    try:
        result = _capture_and_save(provinces, capture_dir, logger)
        with open(os.path.join(capture_dir, "status.json"), "w", encoding="utf-8") as handle:
            json.dump({"status": "verified", "counts": result, "finished_at": _now()},
                      handle, ensure_ascii=False, indent=2)
        return result
    except Exception as error:
        logger.exception("交易所同步失败")
        capture_store.persist_failure(error, capture_dir)
        raise
    finally:
        logger.removeHandler(handler)
        handler.close()


def _capture_and_save(provinces, capture_dir, logger):
    """同步三所 → 快照库（阻塞主体；running 状态由 start_sync 管理）。
    provinces=None 时同步全部 Part 的省份；指定时只同步该范围。"""
    provinces = list(provinces) if provinces else all_provinces()
    full = full_province_names(provinces)
    snapshot.init(config.SNAPSHOT_DB)
    frames = []
    # One complete capture per exchange keeps its scope and evidence together.
    # Concatenating independent DataFrames can silently discard their attrs.
    for tag, label, fetch, scope, percent in (
            ("SSE", "上交所", main_mac.fetch_sse, provinces, 5),
            ("SZSE", "深交所", main_mac.fetch_szse, provinces, 35),
            ("BSE", "北交所", main_mac.fetch_bse, full, 75)):
        _state["progress"] = {"percent": percent, "stage": f"正在同步{label}（{len(provinces)}省）"}
        frame = fetch(scope, logger=logger)
        path = capture_store.persist_frame(frame, tag, os.path.join(capture_dir, tag))
        snapshot.validate_capture(frame, tag, provinces)
        frames.append((tag, frame, path))

    _state["progress"] = {"percent": 96, "stage": "写入快照库"}
    for tag, frame, path in frames:
        snapshot.save(frame, tag, scope=provinces, complete=True,
                      evidence_path=path, db_path=config.SNAPSHOT_DB)
    _state["progress"] = {"percent": 100, "stage": "已完成"}
    _state["finished_at"] = _now()
    return {tag.lower(): len(frame) for tag, frame, path in frames}


def run_sync(provinces=None):
    """同步执行（阻塞，供直接调用/测试）。"""
    try:
        return _sync_body(provinces)
    except Exception as e:
        _state["last_error"] = str(e)[:500]
        _state["progress"] = {"percent": 0, "stage": f"同步失败：{str(e)[:60]}"}
        raise


def start_sync(provinces=None):
    """非阻塞触发（可指定省份范围）。

    ``provinces=None`` 明确表示全量；“没有待办”由 ``pending_all=False``
    且 ``pending_provinces=[]`` 表示，二者不能共用同一个哨兵。
    已有局部同步在运行时，新范围会合并并由同一个 worker 顺序补跑。
    """
    requested = None if provinces is None else list(dict.fromkeys(provinces))
    with _lock:
        if _state["running"]:
            active = _state["active_provinces"]
            # 当前已经是全量同步，任何局部/全量请求都已被覆盖。
            if active is None:
                return {"started": False, "queued": False,
                        "reason": "全量同步任务正在运行，本次范围已覆盖"}
            if requested is None:
                _state["pending_all"] = True
                _state["pending_provinces"] = []
            elif not _state["pending_all"]:
                covered = set(active) | set(_state["pending_provinces"])
                _state["pending_provinces"].extend(
                    p for p in requested if p not in covered)
            queued = _state["pending_all"] or bool(_state["pending_provinces"])
            return {"started": False, "queued": queued,
                    "reason": "同步任务正在运行，完成后会自动补跑剩余省份"
                              if queued else "同步任务正在运行，本次范围已覆盖"}

        _state["running"] = True
        _state["last_error"] = None
        _state["active_provinces"] = requested
        _state["pending_all"] = False
        _state["pending_provinces"] = []
        _state["progress"] = {"percent": 0, "stage": "准备同步"}

    def worker():
        run_prov = requested
        try:
            while True:
                try:
                    _sync_body(run_prov)
                except Exception as e:
                    with _lock:
                        _state["last_error"] = str(e)[:500]
                        _state["progress"] = {
                            "percent": 0, "stage": f"同步失败：{str(e)[:60]}"}
                        _state["running"] = False
                        _state["active_provinces"] = None
                        _state["pending_all"] = False
                        _state["pending_provinces"] = []
                    return
                with _lock:
                    if _state["pending_all"]:
                        run_prov = None
                        _state["pending_all"] = False
                        _state["pending_provinces"] = []
                        _state["active_provinces"] = None
                    elif _state["pending_provinces"]:
                        run_prov = list(_state["pending_provinces"])
                        _state["pending_provinces"] = []
                        _state["active_provinces"] = run_prov
                    else:
                        # “无待办”判断与 running=False 在同一把锁内完成，
                        # 不给新请求留下入队后无人消费的竞态窗口。
                        _state["running"] = False
                        _state["active_provinces"] = None
                        return
        finally:
            # 防住非 Exception 类退出或未来维护中新增的意外路径。
            with _lock:
                if _state["running"]:
                    _state["running"] = False
                    _state["active_provinces"] = None
                    _state["pending_all"] = False
                    _state["pending_provinces"] = []

    threading.Thread(target=worker, daemon=True).start()
    return {"started": True, "queued": False}


def state():
    with _lock:
        out = dict(_state)
        out["progress"] = dict(_state["progress"])
        out["pending_provinces"] = list(_state["pending_provinces"])
        out["active_provinces"] = (None if _state["active_provinces"] is None
                                   else list(_state["active_provinces"]))
        return out
