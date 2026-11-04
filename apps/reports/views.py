import os
import re

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from apps.reports.forms import UploadForm
from apps.reports.models import PendingFile, Report
from apps.reports.services import (assign_pending_file, confirm_upload,
                                   stage_upload)

PAGE_SIZE = 20


def _can_manage(user):
    """管理层与运维：可上传、重新处理、看任务状态与群体统计。"""
    return user.role in ('manager', 'admin')


def _personal_only(user):
    """只有受检员工本人能看到报告的具体内容。"""
    return user.role == 'employee'


@login_required
def report_list(request):
    if request.tenant is None:
        raise PermissionDenied('账号未绑定组织')

    qs = Report.objects.filter(tenant_id=request.tenant.id,
                               deleted_at__isnull=True)
    if _personal_only(request.user):
        if request.user.subject_id is None:
            # 常见于建账号时漏绑体检人，空列表看不出原因
            messages.warning(request, '账号尚未绑定体检记录，请联系管理员')
            qs = qs.none()
        else:
            qs = qs.filter(subject_id=request.user.subject_id)

    qs = qs.prefetch_related('pages').order_by('-created_at')
    running = qs.filter(status__in=(Report.STATUS_UPLOADED,
                                    Report.STATUS_PROCESSING)).count()
    failed = qs.filter(status=Report.STATUS_FAILED).count()
    # 500 份规模下一次性渲染会明显卡顿，分页加载
    page = Paginator(qs, PAGE_SIZE).get_page(request.GET.get('page'))

    return render(request, 'reports/report_list.html', {
        'page': page,
        'reports': page.object_list,
        'is_employee': _personal_only(request.user),
        'running': running,
        'failed': failed,
    })


@login_required
def report_detail(request, pk):
    report = get_object_or_404(
        Report, pk=pk, tenant_id=request.tenant.id,
        deleted_at__isnull=True)

    if _personal_only(request.user):
        if report.subject_id != request.user.subject_id:
            raise PermissionDenied('无权查看该报告')
        return render(request, 'reports/report_detail.html', {
            'report': report,
            'pages': report.pages.order_by('page_no'),
            'items': report.items.order_by('seq'),
            'explanation': report.explanations.order_by('-created_at').first(),
            'tasks': report.tasks.order_by('-created_at')[:8],
            'can_reprocess': False,
        })

    # 管理层与运维只能看到处理状态与数量，不得查看具体指标与解读
    items = report.items.all()
    return render(request, 'reports/report_meta.html', {
        'report': report,
        'tasks': report.tasks.order_by('-created_at')[:8],
        'n_items': items.count(),
        'n_abnormal': items.filter(flag='abnormal').count(),
        'n_unknown': items.filter(flag='unknown').count(),
        'n_graded': items.exclude(severity__isnull=True).count(),
        'can_reprocess': True,
    })


@login_required
def reprocess(request, pk):
    """重新排队处理一份报告。仅管理层与运维可用，只接受 POST。"""
    if not _can_manage(request.user):
        raise PermissionDenied('无权重新处理')

    report = get_object_or_404(
        Report, pk=pk, tenant_id=request.tenant.id,
        deleted_at__isnull=True)

    if request.method != 'POST':
        return redirect('reports:report_detail', pk=pk)

    from apps.pipeline.services import enqueue
    from apps.pipeline.models import Task

    # 清掉未完成的旧任务，避免重复
    Task.objects.filter(report=report,
                        status__in=(Task.STATUS_PENDING,
                                    Task.STATUS_RUNNING)).delete()
    report.status = Report.STATUS_UPLOADED
    report.save(update_fields=['status'])
    enqueue(report, 'ocr')

    messages.success(request, '已重新排队，等待 worker 处理')
    return redirect('reports:report_detail', pk=pk)


