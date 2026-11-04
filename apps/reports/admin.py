from django.contrib import admin

from apps.reports.models import (Explanation, ExplanationSection, Item,
                                 PendingFile, Report, ReportPage, Subject)


class ReportPageInline(admin.TabularInline):
    model = ReportPage
    extra = 0
    fields = ('page_no', 'image_path', 'ocr_status')


class ItemInline(admin.TabularInline):
    model = Item
    extra = 0
    fields = ('seq', 'name_raw', 'value_raw', 'unit', 'ref_raw',
              'flag', 'severity')
    readonly_fields = ('seq', 'name_raw', 'value_raw', 'unit', 'ref_raw',
                       'flag', 'severity')
    can_delete = False
    max_num = 0


@admin.register(Subject)
class SubjectAdmin(admin.ModelAdmin):
    list_display = ('id', 'tenant_id', 'subject_key', 'sex', 'birth_year',
                    'created_at')
    search_fields = ('subject_key',)


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = ('id', 'tenant_id', 'report_no', 'subject', 'report_date',
                    'status', 'deleted_at', 'created_at')
    list_filter = ('status', 'report_date')
    search_fields = ('report_no', 'institution')
    date_hierarchy = 'created_at'
    inlines = [ReportPageInline, ItemInline]


@admin.register(ReportPage)
class ReportPageAdmin(admin.ModelAdmin):
    list_display = ('id', 'report', 'page_no', 'ocr_status', 'created_at')
    list_filter = ('ocr_status',)


@admin.register(Item)
class ItemAdmin(admin.ModelAdmin):
    list_display = ('id', 'report', 'seq', 'name_raw', 'value_raw', 'unit',
                    'flag', 'severity')
    list_filter = ('flag', 'severity')
    search_fields = ('name_raw', 'name_key')


class ExplanationSectionInline(admin.TabularInline):
    model = ExplanationSection
    extra = 0
    fields = ('seq', 'kind', 'title', 'body', 'cite_items')
    readonly_fields = ('seq', 'kind', 'title', 'body', 'cite_items')
    can_delete = False
    max_num = 0


@admin.register(Explanation)
class ExplanationAdmin(admin.ModelAdmin):
    list_display = ('id', 'report', 'tier', 'model', 'guard_status',
                    'n_items', 'n_abnormal', 'created_at')
    list_filter = ('tier', 'guard_status')
    date_hierarchy = 'created_at'
    inlines = [ExplanationSectionInline]


@admin.register(PendingFile)
class PendingFileAdmin(admin.ModelAdmin):
    list_display = ('id', 'tenant_id', 'batch_id', 'filename', 'employee_no',
                    'state', 'size', 'created_at')
    list_filter = ('state',)
    search_fields = ('filename', 'employee_no')
    date_hierarchy = 'created_at'
