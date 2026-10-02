#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""交易所批次快照：只有有官方分页凭据的完整批次可作为生成依据。

历史批次原样保留并标为 legacy_unverified。重放先验证整个最近覆盖批次，
再按省份筛选；不静默丢坏行、不用旧批次掩盖最近批次的失败。
"""

import hashlib
import json
import os
import re
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pandas as pd

_PROVINCE_FULL = {
    "北京": "北京市", "天津": "天津市", "上海": "上海市", "重庆": "重庆市",
    "内蒙古": "内蒙古自治区", "广西": "广西壮族自治区", "西藏": "西藏自治区",
    "宁夏": "宁夏回族自治区", "新疆": "新疆维吾尔自治区",
    "香港": "香港特别行政区", "澳门": "澳门特别行政区",
}
_ALIASES = {
    "province": ("省份", "province", "发行人省份"),
    "project_id": ("project_id", "项目编号"),
    "project_name": ("债券名称/公募REITs名称", "项目名称", "project_name"),
    "bond_type": ("品种", "债券类别", "债券类型", "bond_type"),
    "amount": ("拟发行金额(亿元)", "申请规模(亿元)", "计划发行金额(亿元)", "amount"),
    "status": ("项目状态", "项目进度", "办理状态", "status"),
    "update_date": ("更新日期", "update_date"),
    "underwriter": ("承销商/管理人", "承销商", "underwriter"),
}


class SnapshotCoverageError(RuntimeError):
    """没有覆盖所需时间/省份的快照，不等于无项目。"""


class SnapshotIntegrityError(RuntimeError):
    """批次缺少完整性凭据或数据损坏，禁止用作正式生成依据。"""


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _short(p):
    return re.sub(r"(壮族自治区|回族自治区|维吾尔自治区|特别行政区|自治区|省|市)$",
                  "", str(p).strip())


def canonical_scope(provinces):
    short = {_short(p) for p in provinces}
    fulls = sorted({_PROVINCE_FULL.get(p, p + "省") for p in short if p})
    return "|".join(fulls), fulls


def init(db_path):
    """增量迁移只补审计列；不改写旧批次数据或旧 complete 标志。"""
    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as con, con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS snapshot_runs (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL, scope TEXT NOT NULL, scope_key TEXT NOT NULL,
                captured_at TEXT NOT NULL, complete INTEGER NOT NULL DEFAULT 0,
                row_count INTEGER NOT NULL DEFAULT 0,
                verification_status TEXT NOT NULL DEFAULT 'unverified',
                evidence_json TEXT, evidence_path TEXT
            );
            CREATE TABLE IF NOT EXISTS snapshot_rows (
                run_id INTEGER NOT NULL, project_id TEXT, province TEXT,
                project_name TEXT, bond_type TEXT, amount TEXT, status TEXT,
                update_date TEXT, raw_payload TEXT, raw_sha256 TEXT
            );
            CREATE TABLE IF NOT EXISTS snapshot_meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE INDEX IF NOT EXISTS idx_rows_run ON snapshot_rows(run_id);
            CREATE INDEX IF NOT EXISTS idx_runs_src_time ON snapshot_runs(source, captured_at);
        """)
        existing = {r[1] for r in con.execute("PRAGMA table_info(snapshot_runs)")}
        for name, declaration in (
            ("verification_status", "TEXT NOT NULL DEFAULT 'legacy_unverified'"),
            ("evidence_json", "TEXT"), ("evidence_path", "TEXT"),
        ):
            if name not in existing:
                con.execute(f"ALTER TABLE snapshot_runs ADD COLUMN {name} {declaration}")
        if "raw_sha256" not in {r[1] for r in con.execute("PRAGMA table_info(snapshot_rows)")}:
            con.execute("ALTER TABLE snapshot_rows ADD COLUMN raw_sha256 TEXT")
    return db_path


