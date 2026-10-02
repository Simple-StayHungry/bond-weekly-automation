import os
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app" / "core"))

import main_mac
import rules

class PublicCoreTests(unittest.TestCase):
    def test_amount_rules(self):
        self.assertEqual(rules.fmt_amount_sse("95"), "95.0")
        self.assertEqual(rules.fmt_amount_raw("20.00"), "20.00")
        self.assertEqual(rules.fmt_amount_nafmii(16.25), "16.25亿元")

    def test_synthetic_name_map_loads(self):
        m, conflicts = main_mac.load_name_map(str(ROOT / "input" / "简称.xlsx"))
        self.assertEqual(m["示例证券股份有限公司"], "示例证券")
        self.assertFalse(conflicts)

    def test_unknown_nafmii_status_is_blocked(self):
        df = pd.DataFrame({
            "项目名称": ["示例项目"], "项目状态": ["未配置状态"],
            "更新日期": ["2026-09-04"], "省份": ["河南省"],
            "品种": ["MTN"], "金额(亿)": [10],
            "管理人/主承销商": ["示例证券股份有限公司"],
        })
        text, blockers, *_ = main_mac.build_nafmii_section(df, ["河南省"])
        self.assertEqual(len(blockers), 1)
        self.assertIn("状态待确认预览", text)

    def test_no_priority_underwriter_by_default(self):
        self.assertEqual(main_mac.PRIORITY_UNDERWRITER, "")

if __name__ == "__main__":
    unittest.main(verbosity=2)