@login_required
def report_image(request, pk, page_no):
    """原图下载。不通过 MEDIA_URL 直接暴露，必须先校验归属。

    只有受检员工本人能取自己的原图。管理层与运维即使能进报告页，
    也不得拿到报告影像。
    """
    if not _personal_only(request.user):
        raise PermissionDenied('管理层与运维不得查看报告原图')

    report = get_object_or_404(
        Report, pk=pk, tenant_id=request.tenant.id,
        deleted_at__isnull=True)

    if report.subject_id != request.user.subject_id:
        raise PermissionDenied('无权查看该报告')

    page = report.pages.filter(page_no=page_no).first()
    if not page or not page.image_path:
        raise Http404('页图不存在')

    abs_path = os.path.join(settings.MEDIA_ROOT, page.image_path)
    if not os.path.exists(abs_path):
        raise Http404('原图文件已清理')

    return FileResponse(open(abs_path, 'rb'))


@login_required
def upload(request):
    """批量上传第一步：接收文件并暂存。"""
    if not _can_manage(request.user):
        raise PermissionDenied('无权上传报告')

    if request.method == 'POST':
        form = UploadForm(request.POST, request.FILES)
        if form.is_valid():
            staged = stage_upload(
                form.cleaned_data['image_list'],
                form.cleaned_data['archive_list'],
                request.tenant,
            )
            for err in staged['errors'][:10]:
                messages.error(request, err)
            for warn in staged['warnings']:
                messages.warning(request, warn)
            if not staged['batch']:
                return redirect('reports:upload')
            return redirect('%s?batch=%s' % (
                reverse('reports:upload_confirm'), staged['batch']))
    else:
        form = UploadForm()

    return render(request, 'reports/upload.html', {'form': form})


@login_required
def upload_confirm(request):
    """批量上传第二步：显示待导入清单，确认后才建报告。"""
    if not _can_manage(request.user):
        raise PermissionDenied('无权上传报告')

    batch = request.GET.get('batch') or request.POST.get('batch') or ''
    files = list(PendingFile.objects.filter(
        tenant_id=request.tenant.id, batch_id=batch).order_by('filename'))
    if not files:
        messages.warning(request, '这批文件已处理或不存在')
        return redirect('reports:report_list')

    if request.method == 'POST':
        assignments = {}
        for f in files:
            value = request.POST.get('emp_%d' % f.id, '').strip()
            if value:
                assignments[f.filename] = value
        result = confirm_upload(batch, request.tenant, assignments)

        if result['created']:
            messages.success(request, '已创建 %d 份报告' % result['created'])
        if result['pending']:
            messages.warning(
                request,
                '还有 %d 个文件没有工号，已转入待匹配' % len(result['pending']))
        for warn in result['warnings']:
            messages.warning(request, warn)
        for err in result['errors'][:10]:
            messages.error(request, err)
        return redirect('reports:report_list')

    groups = {}
    unmatched = []
    for f in files:
        if f.employee_no:
            groups.setdefault(f.employee_no, []).append(f)
        else:
            unmatched.append(f)

    return render(request, 'reports/upload_confirm.html', {
        'batch': batch,
        'groups': sorted(groups.items()),
        'unmatched': unmatched,
        'total': len(files),
        'total_mb': round(sum(f.size for f in files) / 1024.0 / 1024.0, 1),
    })


@login_required
def upload_pending(request):
    """待匹配：列出解析不出工号、尚未入库的文件。"""
    if not _can_manage(request.user):
        raise PermissionDenied('无权处理待匹配文件')

    files = PendingFile.objects.filter(
        tenant_id=request.tenant.id,
        state=PendingFile.STATE_UNMATCHED).order_by('-created_at')

    return render(request, 'reports/pending_list.html', {'files': files})


@login_required
def assign_pending(request, pk):
    """给一个待匹配文件指定工号并入库。"""
    if not _can_manage(request.user):
        raise PermissionDenied('无权处理待匹配文件')

    pending = get_object_or_404(
        PendingFile, pk=pk, tenant_id=request.tenant.id)

    if request.method == 'POST':
        emp = request.POST.get('employee_no', '').strip()
        if not re.match(r'^[A-Za-z0-9]+$', emp or ''):
            messages.error(request, '工号只能是字母与数字')
        else:
            ok, err = assign_pending_file(pending, request.tenant, emp)
            if ok:
                messages.success(request, '已归入工号 %s 的报告' % emp)
                return redirect('reports:upload_pending')
            messages.error(request, err)

    return render(request, 'reports/pending_assign.html', {'file': pending})
