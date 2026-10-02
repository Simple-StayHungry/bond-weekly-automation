#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公共数据集：老师的两份 Wind 文件是全组公共数据，不是 Job 输入。
上传一次建 Dataset（含 sha256/上传人/时间），所有 Part 的 Job 引用同一 dataset_id。
"""

import hashlib
import os
import shutil
import uuid
from datetime import datetime

from fastapi import HTTPException

from . import config, db, period


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def classify(filename, sample_bytes=None):
    """按文件名判断文件类型：newbond / nafmii。文件名不含关键字时按列名兜底。"""
    f = (filename or "").upper()
    if "NAFMII" in f:
        return "nafmii"
    if "新发行" in filename or "NEWBOND" in f:
        return "newbond"
    if sample_bytes is not None:
        try:
            import io
            import pandas as pd
            cols = pd.read_excel(io.BytesIO(sample_bytes), nrows=0).columns
            cols = {str(c) for c in cols}
            if "发行人省份" in cols and "债券全称" in cols:
                return "newbond"
            if "项目名称" in cols and "管理人/主承销商" in cols:
                return "nafmii"
        except Exception:
            pass
    raise HTTPException(400,
                        f"无法判断文件类型：{filename}（文件名应含“新发行债券”或“NAFMII”）")


def save_dataset(newbond_path, nafmii_path, newbond_orig, nafmii_orig, user):
    """保存两份文件到不可变版本目录 shared/datasets/<日期>/<uuid>/。
    已登记过的 source path 内容此后永不改变：重复上传 = 新版本新目录，
    旧数据集与旧 Job 的 sha256 审计链不受影响。"""
    d1 = period.date_from_filename(newbond_orig)
    d2 = period.date_from_filename(nafmii_orig)
    dates = [d for d in (d1, d2) if d]
    if not dates:
        # 文件名没有日期也不挑：从文件内容推断（新发行=发行起始日最大，NAFMII=更新日期最大）
        try:
            import datetime as _dt
            import pandas as pd
            nb_dates = pd.to_datetime(
                pd.read_excel(newbond_path).get("发行起始日"), errors="coerce").dropna()
            nf_dates = pd.to_datetime(
                pd.read_excel(nafmii_path).get("更新日期"), errors="coerce").dropna()
            cand = []
            if len(nb_dates):
                cand.append(nb_dates.max().date())
            if len(nf_dates):
                cand.append(nf_dates.max().date())
            if not cand:
                raise HTTPException(400, "无法从文件名或文件内容确定数据日期，请检查文件")
            dates = [max(cand)]
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(400, "无法从文件名或文件内容确定数据日期，请检查文件")
    period_end = max(dates)

    target_dir = os.path.join(config.SHARED_DATASETS_DIR,
                              period_end.strftime("%Y-%m-%d"), uuid.uuid4().hex[:8])
    os.makedirs(target_dir, exist_ok=True)
    nb_dst = os.path.join(target_dir, "newbond.xlsx")
    nf_dst = os.path.join(target_dir, "nafmii.xlsx")
    shutil.copy2(newbond_path, nb_dst)
    shutil.copy2(nafmii_path, nf_dst)

    con = db.connect()
    cur = con.execute(
        "INSERT INTO datasets (period_end, newbond_path, nafmii_path, newbond_sha256, "
        "nafmii_sha256, newbond_orig, nafmii_orig, uploaded_by, uploaded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (period_end.strftime("%Y-%m-%d"), nb_dst, nf_dst, _sha256(nb_dst), _sha256(nf_dst),
         newbond_orig, nafmii_orig, user, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    dataset_id = cur.lastrowid
    con.commit()
    con.close()
    return dataset_id


def latest():
    con = db.connect()
    row = con.execute("SELECT * FROM datasets ORDER BY id DESC LIMIT 1").fetchone()
    con.close()
    return dict(row) if row else None


def get(dataset_id):
    con = db.connect()
    row = con.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
    con.close()
    return dict(row) if row else None
