#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Web v0.1 配置：只放部署环境相关的东西（路径/口令/端口）。
业务判断一律读 Core 的 ReportResult/manifest，不在这里出现。"""

import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_DIR = os.path.join(BASE_DIR, "app")
CORE_DIR = os.path.join(APP_DIR, "core")

DATA_DIR = os.path.join(BASE_DIR, "data")
WEB_DB = os.path.join(DATA_DIR, "web.db")
SNAPSHOT_DB = os.path.join(DATA_DIR, "exchange_snapshots.db")

SHARED_DATASETS_DIR = os.path.join(BASE_DIR, "shared", "datasets")
JOBS_DIR = os.path.join(BASE_DIR, "jobs")
TEMPLATES_DIR = os.path.join(BASE_DIR, "shared", "templates")

PARTS_PATH = os.path.join(CORE_DIR, "parts.yaml")
MAP_PATH = os.path.join(BASE_DIR, "input", "简称.xlsx")

HOST = os.environ.get("WEEKLY_HOST", "127.0.0.1")
PORT = int(os.environ.get("WEEKLY_PORT", "8000"))

# 公开版默认仅监听本机回环地址，不提供局域网认证。
# 如需多人使用，请在受控网络中自行增加认证/反向代理后再开放监听地址。


for d in (DATA_DIR, SHARED_DATASETS_DIR, JOBS_DIR, TEMPLATES_DIR):
    os.makedirs(d, exist_ok=True)
