"""任务队列的回归测试：入队去重、取任务、状态流转、失败重试。"""

import os
import shutil
import tempfile
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.models import Organization
from apps.pipeline import services
from apps.pipeline.models import Task
from apps.reports.models import Report, ReportPage


class TaskQueueTest(TestCase):

    def setUp(self):
        self.org = Organization.objects.create(name='测试企业', code='t1')
        self.report = Report.objects.create(
            tenant_id=self.org.id, report_no='R001', schema_version='1')

    def test_enqueue_creates_pending_task(self):
        task = services.enqueue(self.report, 'ocr')
        self.assertIsNotNone(task)
        self.assertEqual(task.status, Task.STATUS_PENDING)
        self.assertEqual(task.task_type, 'ocr')
        self.assertEqual(task.tenant_id, self.org.id)

    def test_enqueue_marks_report_processing(self):
        services.enqueue(self.report, 'ocr')
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, Report.STATUS_PROCESSING)

    def test_enqueue_skips_duplicate(self):
        first = services.enqueue(self.report, 'ocr')
        second = services.enqueue(self.report, 'ocr')
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(
            Task.objects.filter(report=self.report, task_type='ocr').count(), 1)

    def test_pick_marks_running_and_counts_attempt(self):
        task = services.enqueue(self.report, 'ocr')
        picked = services.pick_task('worker-1')
        self.assertEqual(picked.id, task.id)

        picked.refresh_from_db()
        self.assertEqual(picked.status, Task.STATUS_RUNNING)
        self.assertEqual(picked.worker_id, 'worker-1')
        self.assertEqual(picked.attempts, 1)
        self.assertIsNotNone(picked.started_at)

    def test_pick_ignores_future_schedule(self):
        services.enqueue(self.report, 'ocr', delay_seconds=60)
        self.assertIsNone(services.pick_task('worker-1'))

    def test_pick_empty_queue_returns_none(self):
        self.assertIsNone(services.pick_task('worker-1'))

    def test_done_chains_next_stage(self):
        services.enqueue(self.report, 'ocr')
        task = services.pick_task('worker-1')
        services.mark_done(task)

        task.refresh_from_db()
        self.assertEqual(task.status, Task.STATUS_DONE)
        self.assertTrue(
            Task.objects.filter(report=self.report,
                                task_type='parse').exists())

    def test_explain_completion_marks_report_done(self):
        services.enqueue(self.report, 'explain')
        task = services.pick_task('worker-1')
        services.mark_done(task)

        self.report.refresh_from_db()
        self.assertEqual(self.report.status, Report.STATUS_DONE)

    def test_fail_requeues_with_backoff(self):
        services.enqueue(self.report, 'ocr')
        task = services.pick_task('worker-1')
        services.mark_failed(task, 'x', 'boom')

        task.refresh_from_db()
        self.assertEqual(task.status, Task.STATUS_PENDING)
        self.assertGreater(task.scheduled_at, timezone.now())
        self.assertEqual(task.error_code, 'x')

    def test_fail_exhausts_attempts(self):
        task = services.enqueue(self.report, 'ocr')
        task.max_attempts = 1
        task.save(update_fields=['max_attempts'])

        picked = services.pick_task('worker-1')
        services.mark_failed(picked, 'x', 'boom')

        picked.refresh_from_db()
        self.assertEqual(picked.status, Task.STATUS_FAILED)
        self.assertIsNotNone(picked.finished_at)

    def test_exhausted_failure_marks_report_failed(self):
        task = services.enqueue(self.report, 'ocr')
        task.max_attempts = 1
        task.save(update_fields=['max_attempts'])

        picked = services.pick_task('worker-1')
        services.mark_failed(picked, 'x', 'boom')

        self.report.refresh_from_db()
        self.assertEqual(self.report.status, Report.STATUS_FAILED)

    def test_process_reports_missing_prerequisite(self):
        """前置产物缺失时不抛异常，返回可记录的错误。"""
        services.enqueue(self.report, 'parse')
        task = services.pick_task('worker-1')
        ok, code, msg = services.process(task)

        self.assertFalse(ok)
        self.assertEqual(code, 'no_artifacts')

    def test_ocr_without_paddle_reports_clear_reason(self):
        services.enqueue(self.report, 'ocr')
        task = services.pick_task('worker-1')
        ok, code, msg = services.process(task)

        self.assertFalse(ok)
        # 本机无 PaddlePaddle 时应给出明确原因，而不是抛异常
        self.assertIn(code, ('no_gpu', 'no_images'))
        self.assertTrue(msg)


