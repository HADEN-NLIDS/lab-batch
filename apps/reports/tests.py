"""上传、页归并、视图权限的回归测试。"""

import io
import shutil
import tempfile
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image

from apps.accounts.models import User
from apps.core.models import Organization
from apps.core.utils import parse_employee_no
from apps.reports.models import PendingFile, Report, ReportPage, Subject
from apps.reports.services import (assign_pending_file, confirm_upload,
                                   handle_upload, stage_upload)


def make_image(color, fmt='JPEG'):
    buf = io.BytesIO()
    Image.new('RGB', (400, 300), color).save(buf, fmt)
    return buf.getvalue()


def upload_file(name, color='white'):
    return SimpleUploadedFile(name, make_image(color), 'image/jpeg')


class ParseEmployeeNoTest(TestCase):
    """文件名解析的四种形态。"""

    def test_employee_no_with_name(self):
        self.assertEqual(parse_employee_no('E001_张三.jpg'), 'E001')

    def test_employee_no_only(self):
        self.assertEqual(parse_employee_no('E002.jpg'), 'E002')

    def test_employee_no_with_page_suffix(self):
        self.assertEqual(parse_employee_no('E003_1.jpg'), 'E003')
        self.assertEqual(parse_employee_no('E003-2.jpg'), 'E003')

    def test_unparseable_returns_none(self):
        self.assertIsNone(parse_employee_no('扫描件.jpg'))
        self.assertIsNone(parse_employee_no(''))

    def test_full_path_is_stripped(self):
        self.assertEqual(parse_employee_no('C:/tmp/E004_李四.png'), 'E004')


