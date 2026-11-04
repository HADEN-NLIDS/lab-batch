"""JSONL 产物入库的回归测试。"""

import json
import os
import shutil
import tempfile

from django.test import TestCase

from apps.core.models import Organization
from apps.pipeline.importers import (import_explanation, import_items,
                                     load_severity, name_key)
from apps.reports.models import Explanation, Item, Report, ReportPage

FLAGGED = {
    "report_id": "E100_1",
    "items": [
        {
            "item_name": "白细胞计数(WBC)",
            "item_value": "11.2",
            "item_unit": "10^9/L",
            "item_range": "4-10",
            "is_abnormal": "abnormal",
            "abnormal_reason": "高于上限",
            "abnormal_confidence": "high",
        },
        {
            "item_name": "血红蛋白",
            "item_value": "145",
            "item_unit": "g/L",
            "item_range": "130-175",
            "is_abnormal": "normal",
        },
        {
            "item_name": "DeRitis比值",
            "item_value": "0.77",
            "item_unit": "",
            "item_range": "",
            "is_abnormal": "unknown",
        },
    ],
}

SEVERITY = {
    "report_id": "E100",
    "severity": {
        "max_level": "moderate",
        "items": [
            {"name": "白细胞计数(WBC)", "level": "moderate",
             "reason": "偏离约区间宽度的 1.2 倍"},
        ],
    },
}

EXPLANATIONS = {
    "report_id": "E100",
    "sections": [
        {"kind": "status", "title": "本次情况", "body": "白细胞略高。"},
        {"kind": "meaning", "title": "指标说明", "body": "白细胞参与免疫。"},
        {"kind": "action", "title": "建议", "body": "建议复查。"},
    ],
    "degraded": False,
    "model": "test-model",
}


class ImporterTest(TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.org = Organization.objects.create(name='测试企业', code='t1')
        self.report = Report.objects.create(
            tenant_id=self.org.id, report_no='E100', schema_version='1')
        ReportPage.objects.create(
            tenant_id=self.org.id, report=self.report, page_no=1,
            image_path='t/1.jpg')

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write(self, name, rows):
        path = os.path.join(self.tmpdir, name)
        with open(path, 'w', encoding='utf-8') as fp:
            for r in rows:
                fp.write(json.dumps(r, ensure_ascii=False) + '\n')
        return path

    def test_name_key_normalizes(self):
        # 规则与 tools/34_store.py 一致：转全角标点、去空格、去前缀符号，
        # 但不转全角字母（保持两侧 key 相同，否则同一指标会对不上）
        self.assertEqual(name_key('白细胞计数(WBC)'), '白细胞计数(wbc)')
        self.assertEqual(name_key('白细胞计数（WBC）'), '白细胞计数(wbc)')
        self.assertEqual(name_key(' ★项目 '), '项目')
        self.assertEqual(name_key('A～B'), 'a-b')

    def test_load_severity(self):
        path = self._write('severity.jsonl', [SEVERITY])
        mapping = load_severity(path)
        self.assertIn(name_key('白细胞计数(WBC)'), mapping)
        level, reason = mapping[name_key('白细胞计数(WBC)')]
        self.assertEqual(level, 'moderate')
        self.assertIn('1.2', reason)

    def test_import_items_writes_all_fields(self):
        flagged = self._write('flagged.jsonl', [FLAGGED])
        severity = self._write('severity.jsonl', [SEVERITY])

        n = import_items(self.report, flagged, severity)
        self.assertEqual(n, 3)

        items = list(Item.objects.filter(report=self.report).order_by('seq'))
        self.assertEqual(len(items), 3)

        wbc = items[0]
        self.assertEqual(wbc.name_raw, '白细胞计数(WBC)')
        self.assertEqual(wbc.name_key, '白细胞计数(wbc)')
        self.assertEqual(wbc.flag, 'abnormal')
        self.assertEqual(wbc.severity, 'moderate')
        self.assertIn('1.2', wbc.severity_reason)
        self.assertAlmostEqual(float(wbc.value_num), 11.2, places=4)

    def test_import_items_keeps_unknown_flag(self):
        flagged = self._write('flagged.jsonl', [FLAGGED])
        import_items(self.report, flagged)

        unknown = Item.objects.get(report=self.report, name_raw='DeRitis比值')
        self.assertEqual(unknown.flag, 'unknown')
        self.assertEqual(unknown.severity, '')

    def test_import_items_merges_pages(self):
        """多页产出应合并到同一份报告，seq 连续。"""
        page2 = dict(FLAGGED)
        page2['report_id'] = 'E100_2'
        page2['items'] = [{
            "item_name": "血小板计数(PLT)", "item_value": "148",
            "item_unit": "10^9/L", "item_range": "188-472",
            "is_abnormal": "abnormal",
        }]
        flagged = self._write('flagged.jsonl', [FLAGGED, page2])

        n = import_items(self.report, flagged)
        self.assertEqual(n, 4)
        seqs = list(Item.objects.filter(report=self.report)
                    .order_by('seq').values_list('seq', flat=True))
        self.assertEqual(seqs, [0, 1, 2, 3])

    def test_import_items_replaces_previous(self):
        flagged = self._write('flagged.jsonl', [FLAGGED])
        import_items(self.report, flagged)
        import_items(self.report, flagged)
        self.assertEqual(Item.objects.filter(report=self.report).count(), 3)

    def test_import_explanation(self):
        flagged = self._write('flagged.jsonl', [FLAGGED])
        import_items(self.report, flagged)
        expl = self._write('explanations.jsonl', [EXPLANATIONS])

        exp = import_explanation(self.report, expl)
        self.assertIsNotNone(exp)
        self.assertEqual(exp.tier, Explanation.TIER_LLM)
        self.assertEqual(exp.model, 'test-model')
        self.assertEqual(exp.n_items, 3)
        self.assertEqual(exp.n_abnormal, 1)
        self.assertEqual(exp.n_unknown, 1)
        self.assertEqual(exp.sections.count(), 3)

    def test_degraded_explanation_marked_template(self):
        flagged = self._write('flagged.jsonl', [FLAGGED])
        import_items(self.report, flagged)
        rec = dict(EXPLANATIONS)
        rec['degraded'] = True
        rec['model'] = ''
        expl = self._write('explanations.jsonl', [rec])

        exp = import_explanation(self.report, expl)
        self.assertEqual(exp.tier, Explanation.TIER_TEMPLATE)
        self.assertEqual(exp.guard_status, 'degraded')

    def test_missing_files_are_soft(self):
        empty = os.path.join(self.tmpdir, 'nope.jsonl')
        self.assertEqual(import_items(self.report, empty), 0)
        self.assertIsNone(import_explanation(self.report, empty))