class OcrStageTest(TestCase):
    """识别环节的图片汇总与脚本调用。"""

    def setUp(self):
        self.org = Organization.objects.create(name='测试企业', code='t2')
        self.media = tempfile.mkdtemp(prefix='media_')
        self.addCleanup(shutil.rmtree, self.media, True)
        override = override_settings(MEDIA_ROOT=self.media)
        override.enable()
        self.addCleanup(override.disable)

        self.report = Report.objects.create(
            tenant_id=self.org.id, report_no='R100', schema_version='1')
        self.page = ReportPage.objects.create(
            tenant_id=self.org.id, report=self.report, page_no=1,
            image_path='originals/a.jpg')
        os.makedirs(os.path.join(self.media, 'originals'), exist_ok=True)
        with open(os.path.join(self.media, 'originals', 'a.jpg'), 'wb') as fp:
            fp.write(b'\xff\xd8\xff')

        self.wdir = services.work_dir(self.report.id)
        self.addCleanup(shutil.rmtree, self.wdir, True)

    def test_prepare_copies_and_renames(self):
        img_dir, err = services.prepare_ocr_images(self.report, self.wdir)
        self.assertEqual(err, '')
        self.assertTrue(os.path.isfile(
            os.path.join(img_dir, 'page_01.jpg')))

    def test_prepare_without_pages(self):
        report = Report.objects.create(
            tenant_id=self.org.id, report_no='R101', schema_version='1')
        img_dir, err = services.prepare_ocr_images(report, self.wdir)
        self.assertEqual(img_dir, '')
        self.assertIn('没有页记录', err)

    def test_prepare_reports_missing_file(self):
        self.page.image_path = 'originals/gone.jpg'
        self.page.save(update_fields=['image_path'])
        img_dir, err = services.prepare_ocr_images(self.report, self.wdir)
        self.assertEqual(img_dir, '')
        self.assertIn('不存在', err)

    def test_ocr_calls_script_in_scan_mode(self):
        services.enqueue(self.report, 'ocr')
        task = services.pick_task('w1')
        calls = []

        def fake_run(script, args, timeout=1800):
            calls.append((script, list(args)))
            out_dir = args[args.index('--out-dir') + 1]
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(out_dir, 'page_01.json'), 'w') as fp:
                fp.write('{}')
            return True, 'ok'

        with mock.patch.dict('sys.modules', {'paddle': mock.MagicMock()}):
            with mock.patch.object(services, 'run_script', fake_run):
                ok, code, msg = services.process(task)

        self.assertTrue(ok, msg)
        self.assertEqual(calls[0][0], '03_export_ocr.py')
        self.assertIn('--scan-src', calls[0][1])
        self.assertIn('--src', calls[0][1])

    def test_ocr_without_output_reports_empty(self):
        services.enqueue(self.report, 'ocr')
        task = services.pick_task('w1')

        def fake_run(script, args, timeout=1800):
            os.makedirs(args[args.index('--out-dir') + 1], exist_ok=True)
            return True, 'ok'

        with mock.patch.dict('sys.modules', {'paddle': mock.MagicMock()}):
            with mock.patch.object(services, 'run_script', fake_run):
                ok, code, msg = services.process(task)

        self.assertFalse(ok)
        self.assertEqual(code, 'ocr_empty')


class ExplainStageTest(TestCase):
    """解读环节在未配置模型时应自动降级为模板。"""

    def setUp(self):
        self.org = Organization.objects.create(name='测试企业', code='t3')
        self.report = Report.objects.create(
            tenant_id=self.org.id, report_no='R200', schema_version='1')
        self.wdir = services.work_dir(self.report.id)
        self.addCleanup(shutil.rmtree, self.wdir, True)
        with open(os.path.join(self.wdir, 'flagged.jsonl'), 'w') as fp:
            fp.write('')

    def _run_explain(self):
        services.enqueue(self.report, 'explain')
        task = services.pick_task('w1')
        seen = {}

        def fake_run(script, args, timeout=1800):
            seen['args'] = list(args)
            return False, 'stop here'

        with mock.patch.object(services, 'run_script', fake_run):
            services.process(task)
        return seen

    def test_uses_template_when_key_absent(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('LLM_API_KEY', None)
            seen = self._run_explain()
        self.assertIn('--template', seen['args'])

    def test_calls_model_when_key_present(self):
        with mock.patch.dict(os.environ, {'LLM_API_KEY': 'dummy'}):
            seen = self._run_explain()
        self.assertNotIn('--template', seen['args'])
