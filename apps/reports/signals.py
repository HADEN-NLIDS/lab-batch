"""删除报告或页时清理对应的图片文件，避免留下孤儿文件。"""

import logging
import os

from django.conf import settings
from django.db.models.signals import post_delete
from django.dispatch import receiver

from apps.reports.models import ReportPage

logger = logging.getLogger(__name__)


def remove_image_file(rel_path):
    """删除 media 下的一个文件。路径为空或文件不存在时静默跳过。

    只删 media 内的文件：先解析绝对路径并确认仍在 MEDIA_ROOT 下，
    避免 image_path 被构造为 ../ 而删到项目外的东西。
    """
    if not rel_path:
        return False

    media_root = os.path.abspath(str(settings.MEDIA_ROOT))
    abs_path = os.path.abspath(os.path.join(media_root, rel_path))
    if not abs_path.startswith(media_root + os.sep):
        logger.warning('拒绝删除 media 之外的路径：%s', rel_path)
        return False

    try:
        if os.path.exists(abs_path):
            os.remove(abs_path)
            return True
    except OSError as e:
        logger.warning('删除图片失败 %s：%s', abs_path, e)
    return False


@receiver(post_delete, sender=ReportPage)
def cleanup_page_image(sender, instance, **kwargs):
    """页记录被删除时删掉图片。

    删除报告会级联删除其所有页，因此这里也覆盖了整份报告的清理。
    软删除（只设 deleted_at）不会触发本信号，文件会保留到清理任务处理。
    """
    remove_image_file(instance.image_path)
