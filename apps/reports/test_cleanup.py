"""删除信号与清理任务的测试。"""

import os
import shutil
import tempfile
from datetime import timedelta

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.models import Organization
from apps.reports.models import Report, ReportPage
from apps.reports.signals import remove_image_file


class CleanupTest(TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.override = override_settings(MEDIA_ROOT=self.tmpdir)
        self.override.enable()

        self.org = Organization.objects.create(name='测试企业', code='t1')
        self.report = Report.objects.create(
            tenant_id=self.org.id, report_no='R001', schema_version='1')

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_page(self, page_no, content=b'x'):
        rel = 't/r%s_%d.jpg' % (self.report.id, page_no)
        abs_path = os.path.join(self.tmpdir, rel)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, 'wb') as fp:
            fp.write(content)
        return ReportPage.objects.create(
            tenant_id=self.org.id, report=self.report,
            page_no=page_no, image_path=rel)

    def test_deleting_page_removes_file(self):
        page = self._make_page(1)
        abs_path = os.path.join(self.tmpdir, page.image_path)
        self.assertTrue(os.path.exists(abs_path))

        page.delete()
        self.assertFalse(os.path.exists(abs_path))

    def test_deleting_report_removes_all_page_files(self):
        p1 = self._make_page(1)
        p2 = self._make_page(2)
        paths = [os.path.join(self.tmpdir, p.image_path) for p in (p1, p2)]
        self.assertTrue(all(os.path.exists(p) for p in paths))

        self.report.delete()
        self.assertFalse(any(os.path.exists(p) for p in paths))

    def test_soft_delete_keeps_files(self):
        page = self._make_page(1)
        abs_path = os.path.join(self.tmpdir, page.image_path)

        self.report.deleted_at = timezone.now()
        self.report.save(update_fields=['deleted_at'])

        self.assertTrue(os.path.exists(abs_path))

    def test_remove_rejects_path_outside_media(self):
        outside = os.path.join(tempfile.gettempdir(), 'should_not_delete.txt')
        with open(outside, 'w') as fp:
            fp.write('x')
        try:
            self.assertFalse(remove_image_file('../../' + os.path.basename(outside)))
            self.assertTrue(os.path.exists(outside))
        finally:
            if os.path.exists(outside):
                os.remove(outside)

    def test_purge_only_touches_expired(self):
        old_page = self._make_page(1)
        new_page = self._make_page(2)

        # 一份 8 天前软删除，一份刚软删除
        old_report = self.report
        old_report.deleted_at = timezone.now() - timedelta(days=8)
        old_report.save(update_fields=['deleted_at'])

        fresh_report = Report.objects.create(
            tenant_id=self.org.id, report_no='R002', schema_version='1')
        fresh_rel = 't/fresh.jpg'
        fresh_abs = os.path.join(self.tmpdir, fresh_rel)
        os.makedirs(os.path.dirname(fresh_abs), exist_ok=True)
        open(fresh_abs, 'wb').write(b'y')
        ReportPage.objects.create(
            tenant_id=self.org.id, report=fresh_report,
            page_no=1, image_path=fresh_rel)
        fresh_report.deleted_at = timezone.now()
        fresh_report.save(update_fields=['deleted_at'])

        call_command('purge_images', '--days', '7', verbosity=0)

        old_page.refresh_from_db()
        self.assertEqual(old_page.image_path, '')
        self.assertFalse(os.path.exists(
            os.path.join(self.tmpdir, old_page.image_path or 'x')))

        fresh_page = ReportPage.objects.get(report=fresh_report)
        self.assertEqual(fresh_page.image_path, fresh_rel)
        self.assertTrue(os.path.exists(fresh_abs))

    def test_purge_dry_run_keeps_files(self):
        self.report.deleted_at = timezone.now() - timedelta(days=10)
        self.report.save(update_fields=['deleted_at'])
        page = self._make_page(1)
        abs_path = os.path.join(self.tmpdir, page.image_path)

        call_command('purge_images', '--days', '7', '--dry', verbosity=0)

        page.refresh_from_db()
        self.assertEqual(page.image_path, 't/r%s_1.jpg' % self.report.id)
        self.assertTrue(os.path.exists(abs_path))
