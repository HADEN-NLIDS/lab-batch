"""上传处理：校验、落盘、创建报告与页记录。"""

import os
import uuid
from datetime import datetime

from django.conf import settings
from django.db import transaction

from apps.accounts.models import User
from apps.core.utils import (make_subject_key, open_image_stream,
                             parse_employee_no, read_file_bytes,
                             safe_extract_zip, validate_image)
from apps.reports.models import Report, ReportPage, Subject

# 单次上传的文件数上限。所有图片都会读进内存，不设限时一次选几百张会吃满内存。
MAX_UPLOAD_FILES = 200

# 截图与传输工具常用的默认文件名前缀。这类名字解析出来的"工号"其实是工具名，
# 同一批图会被错误归并到同一个人名下。
GENERIC_NAME_PREFIXES = (
    'tempdragfile', 'temp', 'tmp', 'image', 'img', 'screenshot', 'scan',
    '微信图片', '图片', '截图', 'download', 'photo', 'wx_camera',
)


def looks_like_generic_name(employee_no):
    low = (employee_no or '').strip().lower()
    return bool(low) and any(low.startswith(p) for p in GENERIC_NAME_PREFIXES)


def _save_image(data, ext, tenant_code):
    """把图片写到 media 下，返回相对路径。用 uuid 命名避免重名。"""
    rel_dir = os.path.join(tenant_code, datetime.now().strftime('%Y%m'))
    abs_dir = os.path.join(settings.MEDIA_ROOT, rel_dir)
    os.makedirs(abs_dir, exist_ok=True)
    name = '%s.%s' % (uuid.uuid4().hex, ext or 'jpg')
    rel_path = os.path.join(rel_dir, name).replace('\\', '/')
    with open(os.path.join(settings.MEDIA_ROOT, rel_path), 'wb') as fp:
        fp.write(data)
    return rel_path


def _resolve_subject(tenant, employee_no):
    """按工号找受检者。优先用员工账号上已绑定的 subject，否则按假名查或建。"""
    user = User.objects.filter(tenant=tenant, username=employee_no,
                               deleted_at__isnull=True).first()
    if user and user.subject_id:
        return user.subject

    key = make_subject_key(employee_no)
    subject, _ = Subject.objects.get_or_create(
        tenant_id=tenant.id, subject_key=key)
    return subject


def collect_files(image_files, zip_files):
    """把多选图片与压缩包统一成 [(文件名, 字节)]，同时收集错误。"""
    collected, errors = [], []
    overflow = False

    def full():
        return len(collected) >= MAX_UPLOAD_FILES

    for f in image_files or []:
        if full():
            overflow = True
            break
        data = read_file_bytes(f)
        if not data:
            errors.append('%s：超过单文件大小上限' % f.name)
            continue
        ok, msg = validate_image(open_image_stream(data))
        if not ok:
            errors.append('%s：%s' % (f.name, msg))
            continue
        collected.append((f.name, data))

    if not overflow:
        for zf in zip_files or []:
            if full():
                overflow = True
                break
            entries, err = safe_extract_zip(zf)
            if err:
                errors.append('%s：%s' % (zf.name, err))
                continue
            for name, data in entries:
                if full():
                    overflow = True
                    break
                ok, msg = validate_image(open_image_stream(data))
                if not ok:
                    errors.append('%s：%s' % (name, msg))
                    continue
                collected.append((name, data))

    if overflow:
        errors.append('单次上传最多 %d 个文件，超出部分已忽略'
                      % MAX_UPLOAD_FILES)

    return collected, errors


def handle_upload(image_files, zip_files, tenant):
    """处理一次上传。

    返回 dict：
      created  已创建的报告数量
      reports  创建的报告 id 列表
      pending  解析不出工号的文件名，需人工匹配
      errors   被拒绝的文件与原因
    """
    collected, errors = collect_files(image_files, zip_files)

    groups = {}
    pending = []
    for name, data in collected:
        emp = parse_employee_no(name)
        if emp:
            groups.setdefault(emp, []).append((name, data))
        else:
            pending.append(name)

    warnings = []
    generic = sorted(e for e in groups if looks_like_generic_name(e))
    if generic:
        warnings.append(
            '以下文件名不像工号，同批图片可能被归并到同一个人：%s。'
            '建议改名为「工号_姓名」后重新上传。'
            % '、'.join(generic[:5]))

    created_ids = []
    for emp, items in groups.items():
        if Report.objects.filter(tenant_id=tenant.id, report_no=emp,
                                 deleted_at__isnull=True).exists():
            warnings.append('%s 的报告已存在，本次跳过' % emp)
            continue
        subject = _resolve_subject(tenant, emp)
        # 同一工号的多张图按文件名排序作为多页
        items.sort(key=lambda x: x[0])
        try:
            with transaction.atomic():
                report = Report.objects.create(
                    tenant_id=tenant.id,
                    subject=subject,
                    report_no=emp,
                    schema_version='1',
                    status=Report.STATUS_UPLOADED,
                )
                for idx, (name, data) in enumerate(items, 1):
                    ext = os.path.splitext(name)[1].lower().lstrip('.')
                    rel_path = _save_image(data, ext, tenant.code)
                    ReportPage.objects.create(
                        tenant_id=tenant.id,
                        report=report,
                        page_no=idx,
                        image_path=rel_path,
                    )
                created_ids.append(report.id)

                # 报告建好后入队，等待 worker 处理
                from apps.pipeline.services import enqueue
                enqueue(report, 'ocr')
        except Exception as e:
            errors.append('%s：保存失败 %s' % (emp, str(e)[:60]))

    return {
        'created': len(created_ids),
        'reports': created_ids,
        'pending': pending,
        'errors': errors,
        'warnings': warnings,
        'total_files': len(collected),
    }
