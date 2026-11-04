"""从现成的 OCR artifacts 导入报告，绕过 OCR 环节。

用途：在没有 GPU 的环境里用已有识别结果做演示或回归，
走的是解析、判定、分级、入库这几段真实链路。

用法：
    python manage.py import_artifacts --src artifacts_self
    python manage.py import_artifacts --src artifacts --images images
    python manage.py import_artifacts --src artifacts_self --limit 3 --no-explain
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.core.models import Organization
from apps.core.utils import make_subject_key, parse_employee_no
from apps.pipeline.importers import import_explanation, import_items
from apps.reports.models import Report, ReportPage, Subject


class Command(BaseCommand):
    help = '从已有 OCR artifacts 导入报告并完成解析、判定、分级与入库'

    def add_arguments(self, parser):
        parser.add_argument('--src', required=True,
                            help='artifacts 目录，内含每份一个 json')
        parser.add_argument('--images', default='',
                            help='原始图片目录，用于详情页展示，可省略')
        parser.add_argument('--org', default='default',
                            help='组织编码，默认 default')
        parser.add_argument('--limit', type=int, default=0,
                            help='最多导入几份，0 表示全部')
        parser.add_argument('--no-explain', action='store_true',
                            help='跳过解读生成')

    def handle(self, *args, **options):
        src = options['src']
        if not os.path.isdir(src):
            raise CommandError('目录不存在：%s' % src)

        org = Organization.objects.filter(code=options['org']).first()
        if org is None:
            raise CommandError('组织不存在：%s' % options['org'])

        tmpdir = tempfile.mkdtemp(prefix='import_')
        try:
            pred, flagged, severity, expl = self._run_chain(
                src, tmpdir, skip_explain=options['no_explain'])
            if flagged is None:
                return

            rows = self._read_jsonl(flagged)
            if options['limit']:
                rows = rows[:options['limit']]
            if not rows:
                self.stdout.write('没有可导入的记录')
                return

            sev_rows = self._read_jsonl(severity) if severity else []
            sev_by_report = {r.get('report_id'): r for r in sev_rows}

            imported = 0
            items_total = 0
            for row in rows:
                rid = row.get('report_id') or row.get('image') or ''
                if not rid:
                    continue
                report, created = self._ensure_report(
                    org, rid, options['images'])
                n = import_items(
                    report, rows=[row],
                    severity_path=self._write_severity(
                        tmpdir, rid, sev_by_report) if sev_by_report else '')
                items_total += n
                imported += 1
                self.stdout.write('  %-40s %3d 项%s'
                                  % (rid[:38], n, '（新建）' if created else ''))

            if expl:
                self._import_explanations(tmpdir, org, expl)

            self.stdout.write(self.style.SUCCESS(
                '导入 %d 份报告，共 %d 项指标' % (imported, items_total)))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # ---------- 内部步骤 ----------

    def _read_jsonl(self, path):
        out = []
        if not path or not os.path.exists(path):
            return out
        with open(path, encoding='utf-8') as fp:
            for line in fp:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return out

    def _script(self, name, args, timeout=1800):
        path = os.path.join(settings.BASE_DIR, 'tools', name)
        if not os.path.exists(path):
            raise CommandError('脚本不存在：%s' % name)
        proc = subprocess.run(
            [sys.executable, path] + list(args),
            capture_output=True, text=True, timeout=timeout,
            encoding='utf-8', errors='replace')
        return proc.returncode == 0, (proc.stdout or '')[-400:] \
            + (proc.stderr or '')[-400:]

    def _run_chain(self, src, tmpdir, skip_explain=False):
        pred = os.path.join(tmpdir, 'predictions.jsonl')
        flagged = os.path.join(tmpdir, 'flagged.jsonl')
        severity = os.path.join(tmpdir, 'severity.jsonl')
        expl = os.path.join(tmpdir, 'explanations.jsonl')

        self.stdout.write('解析 artifacts...')
        ok, msg = self._script('04_parse_predictions.py',
                               ['--artifacts', src, '--out', pred])
        if not ok:
            self.stdout.write(self.style.ERROR('解析失败：%s' % msg))
            return None, None, None, None

        self.stdout.write('判定异常...')
        ok, msg = self._script('31_judge_abnormal.py',
                               ['--predictions', pred, '--out', flagged])
        if not ok:
            self.stdout.write(self.style.ERROR('判定失败：%s' % msg))
            return None, None, None, None

        self.stdout.write('计算严重度...')
        ok, msg = self._script('45_grade_severity.py',
                               ['--predictions', flagged, '--out', severity])
        if not ok:
            self.stdout.write(self.style.WARNING('分级失败，继续：%s' % msg))
            severity = None

        if skip_explain:
            return pred, flagged, severity, None

        self.stdout.write('生成解读（模板模式，不调模型）...')
        ok, msg = self._script('35_llm_explain.py',
                               ['--predictions', flagged, '--out', expl,
                                '--template'])
        if not ok:
            self.stdout.write(self.style.WARNING('解读失败，继续：%s' % msg))
            expl = None

        return pred, flagged, severity, expl

    def _write_severity(self, tmpdir, report_id, sev_by_report):
        """把某一份报告的分级记录单独写一个文件，供 import_items 读取。"""
        row = sev_by_report.get(report_id)
        if not row:
            return ''
        path = os.path.join(tmpdir, 'sev_%s.jsonl' % abs(hash(report_id)))
        with open(path, 'w', encoding='utf-8') as fp:
            fp.write(json.dumps(row, ensure_ascii=False) + '\n')
        return path

    def _ensure_report(self, org, report_id, images_dir):
        """建或复用报告记录，并在能找到原图时建立页记录。

        报告号直接用 report_id 去扩展名，不走 parse_employee_no：
        artifacts 的 report_id 本身就是报告标识（可能形如 TempDragFile_日期_序号），
        按"工号_姓名"规则解析会把多份报告归并成同一份。
        """
        report_no = (os.path.splitext(report_id)[0] or report_id)[:64]
        report = Report.objects.filter(
            tenant_id=org.id, report_no=report_no,
            deleted_at__isnull=True).first()
        created = False
        if report is None:
            key = make_subject_key(report_no)
            subject, _ = Subject.objects.get_or_create(
                tenant_id=org.id, subject_key=key)
            report = Report.objects.create(
                tenant_id=org.id, subject=subject, report_no=report_no,
                schema_version='1', status=Report.STATUS_DONE)
            created = True

        if images_dir and not report.pages.exists():
            self._attach_image(org, report, report_id, images_dir)

        return report, created

    def _attach_image(self, org, report, report_id, images_dir):
        """把原图复制进 media 并建页记录。找不到时跳过，不阻断导入。"""
        candidates = []
        for ext in ('', '.jpg', '.jpeg', '.png', '.bmp', '.tif', '.tiff'):
            candidates.append(os.path.join(images_dir, report_id + ext))
        src_path = next((p for p in candidates if os.path.exists(p)), None)
        if not src_path:
            return False

        rel_dir = os.path.join(org.code, timezone.now().strftime('%Y%m'))
        abs_dir = os.path.join(settings.MEDIA_ROOT, rel_dir)
        os.makedirs(abs_dir, exist_ok=True)
        name = '%s%s' % (report_id, os.path.splitext(src_path)[1].lower())
        rel_path = os.path.join(rel_dir, name).replace('\\', '/')
        shutil.copyfile(src_path, os.path.join(settings.MEDIA_ROOT, rel_path))

        ReportPage.objects.create(
            tenant_id=org.id, report=report, page_no=1,
            image_path=rel_path)
        return True

    def _import_explanations(self, tmpdir, org, expl_path):
        rows = self._read_jsonl(expl_path)
        for row in rows:
            rid = row.get('report_id') or ''
            report_no = (os.path.splitext(rid)[0] or rid)[:64]
            report = Report.objects.filter(
                tenant_id=org.id, report_no=report_no,
                deleted_at__isnull=True).first()
            if report is None:
                continue
            single = os.path.join(tmpdir, 'exp_single.jsonl')
            with open(single, 'w', encoding='utf-8') as fp:
                fp.write(json.dumps(row, ensure_ascii=False) + '\n')
            import_explanation(report, single)
