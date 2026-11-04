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
from apps.reports.models import (PendingFile, Report, ReportPage, Subject)

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


def _create_report(tenant, emp, pages, errors, warnings):
    """按工号建一份报告。pages 为 [(文件名, 图片相对路径)]。"""
    if Report.objects.filter(tenant_id=tenant.id, report_no=emp,
                             deleted_at__isnull=True).exists():
        warnings.append('%s 的报告已存在，本次跳过' % emp)
        return None

    subject = _resolve_subject(tenant, emp)
    # 同一工号的多张图按文件名排序作为多页
    pages = sorted(pages, key=lambda x: x[0])
    try:
        with transaction.atomic():
            report = Report.objects.create(
                tenant_id=tenant.id,
                subject=subject,
                report_no=emp,
                schema_version='1',
                status=Report.STATUS_UPLOADED,
            )
            for idx, (name, rel_path) in enumerate(pages, 1):
                ReportPage.objects.create(
                    tenant_id=tenant.id,
                    report=report,
                    page_no=idx,
                    image_path=rel_path,
                )
        from apps.pipeline.services import enqueue
        enqueue(report, 'ocr')
        return report
    except Exception as e:
        errors.append('%s：保存失败 %s' % (emp, str(e)[:60]))
        return None


def stage_upload(image_files, zip_files, tenant):
    """第一步：校验并暂存，先不建报告。

    文件落到 media，行写进 pending_file，等用户看过清单再确认。
    """
    collected, errors = collect_files(image_files, zip_files)
    if not collected:
        return {'batch': '', 'staged': 0, 'groups': [], 'unmatched': [],
                'errors': errors, 'warnings': []}

    batch = uuid.uuid4().hex[:16]
    groups = {}
    unmatched = []
    for name, data in collected:
        emp = parse_employee_no(name)
        if emp:
            groups.setdefault(emp, []).append(name)
        else:
            unmatched.append(name)

        ext = os.path.splitext(name)[1].lower().lstrip('.')
        rel_path = _save_image(data, ext, tenant.code)
        PendingFile.objects.create(
            tenant_id=tenant.id,
            batch_id=batch,
            filename=name[:128],
            image_path=rel_path,
            employee_no=emp or '',
            size=len(data),
            state=(PendingFile.STATE_STAGED if emp
                   else PendingFile.STATE_UNMATCHED),
        )

    warnings = []
    generic = sorted(e for e in groups if looks_like_generic_name(e))
    if generic:
        warnings.append(
            '以下文件名不像工号，同批图片可能被归并到同一个人：%s。'
            '建议改名为「工号_姓名」后重新上传。'
            % '、'.join(generic[:5]))

    return {
        'batch': batch,
        'staged': len(collected),
        'groups': [(e, len(n)) for e, n in sorted(groups.items())],
        'unmatched': unmatched,
        'errors': errors,
        'warnings': warnings,
    }


def confirm_upload(batch_id, tenant, assignments=None):
    """第二步：把暂存的文件入库。

    assignments 是 {文件名: 工号}，用于给解析不出工号的文件人工指定。
    没有工号也没有指定的文件保持 unmatched 状态，留给待匹配页处理。
    """
    assignments = assignments or {}
    files = list(PendingFile.objects.filter(
        tenant_id=tenant.id, batch_id=batch_id).order_by('filename'))
    if not files:
        return {'created': 0, 'reports': [], 'pending': [],
                'errors': ['没有待确认的文件'], 'warnings': []}

    errors, warnings = [], []
    groups, kept = {}, []
    for f in files:
        emp = (assignments.get(f.filename) or f.employee_no or '').strip()
        if emp:
            groups.setdefault(emp, []).append((f.filename, f.image_path))
        else:
            kept.append(f)

    created_ids = []
    for emp, pages in groups.items():
        report = _create_report(tenant, emp, pages, errors, warnings)
        if report is not None:
            created_ids.append(report.id)

    done_ids = [f.id for f in files if f not in kept]
    PendingFile.objects.filter(id__in=done_ids).delete()

    return {
        'created': len(created_ids),
        'reports': created_ids,
        'pending': [f.filename for f in kept],
        'errors': errors,
        'warnings': warnings,
    }


def handle_upload(image_files, zip_files, tenant):
    """一步完成上传与入库，供脚本命令行与测试使用。

    页面走的是 stage_upload 加 confirm_upload 两步，中间能看到确认清单。
    """
    staged = stage_upload(image_files, zip_files, tenant)
    if not staged['batch']:
        return {'created': 0, 'reports': [], 'pending': [],
                'errors': staged['errors'], 'warnings': staged['warnings'],
                'total_files': 0}

    result = confirm_upload(staged['batch'], tenant)
    result['errors'] = staged['errors'] + result['errors']
    result['warnings'] = staged['warnings'] + result['warnings']
    result['pending'] = staged['unmatched']
    result['total_files'] = staged['staged']
    # 一步式调用不留暂存痕迹
    PendingFile.objects.filter(
        tenant_id=tenant.id, batch_id=staged['batch']).delete()
    return result


def assign_pending_file(pending, tenant, employee_no):
    """给一个待匹配文件指定工号并入库。

    该工号已有报告时追加为新的一页，否则新建报告。
    返回 (成功, 错误说明)。
    """
    employee_no = (employee_no or '').strip()
    if not employee_no:
        return False, '工号不能为空'

    report = Report.objects.filter(
        tenant_id=tenant.id, report_no=employee_no,
        deleted_at__isnull=True).first()

    if report is None:
        errors, warnings = [], []
        report = _create_report(
            tenant, employee_no, [(pending.filename, pending.image_path)],
            errors, warnings)
        if report is None:
            return False, (errors or warnings or ['创建报告失败'])[0]
    else:
        ReportPage.objects.create(
            tenant_id=tenant.id, report=report,
            page_no=report.pages.count() + 1,
            image_path=pending.image_path)

    pending.delete()
    return True, ''
