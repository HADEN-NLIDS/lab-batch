"""任务队列：创建、取任务、状态流转、执行分发。"""

import os
import shutil
import subprocess
import sys
from datetime import timedelta

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from apps.pipeline.models import Task
from apps.reports.models import Report

# 处理中间产物的根目录，与用户上传的 media 分开
VAR_ROOT = os.path.join(settings.BASE_DIR, 'var')

# 任务类型到处理脚本的映射
SCRIPT_MAP = {
    'ocr': '03_export_ocr.py',
    'parse': '04_parse_predictions.py',
    'judge': '31_judge_abnormal.py',
    'explain': '35_llm_explain.py',
}


def work_dir(report_id):
    """某份报告的工作目录，存放 OCR 与解析中间产物。"""
    path = os.path.join(VAR_ROOT, 'work', str(report_id))
    os.makedirs(path, exist_ok=True)
    return path


def prepare_ocr_images(report, wdir):
    """把报告的页图汇总到一个目录，返回 (图片目录, 错误说明)。

    识别脚本按目录扫描，一张图对应一份产物，文件名即产物里的 report_id。
    这里统一命名为 page_01、page_02，多页按页序排列，后续合并时顺序稳定。
    """
    pages = list(report.pages.order_by('page_no'))
    if not pages:
        return '', '没有页记录，无法识别'

    img_dir = os.path.join(wdir, 'images')
    shutil.rmtree(img_dir, ignore_errors=True)
    os.makedirs(img_dir, exist_ok=True)

    for page in pages:
        if not page.image_path:
            return '', '第 %d 页没有图片路径' % page.page_no
        src = os.path.join(settings.MEDIA_ROOT, page.image_path)
        if not os.path.exists(src):
            return '', '第 %d 页原图已不存在' % page.page_no
        ext = os.path.splitext(src)[1].lower() or '.jpg'
        shutil.copyfile(
            src, os.path.join(img_dir, 'page_%02d%s' % (page.page_no, ext)))
    return img_dir, ''


def enqueue(report, task_type, delay_seconds=0):
    """为报告创建一个待处理任务。已存在同类型未完成任务时不重复创建。"""
    exists = Task.objects.filter(
        report_id=report.id, task_type=task_type,
        status__in=(Task.STATUS_PENDING, Task.STATUS_RUNNING)).exists()
    if exists:
        return None

    task = Task.objects.create(
        tenant_id=report.tenant_id,
        report_id=report.id,
        task_type=task_type,
        scheduled_at=timezone.now() + timedelta(seconds=delay_seconds),
    )
    if report.status != Report.STATUS_PROCESSING:
        # 进入处理态，列表页据此显示进度
        report.status = Report.STATUS_PROCESSING
        report.save(update_fields=['status'])
    return task


def pick_task(worker_id):
    """取一个到期任务并置为 running。

    用 MySQL 8.0 的 SKIP LOCKED，多 worker 并发取不会重复也不会互相阻塞。

    时间一律由 Django 传入，不用数据库的 NOW()：USE_TZ=True 时 DATETIME 按
    UTC 存储，而 NOW() 返回会话时区的本地时间，直接比较会差出时区偏移。
    """
    now = timezone.now()
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("""
                SELECT id FROM task
                 WHERE status = 'pending' AND scheduled_at <= %s
                 ORDER BY scheduled_at
                 LIMIT 1
                 FOR UPDATE SKIP LOCKED
            """, [now])
            row = cur.fetchone()
            if row is None:
                return None
            task_id = row[0]
            cur.execute("""
                UPDATE task
                   SET status = 'running', worker_id = %s,
                       started_at = %s, attempts = attempts + 1
                 WHERE id = %s AND status = 'pending'
            """, [worker_id, now, task_id])
    return Task.objects.filter(id=task_id).first()


def mark_done(task):
    task.status = Task.STATUS_DONE
    task.finished_at = timezone.now()
    task.save(update_fields=['status', 'finished_at'])
    _on_success(task)


def mark_failed(task, error_code, error_msg):
    """失败后按退避策略退回 pending，达到上限才置为 failed。"""
    task.error_code = str(error_code)[:50]
    task.error_msg = str(error_msg)[:2000]
    if task.attempts < task.max_attempts:
        task.status = Task.STATUS_PENDING
        task.scheduled_at = timezone.now() + timedelta(
            seconds=30 * task.attempts)
    else:
        task.status = Task.STATUS_FAILED
        task.finished_at = timezone.now()
        # 重试耗尽才算整体失败，中途重试仍保持处理中
        Report.objects.filter(id=task.report_id).update(
            status=Report.STATUS_FAILED)
    task.save(update_fields=['status', 'error_code', 'error_msg',
                            'scheduled_at', 'finished_at'])


