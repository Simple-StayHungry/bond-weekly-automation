#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""web.db：Web 自己的状态（与 Core 的 exchange_snapshots.db 严格分离）。

Web 只负责：谁上传了什么、谁点了生成、任务什么状态、谁做了人工裁决、哪个文件允许下载。
业务判断（PASS/裁决/下载资格）一律来自 Core 的 ReportResult / manifest。
"""

import os
import sqlite3
from datetime import datetime

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_end TEXT NOT NULL,
    newbond_path TEXT NOT NULL,
    nafmii_path TEXT NOT NULL,
    newbond_sha256 TEXT NOT NULL,
    nafmii_sha256 TEXT NOT NULL,
    newbond_orig TEXT,
    nafmii_orig TEXT,
    uploaded_by TEXT NOT NULL,
    uploaded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    part_id TEXT NOT NULL,
    dataset_id INTEGER,
    template_path TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    output_file TEXT,
    manifest_path TEXT,
    blockers_json TEXT,
    error TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS manual_resolutions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    project TEXT NOT NULL,
    date TEXT,
    chosen TEXT NOT NULL,
    resolved_by TEXT NOT NULL,
    resolved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS part_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    part_id TEXT NOT NULL,
    template_path TEXT NOT NULL,
    uploaded_by TEXT NOT NULL,
    uploaded_at TEXT NOT NULL
);
"""


def connect():
    con = sqlite3.connect(config.WEB_DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.commit()
    return con


def init():
    con = connect()
    # Web 进程重启后，旧线程不可能继续运行；不能让上次遗留的 RUNNING/PENDING
    # 永久占住“生成中”状态。启动时明确标记为中断，允许用户重新生成。
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    con.execute(
        "UPDATE jobs SET status='FAILED', error=COALESCE(error, ?), finished_at=COALESCE(finished_at, ?) "
        "WHERE status IN ('PENDING','RUNNING')",
        ("服务已重启，上一次生成任务已中断，请重新生成", now),
    )
    con.commit()
    con.close()
