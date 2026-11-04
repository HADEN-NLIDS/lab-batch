from django import forms


class UploadForm(forms.Form):
    """批量上传的校验。

    Django 4.2 不允许在 widget 上设 multiple，因此不声明文件字段，
    改在 clean 中从 request.FILES 取列表。模板里的 input 是手写的。
    """

    def clean(self):
        cleaned = super().clean()
        images = self.files.getlist('images')
        archives = self.files.getlist('archives')
        if not images and not archives:
            raise forms.ValidationError('请至少选择一个图片或压缩包')
        cleaned['image_list'] = images
        cleaned['archive_list'] = archives
        return cleaned
