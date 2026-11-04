"""解读段落与指标关联的测试。"""

import json
import os
import shutil
import tempfile

from django.test import TestCase

from apps.core.models import Organization
from apps.core.utils import cite_items_of
from apps.pipeline.importers import import_explanation, import_items
from apps.reports.models import Item, Report


class CiteItemsTest(TestCase):

    def test_long_name_wins(self):
        """短名是长名子串时只保留长名。"""
        names = ['淋巴细胞', '淋巴细胞绝对值(LYM#)']
        body = '本次淋巴细胞绝对值(LYM#) 13.89，略高于参考范围。'
        hits = cite_items_of(body, names)
        self.assertEqual(hits, ['淋巴细胞绝对值(LYM#)'])

    def test_independent_short_name_kept(self):
        """短名有独立出现时应当保留。"""
        names = ['红细胞', '红细胞压积 (HCT)']
        body = '红细胞 3.55 偏低，红细胞压积 (HCT) 34.5 也偏低。'
        hits = cite_items_of(body, names)
        self.assertIn('红细胞', hits)
        self.assertIn('红细胞压积 (HCT)', hits)

    def test_unmentioned_ignored(self):
        hits = cite_items_of('本次结果正常。', ['白细胞计数', '血红蛋白'])
        self.assertEqual(hits, [])

    def test_single_char_name_ignored(self):
        hits = cite_items_of('血钾 4.2', ['钾'])
        self.assertEqual(hits, [])

    def test_empty_body(self):
        self.assertEqual(cite_items_of('', ['白细胞']), [])


class ExplanationCiteTest(TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.org = Organization.objects.create(name='测试企业', code='t1')
        self.report = Report.objects.create(
            tenant_id=self.org.id, report_no='R1', schema_version='1')

        Item.objects.create(
            tenant_id=self.org.id, report=self.report, seq=0,
            name_raw='白细胞计数(WBC)', name_key='白细胞计数(wbc)',
            value_raw='11.2', flag='abnormal', severity='moderate')
        Item.objects.create(
            tenant_id=self.org.id, report=self.report, seq=1,
            name_raw='血红蛋白', name_key='血红蛋白',
            value_raw='145', flag='normal')

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_section_records_cited_items(self):
        rec = {
            'report_id': 'R1',
            'sections': [
                {'kind': 'status', 'title': '情况',
                 'body': '白细胞计数(WBC) 11.2 偏高，血红蛋白正常。'},
                {'kind': 'action', 'title': '建议',
                 'body': '建议复查。'},
            ],
            'degraded': False,
            'model': 'test',
        }
        path = os.path.join(self.tmpdir, 'e.jsonl')
        with open(path, 'w', encoding='utf-8') as fp:
            fp.write(json.dumps(rec, ensure_ascii=False) + '\n')

        exp = import_explanation(self.report, path)
        sections = list(exp.sections.order_by('seq'))

        self.assertIn('白细胞计数(wbc)', sections[0].cite_items)
        self.assertIn('血红蛋白', sections[0].cite_items)
        # 建议段没提到具体指标，应为空
        self.assertEqual(sections[1].cite_items, '')