def _meta_get(con, key):
    row = con.execute("SELECT value FROM snapshot_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _meta_set(con, key, value):
    con.execute("INSERT OR REPLACE INTO snapshot_meta (key, value) VALUES (?,?)", (key, value))


def _empty(value):
    return value is None or (isinstance(value, str) and not value.strip()) or (
        not isinstance(value, (list, dict, tuple)) and bool(pd.isna(value)))


def _clean(value):
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return None if _empty(value) else value


def _dumps(value):
    return json.dumps(value, ensure_ascii=False, default=str, allow_nan=False)


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _integer(value, label):
    if isinstance(value, bool):
        raise SnapshotIntegrityError(f"{label} 必须为非负整数")
    try:
        number = int(value)
        if str(number) != str(value) or number < 0:
            raise ValueError()
        return number
    except (TypeError, ValueError):
        raise SnapshotIntegrityError(f"{label} 必须为非负整数，实际 {value!r}") from None


def _normalize(record, source, position):
    if not isinstance(record, dict):
        raise SnapshotIntegrityError(f"{source} 第 {position} 行 payload 必须是 JSON 对象")
    result = dict(record)
    for field, aliases in _ALIASES.items():
        present = [record[a] for a in aliases if a in record]
        if not present:
            raise SnapshotIntegrityError(f"{source} 第 {position} 行缺必需字段 {field}")
        values = [v for v in present if not _empty(v)]
        value = values[0] if values else None
        if isinstance(value, (dict, list, tuple)):
            raise SnapshotIntegrityError(f"{source} 第 {position} 行字段 {field} 不是标量")
        if value is None and field not in ("amount", "underwriter", "update_date"):
            raise SnapshotIntegrityError(f"{source} 第 {position} 行必需字段 {field} 为空")
        result[field] = value
    result["province"] = _short(result["province"])
    result["project_id"] = str(result["project_id"]).strip()
    if source == "SZSE" and re.fullmatch(r"SZ-\d*", result["project_id"]):
        raise SnapshotIntegrityError(f"SZSE 第 {position} 行仍使用动态行号项目ID，必须重新抓取")
    for field in ("bond_type", "status"):
        if str(result[field]).strip() in ("-", "/") or str(result[field]).strip().isdigit():
            raise SnapshotIntegrityError(f"{source} 第 {position} 行 {field} 未解析为业务标签")
    if isinstance(result["update_date"], (int, float, bool)):
        raise SnapshotIntegrityError(f"{source} 第 {position} 行更新日期不是明确日期文本")
    date = pd.to_datetime(result["update_date"], errors="coerce")
    if pd.isna(date):
        reason = record.get("date_missing_reason")
        is_abs = "ABS" in str(result["bond_type"]).upper() or "资产支持" in str(result["bond_type"])
        if __package__:
            from .exchange_fetch import SZSE_VERIFIED_MISSING_DATE_IDS
        else:
            from exchange_fetch import SZSE_VERIFIED_MISSING_DATE_IDS
        known_exception = (source == "SZSE" and result["province"] == "广东"
                           and result["project_id"] in SZSE_VERIFIED_MISSING_DATE_IDS)
        if not _empty(result["update_date"]) or not is_abs or _empty(reason) or not known_exception:
            raise SnapshotIntegrityError(
                f"{source} 第 {position} 行 {result['project_id']} 更新日期缺失或无效且无合法来源说明")
    else:
        result["update_date"] = date.strftime("%Y-%m-%d")
    # 中文业务列是旧快照权威来源；标准冗余空列不能覆盖其有效值。
    for field, aliases in _ALIASES.items():
        for alias in aliases:
            if alias in record or alias in _columns_for(source):
                if alias == "发行人省份" and not _empty(record.get(alias)):
                    continue
                result[alias] = result[field]
    result["省份"] = result["province"]
    return result


def _validate_records(records, source, scope, evidence=None):
    normalized = [_normalize(record, source, i) for i, record in enumerate(records, 1)]
    allowed = {_short(p) for p in scope}
    for r in normalized:
        if r["province"] not in allowed:
            raise SnapshotIntegrityError(f"{source} 行省份 {r['province']} 不在批次 scope 内")
    if __package__:
        from .exchange_fetch import validate_identity, ExchangeFetchError
    else:
        from exchange_fetch import validate_identity, ExchangeFetchError
    try:
        validate_identity(normalized, source, evidence if isinstance(evidence, dict) else {})
    except ExchangeFetchError as exc:
        raise SnapshotIntegrityError(str(exc)) from exc
    return normalized


def _validate_evidence(evidence, source, scope, records):
    if not isinstance(evidence, dict):
        raise SnapshotIntegrityError(f"{source} 缺少官方分页完整性凭据")
    if (evidence.get("schema_version") != 1 or evidence.get("source") != source
            or evidence.get("status") != "verified" or evidence.get("complete") is not True):
        raise SnapshotIntegrityError(f"{source} 官方分页凭据未标记 verified/complete 或版本/来源不匹配")
    requested = {_short(p) for p in scope}
    if {_short(p) for p in evidence.get("scope", [])} != requested:
        raise SnapshotIntegrityError(f"{source} 官方分页凭据 scope 与快照不一致")
    count = len(records)
    for key in ("fetched_count", "official_total"):
        if _integer(evidence.get(key), key) != count:
            raise SnapshotIntegrityError(f"{source} {key} 与成功读取数 {count} 不一致")
    if "row_count" in evidence and _integer(evidence["row_count"], "row_count") != count:
        raise SnapshotIntegrityError(f"{source} evidence.row_count 与成功读取数不一致")
    responses = evidence.get("responses")
    if not isinstance(responses, list) or not responses:
        raise SnapshotIntegrityError(f"{source} 缺官方原始响应（零条结果也必须保留）")
    for i, response in enumerate(responses):
        if not isinstance(response, dict) or not isinstance(response.get("response_text"), str):
            raise SnapshotIntegrityError(f"{source} 官方响应 {i} 缺原文")
        status = response.get("http_status", response.get("status"))
        if not isinstance(status, int) or not 200 <= status < 300:
            raise SnapshotIntegrityError(f"{source} 官方响应 {i} HTTP 状态无效")
        if not response.get("url") or not response.get("method"):
            raise SnapshotIntegrityError(f"{source} 官方响应 {i} 缺请求 URL/方法")
        if _digest(response["response_text"]) != response.get("response_sha256"):
            raise SnapshotIntegrityError(f"{source} 官方响应 {i} SHA-256 不一致")
    provinces = evidence.get("provinces")
    if not isinstance(provinces, list) or len(provinces) != len(requested):
        raise SnapshotIntegrityError(f"{source} 分省分页凭据缺失或重复")
    if {_short(p.get("province")) for p in provinces if isinstance(p, dict)} != requested:
        raise SnapshotIntegrityError(f"{source} 分省分页凭据未覆盖 scope")
    actual_by_province = Counter(r["province"] for r in records)
    for province in provinces:
        name = _short(province["province"])
        total = _integer(province.get("official_total"), "official_total")
        if total != actual_by_province[name] or total != _integer(province.get("actual_count"), "actual_count"):
            raise SnapshotIntegrityError(f"{source} {name} 官方总数/保存数/读取数不一致")
        pages = province.get("pages")
        page_count = _integer(province.get("total_pages"), "total_pages")
        # 官方零条可能声明0页，但仍须抓取首页来证实为空。
        if not isinstance(pages, list) or len(pages) != max(1, page_count):
            raise SnapshotIntegrityError(f"{source} {name} 尾页或分页凭据缺失")
        if not all(isinstance(p, dict) for p in pages):
            raise SnapshotIntegrityError(f"{source} {name} 分页凭据结构损坏")
        first_page = 0 if source == "BSE" else 1
        if [p.get("page") for p in pages] != list(range(first_page, first_page + len(pages))):
            raise SnapshotIntegrityError(f"{source} {name} 页码不连续")
        running_count = 0
        for page in pages:
            n = _integer(page.get("actual_count"), "page.actual_count")
            size = _integer(page.get("page_size"), "page.page_size")
            if not size or n > size or (total > 0 and n == 0):
                raise SnapshotIntegrityError(f"{source} {name} 中途空页或页大小无效")
            index = _integer(page.get("response_index"), "page.response_index")
            if index >= len(responses) or page.get("response_sha256") != responses[index]["response_sha256"]:
                raise SnapshotIntegrityError(f"{source} {name} 分页原始响应引用不一致")
            if not isinstance(page.get("parameters"), dict):
                raise SnapshotIntegrityError(f"{source} {name} 缺分页请求参数")
            running_count += n
        if running_count != total:
            raise SnapshotIntegrityError(f"{source} {name} 分页累计 {running_count} 不等于官方总数 {total}")
        if total == 0 and page_count > 1:
            raise SnapshotIntegrityError(f"{source} {name} 官方零条与页数矛盾")
    return evidence


def validate_capture(df, source, scope, evidence=None):
    """无磁盘写入的完整预验证；同步器可先校验三所，再开始保存任何批次。"""
    if df is None:
        raise SnapshotIntegrityError(f"{source} 抓取返回 None")
    if source not in ("SSE", "SZSE", "BSE"):
        raise SnapshotIntegrityError(f"未知快照来源 {source}")
    _, fulls = canonical_scope(scope)
    if not fulls:
        raise SnapshotIntegrityError("快照 scope 不能为空")
    supplied = evidence if evidence is not None else df.attrs.get("capture_evidence")
    records = _validate_records([_clean(r) for r in df.to_dict(orient="records")], source, fulls, supplied)
    _validate_evidence(supplied, source, fulls, records)
    return records


def save(df, source, scope, complete=None, snapshot_time=None, db_path=None,
         evidence=None, evidence_path=None):
    """保存完整批次需提供 capture_evidence；默认无凭据仅存 unverified。

    complete=True 是完整性要求，不能替代凭据。complete=False 可保留失败/跳过
    的非权威记录；这些记录绝不可作为“无项目”重放。
    """
    if df is None:
        raise SnapshotIntegrityError(f"{source} 抓取返回 None，不能保存为空成功")
    if not db_path:
        raise RuntimeError("db_path 必填（快照库路径需显式传入）")
    if source not in ("SSE", "SZSE", "BSE"):
        raise SnapshotIntegrityError(f"未知快照来源 {source}")
    scope_key, fulls = canonical_scope(scope)
    if not fulls:
        raise SnapshotIntegrityError("快照 scope 不能为空")
    raw_records = [_clean(r) for r in df.to_dict(orient="records")]
    supplied = evidence if evidence is not None else df.attrs.get("capture_evidence")
    records = _validate_records(raw_records, source, fulls, supplied)
    verified = False
    if complete is not False and (supplied is not None or complete is True):
        _validate_evidence(supplied, source, fulls, records)
        verified = True
    # A delayed save or re-save must retain the actual completed capture time.
    # Legacy diagnostic/import evidence may lack times; keep its explicit clock contract.
    finished = supplied.get("finished_at") if verified else None
    if verified and finished is None and snapshot_time is None:
        raise SnapshotIntegrityError(f"{source} 缺少采集完成时刻；历史导入必须显式提供 snapshot_time")
    if finished is not None:
        try:
            local_zone = timezone(timedelta(hours=8))
            def parse_clock(value):
                parsed = datetime.fromisoformat(value)
                return parsed.replace(tzinfo=local_zone) if parsed.tzinfo is None else parsed.astimezone(local_zone)
            finished_dt = parse_clock(finished)
            if supplied.get("started_at") is not None:
                started_dt = parse_clock(supplied["started_at"])
                if started_dt > finished_dt:
                    raise ValueError("capture finished before it started")
        except (TypeError, ValueError) as exc:
            raise SnapshotIntegrityError(f"{source} 采集起止时刻无效") from exc
        # Existing snapshot consumers use second precision; round up rather than
        # make the completed batch available fractionally before its finish time.
        if finished_dt.microsecond:
            finished_dt += timedelta(seconds=1)
        finished = finished_dt.strftime("%Y-%m-%d %H:%M:%S")
        if snapshot_time is not None and snapshot_time != finished:
            raise SnapshotIntegrityError(f"{source} snapshot_time 不得改写官方采集完成时刻")
    snap = finished or snapshot_time or _now()
    payloads = [_dumps(r) for r in raw_records]
    init(db_path)
    with closing(sqlite3.connect(db_path)) as con, con:
        cur = con.execute(
            "INSERT INTO snapshot_runs (source,scope,scope_key,captured_at,complete,row_count,"
            "verification_status,evidence_json,evidence_path) VALUES (?,?,?,?,?,?,?,?,?)",
            (source, _dumps(fulls), scope_key, snap, int(verified), len(records),
             "verified" if verified else "unverified", _dumps(supplied) if supplied else None,
             evidence_path or (supplied or {}).get("evidence_path")))
        run_id = cur.lastrowid
        rows = [(run_id, r["project_id"], r["province"], r["project_name"], r["bond_type"],
                 r["amount"], r["status"], r["update_date"], payload, _digest(payload))
                for r, payload in zip(records, payloads)]
        con.executemany(
            "INSERT INTO snapshot_rows (run_id,project_id,province,project_name,bond_type,"
            "amount,status,update_date,raw_payload,raw_sha256) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        if verified:
            start = _meta_get(con, "verified_coverage_start")
            if start is None or snap[:10] < start:
                _meta_set(con, "verified_coverage_start", snap[:10])
    return run_id, snap


def _info(row):
    return dict(zip(("run_id", "source", "scope_key", "captured_at", "complete", "row_count",
                     "verification_status", "evidence_path"), row), complete=bool(row[4]))


_INFO_COLUMNS = "run_id,source,scope_key,captured_at,complete,row_count,verification_status,evidence_path"


def run_info(run_id, db_path=None):
    if not db_path or not os.path.exists(db_path):
        return None
    init(db_path)
    with closing(sqlite3.connect(db_path)) as con:
        row = con.execute(f"SELECT {_INFO_COLUMNS},evidence_json FROM snapshot_runs WHERE run_id=?", (run_id,)).fetchone()
    if not row:
        return None
    info = _info(row[:8])
    try:
        evidence = json.loads(row[8]) if row[8] else {}
        info["identity_conflicts"] = evidence.get("identity_conflicts", [])
    except (TypeError, ValueError, AttributeError):
        raise SnapshotIntegrityError(f"{info['source']} 批次 {run_id} 官方凭据 JSON 损坏") from None
    return info


def coverage_start(db_path=None):
    if not db_path or not os.path.exists(db_path):
        return None
    with closing(sqlite3.connect(db_path)) as con:
        return _meta_get(con, "verified_coverage_start")


def latest_covering_info(source, cutoff, provinces, db_path=None):
    """仅查询覆盖指定省份的最近已验证批次元数据。

    只供首页/状态展示使用，不读取 snapshot_rows，也不替代 ``replay`` 的
    逐行完整性校验。正式生成仍必须走 ``replay``。若最近覆盖批次未验证，
    与 ``replay`` 一样 fail-closed，不回退到更旧批次。
    """
    if not db_path or not os.path.exists(db_path):
        raise SnapshotCoverageError(f"快照库不存在：{db_path}（无法查询 {source}）")
    init(db_path)
    _, wanted = canonical_scope(provinces)
    with closing(sqlite3.connect(db_path)) as con:
        rows = con.execute(
            f"SELECT {_INFO_COLUMNS},scope FROM snapshot_runs "
            "WHERE source=? AND captured_at<=? ORDER BY captured_at DESC,run_id DESC",
            (source, cutoff)).fetchall()
    for row in rows:
        try:
            scope = json.loads(row[8])
            if not isinstance(scope, list) or not scope:
                raise ValueError()
        except (TypeError, ValueError):
            raise SnapshotIntegrityError(
                f"{source} 批次 {row[0]} scope 损坏，禁止退回旧批次") from None
        if set(canonical_scope(scope)[1]) >= set(wanted):
            info = _info(row[:8])
            if not info["complete"] or info["verification_status"] != "verified":
                raise SnapshotIntegrityError(
                    f"{source} 最近批次 {info['run_id']} 为 {info['verification_status']}，"
                    "缺少已验证官方分页凭据；须重新同步，禁止退回旧批次")
            return info
    raise SnapshotCoverageError(f"截止 {cutoff} 无覆盖 {wanted} 的 {source} 批次")


def replay(source, cutoff, provinces, columns, db_path=None, allow_legacy=False):
    """先校验最近覆盖批次的全部行，才筛省份；不退回更早批次。

    allow_legacy 仅供只读诊断，返回 verification_status=legacy_unverified；
    正式报告调用必须保持默认 False。即便诊断也不能跳过损坏的行。
    """
    if not db_path or not os.path.exists(db_path):
        raise SnapshotCoverageError(f"快照库不存在：{db_path}（无法重放 {source}）")
    init(db_path)
    _, wanted = canonical_scope(provinces)
    picked = None
    with closing(sqlite3.connect(db_path)) as con:
        runs = con.execute(
            f"SELECT {_INFO_COLUMNS},scope,evidence_json FROM snapshot_runs "
            "WHERE source=? AND captured_at<=? ORDER BY captured_at DESC,run_id DESC",
            (source, cutoff)).fetchall()
        for row in runs:
            try:
                scope = json.loads(row[8])
                if not isinstance(scope, list) or not scope:
                    raise ValueError()
            except (TypeError, ValueError):
                raise SnapshotIntegrityError(f"{source} 批次 {row[0]} scope 损坏，禁止退回旧批次") from None
            if set(canonical_scope(scope)[1]) >= set(wanted):
                picked = row
                break
        if picked is None:
            raise SnapshotCoverageError(f"截止 {cutoff} 无覆盖 {wanted} 的 {source} 批次")
        info = _info(picked[:8])
        legacy = info["verification_status"] == "legacy_unverified"
        if not info["complete"] or (info["verification_status"] != "verified" and not (allow_legacy and legacy)):
            raise SnapshotIntegrityError(
                f"{source} 最近批次 {info['run_id']} 为 {info['verification_status']}，"
                "缺少已验证官方分页凭据；须重新同步，禁止退回旧批次")
        payloads = con.execute("SELECT raw_payload,raw_sha256 FROM snapshot_rows WHERE run_id=?",
                               (info["run_id"],)).fetchall()
    declared = _integer(info["row_count"], "snapshot_runs.row_count")
    if len(payloads) != declared:
        raise SnapshotIntegrityError(f"{source} 批次 {info['run_id']} 声明 {declared} 行，SQL 实存 {len(payloads)} 行")
    records = []
    for i, (payload, checksum) in enumerate(payloads, 1):
        if not isinstance(payload, str) or (not legacy and checksum != _digest(payload)):
            raise SnapshotIntegrityError(f"{source} 批次 {info['run_id']} 第 {i} 行原文 SHA-256 不一致")
        try:
            records.append(json.loads(payload, parse_constant=lambda s: (_ for _ in ()).throw(ValueError(s))))
        except (TypeError, ValueError):
            raise SnapshotIntegrityError(f"{source} 批次 {info['run_id']} 第 {i} 行 JSON 损坏") from None
    evidence = None
    if not legacy:
        try:
            evidence = json.loads(picked[9])
        except (TypeError, ValueError):
            raise SnapshotIntegrityError(f"{source} 批次 {info['run_id']} 官方凭据 JSON 损坏") from None
    records = _validate_records(records, source, json.loads(picked[8]), evidence)
    if not legacy:
        _validate_evidence(evidence, source, json.loads(picked[8]), records)
    elif not records:
        raise SnapshotIntegrityError(f"{source} 旧零条批次缺少官方零条响应，不能证明无项目")
    info["validated_row_count"] = len(records)
    info["identity_conflicts"] = (evidence or {}).get("identity_conflicts", [])
    info["date_exceptions"] = [
        {"project_id": r["project_id"], "bond_type": r["bond_type"], "reason": r["date_missing_reason"]}
        for r in records if _empty(r["update_date"])]
    wanted_short = {_short(p) for p in provinces}
    selected = [r for r in records if r["province"] in wanted_short]
    df = pd.DataFrame(selected)
    # 可选显示列（序号、受理日期等）可空；关键业务列已逐行检查，不能靠补空通过。
    for col in columns:
        if col not in df.columns:
            df[col] = ""
    if "更新日期" in df.columns:
        df["更新日期"] = pd.to_datetime(df["更新日期"], errors="coerce")
    result = df[columns].copy()
    if not legacy:
        result.attrs["capture_evidence"] = evidence
    return result, info


def snapshot_stats(db_path=None):
    """只统计已验证批次；旧 complete=1 不再使网页显示为正常已同步。"""
    if not db_path or not os.path.exists(db_path):
        return []
    init(db_path)
    with closing(sqlite3.connect(db_path)) as con:
        return con.execute(
            "SELECT r.source,r.captured_at,r.row_count FROM snapshot_runs r "
            "WHERE r.complete=1 AND r.verification_status='verified' AND r.run_id=("
            "SELECT r2.run_id FROM snapshot_runs r2 WHERE r2.source=r.source "
            "AND r2.complete=1 AND r2.verification_status='verified' "
            "ORDER BY r2.captured_at DESC,r2.run_id DESC LIMIT 1) ORDER BY r.source").fetchall()


def _columns_for(source):
    if __package__:
        from .exchange_fetch import SSE_COLUMNS, SZSE_COLUMNS, BSE_COLUMNS
    else:
        from exchange_fetch import SSE_COLUMNS, SZSE_COLUMNS, BSE_COLUMNS
    return list({"SSE": SSE_COLUMNS, "SZSE": SZSE_COLUMNS, "BSE": BSE_COLUMNS}[source])
