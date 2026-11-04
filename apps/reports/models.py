from django.db import models
from django.utils import timezone


class Subject(models.Model):
    """受检者。以 HMAC 假名标识，不存真实身份。"""

    tenant_id = models.BigIntegerField()
    subject_key = models.CharField(max_length=64,
                                   help_text='HMAC 假名，用于跨报告关联')
    sex = models.CharField(max_length=8, blank=True, default='')
    birth_year = models.SmallIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'subject'
        verbose_name = '受检者'
        verbose_name_plural = '受检者'
        constraints = [
            models.UniqueConstraint(fields=['tenant_id', 'subject_key'],
                                    name='uk_tenant_subject'),
        ]

    def __str__(self):
        return self.subject_key


class Report(models.Model):
    """一份体检报告。图片在 ReportPage 中。"""

    STATUS_UPLOADED = 'uploaded'
    STATUS_PROCESSING = 'processing'
    STATUS_DONE = 'done'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (STATUS_UPLOADED, '已上传'),
        (STATUS_PROCESSING, '处理中'),
        (STATUS_DONE, '已完成'),
        (STATUS_FAILED, '失败'),
    ]

    tenant_id = models.BigIntegerField()
    subject = models.ForeignKey('reports.Subject', null=True, blank=True,
                                on_delete=models.SET_NULL,
                                db_column='subject_id', related_name='reports')
    report_no = models.CharField(max_length=64, null=True, blank=True)
    report_date = models.DateField(null=True, blank=True)
    institution = models.CharField(max_length=128, blank=True, default='')
    schema_version = models.CharField(max_length=8, default='1')
    status = models.CharField(max_length=16, choices=STATUS_CHOICES,
                              default=STATUS_UPLOADED)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'report'
        verbose_name = '报告'
        verbose_name_plural = '报告'
        constraints = [
            models.UniqueConstraint(fields=['tenant_id', 'report_no'],
                                    name='uk_tenant_reportno'),
        ]
        indexes = [
            models.Index(fields=['tenant_id', 'subject', 'report_date'],
                         name='idx_tenant_subject_date'),
            models.Index(fields=['tenant_id', 'status'],
                         name='idx_tenant_status'),
            models.Index(fields=['deleted_at'], name='idx_deleted'),
        ]

    def __str__(self):
        return self.report_no or 'report-%s' % self.pk


class ReportPage(models.Model):
    """报告的一页。一份报告可含多页，指标跨页时判定与解读仍按整份进行。"""

    OCR_PENDING = 'pending'
    OCR_DONE = 'done'
    OCR_FAILED = 'failed'
    OCR_CHOICES = [
        (OCR_PENDING, '待识别'),
        (OCR_DONE, '已识别'),
        (OCR_FAILED, '识别失败'),
    ]

    tenant_id = models.BigIntegerField()
    report = models.ForeignKey('reports.Report', on_delete=models.CASCADE,
                               db_column='report_id', related_name='pages')
    page_no = models.IntegerField(help_text='页序，从 1 开始')
    image_path = models.CharField(max_length=255,
                                  help_text='原图相对路径')
    ocr_artifact = models.CharField(max_length=255, blank=True, default='')
    ocr_status = models.CharField(max_length=16, choices=OCR_CHOICES,
                                  default=OCR_PENDING)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'report_page'
        verbose_name = '报告页'
        verbose_name_plural = '报告页'
        constraints = [
            models.UniqueConstraint(fields=['report', 'page_no'],
                                    name='uk_report_page'),
        ]

    def __str__(self):
        return '%s p%s' % (self.report_id, self.page_no)


class Item(models.Model):
    """指标明细。flag 表示是否异常，severity 表示严重程度，两者来源不同。"""

    FLAG_CHOICES = [
        ('normal', '正常'),
        ('abnormal', '异常'),
        ('unknown', '无法判断'),
    ]
    SEVERITY_CHOICES = [
        ('critical', '危急'),
        ('marked', '显著'),
        ('moderate', '中度'),
        ('mild', '轻度'),
    ]

    tenant_id = models.BigIntegerField()
    report = models.ForeignKey('reports.Report', on_delete=models.CASCADE,
                               db_column='report_id', related_name='items')
    seq = models.IntegerField(help_text='报告内序号')
    name_raw = models.CharField(max_length=128)
    name_key = models.CharField(max_length=128, help_text='归一化名')
    indicator_code = models.CharField(max_length=32, blank=True, default='')
    value_raw = models.CharField(max_length=64, blank=True, default='')
    value_num = models.DecimalField(max_digits=12, decimal_places=4,
                                    null=True, blank=True)
    unit = models.CharField(max_length=32, blank=True, default='')
    ref_raw = models.CharField(max_length=64, blank=True, default='')
    ref_kind = models.CharField(max_length=16, blank=True, default='')
    ref_low = models.DecimalField(max_digits=12, decimal_places=4,
                                  null=True, blank=True)
    ref_high = models.DecimalField(max_digits=12, decimal_places=4,
                                   null=True, blank=True)
    flag = models.CharField(max_length=16, choices=FLAG_CHOICES,
                            blank=True, default='')
    flag_reason = models.CharField(max_length=255, blank=True, default='')
    flag_confidence = models.CharField(max_length=8, blank=True, default='')
    flag_note = models.CharField(max_length=255, blank=True, default='')
    severity = models.CharField(max_length=16, choices=SEVERITY_CHOICES,
                                blank=True, default='')
    severity_reason = models.CharField(max_length=255, blank=True, default='')
    value_source = models.CharField(max_length=16, default='measured',
                                    help_text='measured 本次实测 / carried 引用历史')
    source_report_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        db_table = 'item'
        verbose_name = '指标'
        verbose_name_plural = '指标'
        constraints = [
            models.UniqueConstraint(fields=['report', 'seq'],
                                    name='uk_report_seq'),
        ]
        indexes = [
            models.Index(fields=['tenant_id', 'name_key'],
                         name='idx_tenant_name'),
            models.Index(fields=['tenant_id', 'indicator_code'],
                         name='idx_tenant_code'),
        ]

    def __str__(self):
        return self.name_raw


