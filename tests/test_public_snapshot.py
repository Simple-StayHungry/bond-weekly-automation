import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.core import snapshot
from app.web import config, db


def evidence(records):
    text = json.dumps({"official_total": len(records), "data": records}, ensure_ascii=False)
    checksum = hashlib.sha256(text.encode()).hexdigest()
    return {
        "schema_version": 1, "source": "SZSE", "status": "verified", "complete": True,
        "started_at": "2026-09-01 10:00:00", "finished_at": "2026-09-01 10:00:00",
        "scope": ["陕西"], "fetched_count": len(records), "official_total": len(records),
        "provinces": [{"province": "陕西", "official_total": len(records), "actual_count": len(records),
                       "total_pages": 1, "pages": [{"page": 1, "page_size": 200,
                       "actual_count": len(records), "response_index": 0,
                       "response_sha256": checksum, "parameters": {"province": "陕西"}}]}],
        "responses": [{"url": "https://example.invalid/fixture", "method": "GET",
                       "http_status": 200, "response_text": text, "response_sha256": checksum}],
    }

class PublicSnapshotTests(unittest.TestCase):
    def test_public_web_defaults_to_localhost(self):
        self.assertEqual(config.HOST, "127.0.0.1")

    def test_empty_web_db_can_initialize(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = config.WEB_DB
            try:
                config.WEB_DB = os.path.join(tmp, "web.db")
                db.init()
                self.assertTrue(os.path.exists(config.WEB_DB))
            finally:
                config.WEB_DB = old

    def test_verified_snapshot_roundtrip(self):
        rows = [{"省份":"陕西","project_id":"demo-1","项目名称":"示例项目",
                 "承销商/管理人":"示例证券","债券类别":"私募","申请规模(亿元)":"5",
                 "项目进度":"已受理","更新日期":"2026-09-01"}]
        with tempfile.TemporaryDirectory() as tmp:
            path=os.path.join(tmp,"snap.db")
            snapshot.init(path)
            snapshot.save(pd.DataFrame(rows), "SZSE", ["陕西"], db_path=path, evidence=evidence(rows))
            out, meta=snapshot.replay("SZSE", "2099-01-01 00:00:00", ["陕西"], snapshot._columns_for("SZSE"), db_path=path)
            self.assertEqual(len(out), 1)
            self.assertEqual(out.iloc[0]["项目名称"], "示例项目")
            self.assertEqual(meta["verification_status"], "verified")

if __name__ == "__main__":
    unittest.main(verbosity=2)
