# -*- coding: utf-8 -*-
"""09 云端：UIE-X 依赖预检（装 paddlenlp 前先确认不会破坏现有环境）

背景：AI Studio 环境重装过一次。paddlenlp 对 paddle 版本有要求，
      直接装可能升级/降级 paddle，把已经跑通的 paddleocr 搞坏。

用法（AI Studio）：
    python tools/09_precheck_uiex.py
    # 若结论是"可以安装"，再执行：
    pip install paddlenlp
"""

import sys
import subprocess
import importlib.metadata as md

sys.stdout.reconfigure(encoding='utf-8')

WATCH = ['paddlepaddle', 'paddlepaddle-gpu', 'paddlepaddle-iluvatar-gpu',
         'paddlenlp', 'paddleocr', 'paddlex']


def ver(name):
    try:
        return md.version(name)
    except Exception:
        return None


def main():
    print('=' * 62)
    print('1. 当前关键包版本')
    print('=' * 62)
    current = {}
    for n in WATCH:
        v = ver(n)
        current[n] = v
        print(f'  {n:<28} {v if v else "(未安装)"}')

    print()
    print('=' * 62)
    print('2. 模拟安装 paddlenlp（--dry-run，不会真的装）')
    print('=' * 62)
    try:
        r = subprocess.run([sys.executable, '-m', 'pip', 'install',
                            '--dry-run', 'paddlenlp'],
                           capture_output=True, text=True, timeout=300)
        out = (r.stdout or '') + (r.stderr or '')
        print(out[-3000:])
    except Exception as e:
        print(f'!! 预检失败: {e}')
        print('可手动执行: pip install --dry-run paddlenlp')
        return

    print()
    print('=' * 62)
    print('3. 风险判读')
    print('=' * 62)
    low = out.lower()
    risks = []
    # 会卸载/降级 paddle 本体
    if 'uninstall' in low and 'paddle' in low:
        risks.append('会卸载部分paddle相关包')
    for kw in ('paddlepaddle', 'paddlepaddle-gpu', 'paddlepaddle-iluvatar-gpu'):
        if f'would install {kw}' in low or f'would downgrade {kw}' in low:
            risks.append(f'会变更 {kw}')
    if 'would downgrade' in low:
        risks.append('存在降级操作（最需警惕，可能破坏已跑通的paddleocr）')
    if 'could not find a version' in low or 'no matching distribution' in low:
        risks.append('找不到可安装的版本（可能Python/平台不兼容）')

    if not risks:
        print('  ✓ 未检测到对 paddle 本体的升级/降级')
        print('  → 可以尝试: pip install paddlenlp')
        print('  → 建议先记录现状: pip list > before_paddlenlp.txt')
    else:
        print('  ⚠ 检测到风险：')
        for x in risks:
            print(f'    · {x}')
        print()
        print('  建议做法：')
        print('  1. 先备份现状: pip list > before_paddlenlp.txt')
        print('  2. 若风险涉及paddle本体，优先考虑：')
        print('     · 用 venv 建独立环境装 paddlenlp（但AI Studio环境可能不便）')
        print('     · 或改用 PaddleOCR-VL 直接输出结构化JSON（方案B，零依赖变更）')
        print('  3. 装完务必复验:')
        print('     python -c "import paddle; print(paddle.__version__)"')
        print('     python tools/01_check_env.py')

    print()
    print('注意：paddlenlp 首次运行会下载 uie-x-base 模型（约1GB），耗时较长。')


if __name__ == '__main__':
    main()