def _on_success(task):
    """一个环节成功后，串联下一环节。"""
    report = task.report
    if task.task_type == 'ocr':
        enqueue(report, 'parse')
    elif task.task_type == 'parse':
        enqueue(report, 'judge')
    elif task.task_type == 'judge':
        enqueue(report, 'explain')
    elif task.task_type == 'explain':
        report.status = Report.STATUS_DONE
        report.save(update_fields=['status'])


def run_script(script_name, args, timeout=1800):
    """调用 tools 下的脚本，返回 (成功, 输出摘要)。

    用当前解释器执行，确保与 Django 处在同一个虚拟环境。
    """
    script = os.path.join(settings.BASE_DIR, 'tools', script_name)
    if not os.path.exists(script):
        return False, '脚本不存在：%s' % script_name

    cmd = [sys.executable, script] + list(args)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            encoding='utf-8', errors='replace')
    except subprocess.TimeoutExpired:
        return False, '执行超时（%d 秒）' % timeout

    tail = (proc.stdout or '')[-500:] + (proc.stderr or '')[-500:]
    return proc.returncode == 0, tail.strip()


def process(task):
    """执行一个任务。返回 (成功, 错误码, 错误信息)。"""
    task_type = task.task_type
    script = SCRIPT_MAP.get(task_type)
    if not script:
        return False, 'unknown_type', '未知任务类型：%s' % task_type

    report = task.report
    wdir = work_dir(report.id)

    if task_type == 'ocr':
        # 识别依赖 GPU 与 PaddlePaddle，先做前置检查，给出比脚本崩溃更清楚的原因
        try:
            import paddle  # noqa: F401
        except ImportError:
            return (False, 'no_gpu',
                    '当前环境没有 PaddlePaddle，OCR 需在 GPU 环境下执行')

        img_dir, err = prepare_ocr_images(report, wdir)
        if err:
            return False, 'no_images', err

        artifacts = os.path.join(wdir, 'artifacts')
        shutil.rmtree(artifacts, ignore_errors=True)
        ok, msg = run_script(script, [
            '--scan-src', '--src', img_dir, '--out-dir', artifacts,
            '--limit', str(max(report.pages.count(), 1)),
        ])
        if not ok:
            return False, 'ocr_failed', msg

        produced = []
        if os.path.isdir(artifacts):
            produced = [f for f in os.listdir(artifacts)
                        if f.endswith('.json') and not f.startswith('_')]
        if not produced:
            return False, 'ocr_empty', '识别未产出结果：%s' % msg
        return True, '', '识别 %d 页' % len(produced)

    if task_type == 'parse':
        artifacts = os.path.join(wdir, 'artifacts')
        if not os.path.isdir(artifacts):
            return False, 'no_artifacts', '缺少 OCR 产物目录'
        out = os.path.join(wdir, 'predictions.jsonl')
        ok, msg = run_script(script, ['--artifacts', artifacts, '--out', out])
        return ok, '' if ok else 'parse_failed', msg

    if task_type == 'judge':
        pred = os.path.join(wdir, 'predictions.jsonl')
        if not os.path.exists(pred):
            return False, 'no_predictions', '缺少解析结果'
        flagged = os.path.join(wdir, 'flagged.jsonl')
        ok, msg = run_script(script, ['--predictions', pred, '--out', flagged])
        if not ok:
            return False, 'judge_failed', msg

        # 分级是判定的补充，失败不阻断入库，severity 留空即可
        severity = os.path.join(wdir, 'severity.jsonl')
        ok_sev, _ = run_script('45_grade_severity.py',
                               ['--predictions', flagged,
                                '--out', severity])

        from apps.pipeline.importers import import_items
        n = import_items(report, flagged, severity if ok_sev else '')
        return True, '', '写入 %d 项指标' % n

    if task_type == 'explain':
        flagged = os.path.join(wdir, 'flagged.jsonl')
        if not os.path.exists(flagged):
            return False, 'no_flagged', '缺少判定结果'
        out = os.path.join(wdir, 'explanations.jsonl')
        args = ['--predictions', flagged, '--out', out]
        if not os.environ.get('LLM_API_KEY'):
            # 未配置模型时按模板输出，否则每个任务都会因缺 key 失败
            args.append('--template')
        ok, msg = run_script(script, args)
        if not ok:
            return False, 'explain_failed', msg

        from apps.pipeline.importers import import_explanation
        exp = import_explanation(report, out)
        return True, '', '写入解读 %s' % (exp.id if exp else '无')

    return False, 'unknown_type', '未处理的任务类型'
