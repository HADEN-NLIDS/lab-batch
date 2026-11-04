"""把处理链路的 JSONL 产物写入数据库。

judge 与 explain 两个环节的脚本输出是 JSONL，Django 侧负责解析并落库，
使报告详情页能直接读到指标与解读。
"""

import json
import os

from django.db import transaction

from apps.core.utils import cite_items_of, name_key, to_num
from apps.reports.models import (Explanation, ExplanationSection, Item,
                                 Report)

# flag 取值与模型的 choices 对齐
VALID_FLAGS = {'normal', 'abnormal', 'unknown'}
VALID_SEVERITY = {'critical', 'marked', 'moderate', 'mild'}


def _read_jsonl(path):
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding='utf-8') as fp:
        for line in fp:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def load_severity(path):
    """读取分级结果，返回 {指标名: (level, reason)}。

    分级脚本按报告输出一行，内含该报告所有异常项的分级。
    """
    mapping = {}
    for row in _read_jsonl(path):
        sev = row.get('severity') or {}
        for it in sev.get('items') or []:
            name = (it.get('name') or '').strip()
            level = (it.get('level') or '').strip()
            if not name or level not in VALID_SEVERITY:
                continue
            mapping[name_key(name)] = (level, (it.get('reason') or '')[:255])
    return mapping


def import_items(report, flagged_path='', severity_path='', rows=None):
    """把一份报告的判定结果写入 item 表。

    flagged.jsonl 是逐页产出的，本函数按页合并到同一份报告，
    并重新编 seq。写之前先清空旧指标，便于重跑覆盖。

    rows 可直接传入已解析的记录，供导入命令按报告分组时使用。
    """
    if rows is None:
        rows = _read_jsonl(flagged_path)
    severity = load_severity(severity_path) if severity_path else {}

    merged = []
    for row in rows:
        for it in row.get('items') or []:
            name = (it.get('item_name') or '').strip()
            if not name:
                continue
            merged.append(it)

    if not merged:
        return 0

    with transaction.atomic():
        Item.objects.filter(report=report).delete()
        created = 0
        for seq, it in enumerate(merged):
            name = (it.get('item_name') or '').strip()
            key = name_key(name)
            flag = (it.get('is_abnormal') or '').strip()
            if flag not in VALID_FLAGS:
                flag = ''
            if not flag:
                flag = 'unknown' if not (it.get('item_range') or '').strip() else ''

            level, reason = severity.get(key, ('', ''))

            Item.objects.create(
                tenant_id=report.tenant_id,
                report=report,
                seq=seq,
                name_raw=name[:128],
                name_key=key[:128],
                indicator_code=(it.get('item_code') or '')[:32],
                value_raw=(it.get('item_value') or '')[:64],
                value_num=to_num(it.get('item_value')),
                unit=(it.get('item_unit') or '')[:32],
                ref_raw=(it.get('item_range') or '')[:64],
                ref_kind=(it.get('ref_kind') or '')[:16],
                ref_low=to_num(it.get('ref_low')),
                ref_high=to_num(it.get('ref_high')),
                flag=flag,
                flag_reason=(it.get('abnormal_reason') or '')[:255],
                flag_confidence=(it.get('abnormal_confidence') or '')[:8],
                flag_note=(it.get('abnormal_note') or '')[:255],
                severity=level,
                severity_reason=reason,
                value_source='measured',
            )
            created += 1
    return created


def import_explanation(report, explanations_path):
    """把解读写入 explanation 与 explanation_section。

    同一报告允许多版，这里每次新增一条，取最新展示。
    """
    rows = _read_jsonl(explanations_path)
    if not rows:
        return None

    rec = rows[0]
    sections = rec.get('sections') or []
    if not sections:
        return None

    degraded = bool(rec.get('degraded'))
    n_abnormal = report.items.filter(flag='abnormal').count()
    n_unknown = report.items.exclude(flag__in=['normal', 'abnormal']).count()

    # 段落与指标的关联靠正文匹配还原，模型输出里没有结构化引用
    raw_names = list(report.items.values_list('name_raw', flat=True))

    with transaction.atomic():
        explanation = Explanation.objects.create(
            tenant_id=report.tenant_id,
            report=report,
            tier=Explanation.TIER_TEMPLATE if degraded else Explanation.TIER_LLM,
            model=(rec.get('model') or '')[:64],
            prompt_version=(rec.get('prompt_version') or '')[:32],
            guard_status='degraded' if degraded else 'pass',
            guard_detail=(rec.get('degrade_reason') or '')[:2000],
            n_items=report.items.count(),
            n_abnormal=n_abnormal,
            n_unknown=n_unknown,
        )
        for seq, s in enumerate(sections):
            body = (s.get('body') or '').strip()
            if not body:
                continue
            hits = cite_items_of(body, raw_names)
            cite = ','.join(name_key(h) for h in hits)[:512]
            ExplanationSection.objects.create(
                explanation=explanation,
                seq=seq,
                kind=(s.get('kind') or 'status')[:16],
                title=(s.get('title') or '')[:128],
                body=body,
                cite_items=cite,
            )
    return explanation