class UploadServiceTest(TestCase):
    """上传主流程。媒体目录指向临时目录，不污染项目。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.media_override = override_settings(MEDIA_ROOT=self.tmpdir)
        self.media_override.enable()

        self.org = Organization.objects.create(name='测试企业', code='t1')
        self.subject = Subject.objects.create(tenant_id=self.org.id,
                                              subject_key='sk_A')
        self.user = User.objects.create_user(
            username='E100', password='x', tenant=self.org,
            role=User.ROLE_EMPLOYEE, subject=self.subject)

    def tearDown(self):
        self.media_override.disable()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_same_employee_becomes_one_report_with_pages(self):
        files = [upload_file('E100_张三.jpg', 'white'),
                 upload_file('E100_1.jpg', 'gray')]
        result = handle_upload(files, [], self.org)

        self.assertEqual(result['created'], 1)
        report = Report.objects.get(tenant_id=self.org.id, report_no='E100')
        self.assertEqual(report.pages.count(), 2)
        self.assertEqual(report.subject_id, self.subject.id)

    def test_different_employees_create_separate_reports(self):
        files = [upload_file('E100_张三.jpg'), upload_file('E200_李四.jpg')]
        result = handle_upload(files, [], self.org)

        self.assertEqual(result['created'], 2)
        self.assertEqual(Report.objects.filter(tenant_id=self.org.id).count(), 2)

    def test_unparseable_name_goes_to_pending(self):
        files = [upload_file('E100_张三.jpg'), upload_file('扫描件.jpg')]
        result = handle_upload(files, [], self.org)

        self.assertEqual(result['created'], 1)
        self.assertEqual(result['pending'], ['扫描件.jpg'])

    def test_page_image_is_written_to_media(self):
        import os
        files = [upload_file('E100_张三.jpg')]
        handle_upload(files, [], self.org)

        page = ReportPage.objects.get(tenant_id=self.org.id)
        abs_path = os.path.join(self.tmpdir, page.image_path)
        self.assertTrue(os.path.exists(abs_path))

    def test_non_image_is_rejected(self):
        bad = SimpleUploadedFile('E300.jpg', b'not an image', 'image/jpeg')
        result = handle_upload([bad], [], self.org)

        self.assertEqual(result['created'], 0)
        self.assertTrue(result['errors'])

    def test_page_numbers_are_sequential(self):
        files = [upload_file('E100_b.jpg'), upload_file('E100_a.jpg')]
        handle_upload(files, [], self.org)

        report = Report.objects.get(tenant_id=self.org.id, report_no='E100')
        self.assertEqual(
            list(report.pages.order_by('page_no').values_list('page_no', flat=True)),
            [1, 2])

    def test_generic_filename_produces_warning(self):
        files = [upload_file('TempDragFile_20261022_094238.jpg')]
        result = handle_upload(files, [], self.org)

        self.assertTrue(result['warnings'])

    def test_normal_filename_has_no_warning(self):
        files = [upload_file('E100_张三.jpg')]
        result = handle_upload(files, [], self.org)

        self.assertEqual(result['warnings'], [])

    def test_duplicate_upload_is_skipped(self):
        handle_upload([upload_file('E100_张三.jpg')], [], self.org)
        result = handle_upload([upload_file('E100_张三.jpg')], [], self.org)

        self.assertEqual(result['created'], 0)
        self.assertTrue(any('已存在' in w for w in result['warnings']))
        self.assertEqual(
            Report.objects.filter(tenant_id=self.org.id,
                                  report_no='E100').count(), 1)

    def test_file_count_is_capped(self):
        with mock.patch('apps.reports.services.MAX_UPLOAD_FILES', 3):
            files = [upload_file('E%03d_x.jpg' % i) for i in range(5)]
            result = handle_upload(files, [], self.org)

        self.assertEqual(result['created'], 3)
        self.assertTrue(any('最多' in e for e in result['errors']))


class ViewPermissionTest(TestCase):
    """视图层权限：员工只能看本人报告，不能上传。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.media_override = override_settings(MEDIA_ROOT=self.tmpdir)
        self.media_override.enable()

        self.org = Organization.objects.create(name='测试企业', code='t1')

        self.subj_a = Subject.objects.create(tenant_id=self.org.id,
                                             subject_key='sk_A')
        self.subj_b = Subject.objects.create(tenant_id=self.org.id,
                                             subject_key='sk_B')

        self.emp_a = User.objects.create_user(
            username='A001', password='x', tenant=self.org,
            role=User.ROLE_EMPLOYEE, subject=self.subj_a)
        self.emp_b = User.objects.create_user(
            username='B001', password='x', tenant=self.org,
            role=User.ROLE_EMPLOYEE, subject=self.subj_b)
        self.manager = User.objects.create_user(
            username='M001', password='x', tenant=self.org,
            role=User.ROLE_MANAGER)

        self.report_a = Report.objects.create(
            tenant_id=self.org.id, subject=self.subj_a, report_no='A001',
            schema_version='1')
        self.report_b = Report.objects.create(
            tenant_id=self.org.id, subject=self.subj_b, report_no='B001',
            schema_version='1')

        # 这两个测试关注的是数据归属权限，不是知情同意，
        # 先替员工签好同意书，避免被 ConsentMiddleware 拦在报告页之外
        from django.utils import timezone
        from apps.accounts.models import Consent
        for u in (self.emp_a, self.emp_b):
            Consent.objects.create(
                tenant_id=self.org.id, user=u,
                consent_type='processing', granted_at=timezone.now())

    def tearDown(self):
        self.media_override.disable()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_employee_sees_only_own_reports(self):
        self.client.force_login(self.emp_a)
        resp = self.client.get('/reports/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'A001')
        self.assertNotContains(resp, 'B001')

    def test_employee_cannot_view_others_report(self):
        self.client.force_login(self.emp_a)
        resp = self.client.get('/reports/%d/' % self.report_b.id)
        self.assertEqual(resp.status_code, 403)

    def test_employee_can_view_own_report(self):
        self.client.force_login(self.emp_a)
        resp = self.client.get('/reports/%d/' % self.report_a.id)
        self.assertEqual(resp.status_code, 200)

    def test_employee_cannot_upload(self):
        self.client.force_login(self.emp_a)
        resp = self.client.get('/reports/upload/')
        self.assertEqual(resp.status_code, 403)

    def test_manager_can_upload(self):
        self.client.force_login(self.manager)
        resp = self.client.get('/reports/upload/')
        self.assertEqual(resp.status_code, 200)

    def test_manager_list_hides_employee_no(self):
        self.client.force_login(self.manager)
        resp = self.client.get('/reports/')

        self.assertEqual(resp.status_code, 200)
        # 管理层能看到报告条目，但看不到工号
        self.assertNotContains(resp, 'A001')
        self.assertNotContains(resp, 'B001')
        self.assertContains(resp, '#%d' % self.report_a.id)

    def test_manager_detail_shows_counts_only(self):
        from apps.reports.models import Item
        Item.objects.create(
            tenant_id=self.org.id, report=self.report_a, seq=1,
            name_raw='白细胞计数', name_key='白细胞计数',
            value_raw='4.65', flag='normal')

        self.client.force_login(self.manager)
        resp = self.client.get('/reports/%d/' % self.report_a.id)

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '指标概况')
        self.assertNotContains(resp, '白细胞计数')
        self.assertNotContains(resp, '4.65')

    def test_manager_cannot_download_image(self):
        self.client.force_login(self.manager)
        resp = self.client.get(
            '/reports/%d/page/1/image/' % self.report_a.id)

        self.assertEqual(resp.status_code, 403)

    def test_deleted_report_is_hidden(self):
        from django.utils import timezone
        self.report_a.deleted_at = timezone.now()
        self.report_a.save()
        self.client.force_login(self.emp_a)
        resp = self.client.get('/reports/%d/' % self.report_a.id)
        self.assertEqual(resp.status_code, 404)


