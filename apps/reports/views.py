import os

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render

from apps.reports.forms import UploadForm
from apps.reports.models import Report
from apps.reports.services import handle_upload

PAGE_SIZE = 20


def _should_see_all(user):
    """管理层与运维可看本租户全部，员工只能看本人。"""
    return user.role in ('manager', 'admin')


@login_required
def report_list(request):
    if request.tenant is None:
        raise PermissionDenied('账号未绑定组织')

    qs = Report.objects.filter(tenant_id=request.tenant.id,
                               deleted_at__isnull=True)
    if not _should_see_all(request.user):
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
        'is_employee': not _should_see_all(request.user),
        'running': running,
        'failed': failed,
    })


@login_required
def report_detail(request, pk):
    report = get_object_or_404(
        Report, pk=pk, tenant_id=request.tenant.id,
        deleted_at__isnull=True)

    if not _should_see_all(request.user) and \
            report.subject_id != request.user.subject_id:
        raise PermissionDenied('无权查看该报告')

    return render(request, 'reports/report_detail.html', {
        'report': report,
        'pages': report.pages.order_by('page_no'),
        'items': report.items.order_by('seq'),
        'explanation': report.explanations.order_by('-created_at').first(),
        'tasks': report.tasks.order_by('-created_at')[:8],
        'can_reprocess': _should_see_all(request.user),
    })


@login_required
def reprocess(request, pk):
    """重新排队处理一份报告。仅管理层与运维可用，只接受 POST。"""
    if not _should_see_all(request.user):
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
    """原图下载。不通过 MEDIA_URL 直接暴露，必须先校验归属。"""
    report = get_object_or_404(
        Report, pk=pk, tenant_id=request.tenant.id,
        deleted_at__isnull=True)

    if not _should_see_all(request.user) and \
            report.subject_id != request.user.subject_id:
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
    """批量上传。仅管理层与运维可用。"""
    if not _should_see_all(request.user):
        raise PermissionDenied('无权上传报告')

    if request.method == 'POST':
        form = UploadForm(request.POST, request.FILES)
        if form.is_valid():
            result = handle_upload(
                form.cleaned_data['image_list'],
                form.cleaned_data['archive_list'],
                request.tenant,
            )
            if result['created']:
                messages.success(
                    request,
                    '已创建 %d 份报告，共 %d 个文件'
                    % (result['created'], result['total_files']))
            if result['pending']:
                messages.warning(
                    request,
                    '以下文件未能解析出工号，需人工匹配：%s'
                    % '、'.join(result['pending'][:10]))
            for warn in result['warnings']:
                messages.warning(request, warn)
            for err in result['errors'][:10]:
                messages.error(request, err)
            return redirect('reports:report_list')
    else:
        form = UploadForm()

    return render(request, 'reports/upload.html', {'form': form})