class Explanation(models.Model):
    """一份解读。同一报告可保留多版，取最新展示。"""

    TIER_LLM = 'llm'
    TIER_TEMPLATE = 'template'
    TIER_CHOICES = [
        (TIER_LLM, '模型生成'),
        (TIER_TEMPLATE, '模板输出'),
    ]
    GUARD_CHOICES = [
        ('pass', '通过'),
        ('degraded', '降级'),
    ]

    tenant_id = models.BigIntegerField()
    report = models.ForeignKey('reports.Report', on_delete=models.CASCADE,
                               db_column='report_id',
                               related_name='explanations')
    tier = models.CharField(max_length=16, choices=TIER_CHOICES)
    model = models.CharField(max_length=64, blank=True, default='')
    prompt_version = models.CharField(max_length=32, blank=True, default='')
    guard_status = models.CharField(max_length=16, choices=GUARD_CHOICES,
                                    default='pass')
    guard_detail = models.TextField(blank=True, default='')
    n_items = models.IntegerField(null=True, blank=True)
    n_abnormal = models.IntegerField(null=True, blank=True)
    n_unknown = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'explanation'
        verbose_name = '解读'
        verbose_name_plural = '解读'
        indexes = [
            models.Index(fields=['report', 'created_at'],
                         name='idx_report_time'),
        ]

    def __str__(self):
        return '%s %s' % (self.report_id, self.tier)


class ExplanationSection(models.Model):
    """解读分段。拆行存储，便于反查该段引用了哪些指标。"""

    KIND_CHOICES = [
        ('status', '当前情况'),
        ('meaning', '指标说明'),
        ('action', '建议'),
    ]

    explanation = models.ForeignKey('reports.Explanation',
                                    on_delete=models.CASCADE,
                                    db_column='explanation_id',
                                    related_name='sections')
    seq = models.IntegerField()
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    title = models.CharField(max_length=128, blank=True, default='')
    body = models.TextField()
    cite_items = models.CharField(max_length=512, blank=True, default='',
                                  help_text='引用的指标 name_key，逗号分隔')

    class Meta:
        db_table = 'explanation_section'
        verbose_name = '解读分段'
        verbose_name_plural = '解读分段'
        constraints = [
            models.UniqueConstraint(fields=['explanation', 'seq'],
                                    name='uk_exp_seq'),
        ]

    def __str__(self):
        return '%s#%s' % (self.explanation_id, self.seq)


class PendingFile(models.Model):
    """上传暂存：已接收但尚未入库的图片。

    上传分两步走。第一步只校验并落到暂存区，第二步用户确认后才建报告。
    解析不出工号的文件也留在这里，等管理员指认受检者。
    """

    STATE_STAGED = 'staged'
    STATE_UNMATCHED = 'unmatched'
    STATE_CHOICES = [
        (STATE_STAGED, '待确认'),
        (STATE_UNMATCHED, '待指定工号'),
    ]

    tenant_id = models.BigIntegerField()
    batch_id = models.CharField(max_length=32)
    filename = models.CharField(max_length=128)
    image_path = models.CharField(max_length=255)
    employee_no = models.CharField(max_length=64, blank=True, default='')
    size = models.IntegerField(default=0)
    state = models.CharField(max_length=16, choices=STATE_CHOICES,
                             default=STATE_STAGED)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'pending_file'
        verbose_name = '待处理文件'
        verbose_name_plural = '待处理文件'
        indexes = [
            models.Index(fields=['tenant_id', 'batch_id'],
                         name='idx_pending_batch'),
            models.Index(fields=['tenant_id', 'state'],
                         name='idx_pending_state'),
        ]

    def __str__(self):
        return '%s %s' % (self.filename, self.state)