class TwoStepUploadTest(TestCase):
    """两步上传：暂存不建报告，确认后才入库。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.media_override = override_settings(MEDIA_ROOT=self.tmpdir)
        self.media_override.enable()
        self.org = Organization.objects.create(name='测试企业', code='t2')

    def tearDown(self):
        self.media_override.disable()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_stage_does_not_create_report(self):
        staged = stage_upload([upload_file('E100_张三.jpg')], [], self.org)

        self.assertTrue(staged['batch'])
        self.assertEqual(staged['staged'], 1)
        self.assertEqual(
            Report.objects.filter(tenant_id=self.org.id).count(), 0)
        self.assertEqual(PendingFile.objects.count(), 1)

    def test_confirm_creates_report(self):
        staged = stage_upload([upload_file('E100_张三.jpg')], [], self.org)
        result = confirm_upload(staged['batch'], self.org)

        self.assertEqual(result['created'], 1)
        report = Report.objects.get(tenant_id=self.org.id, report_no='E100')
        self.assertEqual(report.pages.count(), 1)
        self.assertEqual(PendingFile.objects.count(), 0)

    def test_unmatched_file_stays_pending(self):
        staged = stage_upload([upload_file('扫描件.jpg')], [], self.org)
        result = confirm_upload(staged['batch'], self.org)

        self.assertEqual(result['created'], 0)
        self.assertEqual(result['pending'], ['扫描件.jpg'])
        self.assertEqual(PendingFile.objects.filter(
            state=PendingFile.STATE_UNMATCHED).count(), 1)

    def test_employee_no_can_be_given_at_confirm(self):
        staged = stage_upload([upload_file('扫描件.jpg')], [], self.org)
        pending = PendingFile.objects.get()
        result = confirm_upload(staged['batch'], self.org,
                                {pending.filename: 'E200'})

        self.assertEqual(result['created'], 1)
        self.assertTrue(Report.objects.filter(
            tenant_id=self.org.id, report_no='E200').exists())

    def test_assign_creates_new_report(self):
        stage_upload([upload_file('扫描件.jpg')], [], self.org)
        pending = PendingFile.objects.get()

        ok, err = assign_pending_file(pending, self.org, 'E300')

        self.assertTrue(ok, err)
        self.assertTrue(Report.objects.filter(
            tenant_id=self.org.id, report_no='E300').exists())
        self.assertEqual(PendingFile.objects.count(), 0)

    def test_assign_appends_page_to_existing_report(self):
        stage_upload([upload_file('E400_张三.jpg')], [], self.org)
        staged = PendingFile.objects.get(employee_no='E400')
        confirm_upload(staged.batch_id, self.org)

        stage_upload([upload_file('扫描件2.jpg')], [], self.org)
        pending = PendingFile.objects.get(employee_no='')
        ok, err = assign_pending_file(pending, self.org, 'E400')

        self.assertTrue(ok, err)
        report = Report.objects.get(tenant_id=self.org.id, report_no='E400')
        self.assertEqual(report.pages.count(), 2)

    def test_purge_removes_expired_staged(self):
        from datetime import timedelta
        from io import StringIO

        from django.core.management import call_command
        from django.utils import timezone

        stage_upload([upload_file('E500_张三.jpg')], [], self.org)
        PendingFile.objects.update(
            created_at=timezone.now() - timedelta(hours=48))

        call_command('purge_staged', hours=24, stdout=StringIO())

        self.assertEqual(PendingFile.objects.count(), 0)
