# -*- coding: utf-8 -*-
"""10 云端：paddlenlp 导入诊断（定位"pip装成功但import失败"的真实原因）

背景：pip install paddlenlp 报告 Successfully installed paddlenlp-2.8.1，
      但 import paddlenlp 失败。且 paddlenlp 2.8.x 是配 paddle 2.6 时代的版本，
      当前环境是 paddle 3.3.0，存在版本代差。

用法：
    python tools/10_diag_paddlenlp.py
"""

import sys
import traceback
import importlib.metadata as md

sys.stdout.reconfigure(encoding='utf-8')


def show(name):
    try:
        return md.version(name)
    except Exception:
        return None


print('=' * 64)
print('1. 关键包版本')
print('=' * 64)
for n in ['paddlepaddle', 'paddlepaddle-iluvatar-gpu', 'paddleocr', 'paddlex',
          'paddlenlp', 'protobuf', 'onnx', 'paddle2onnx', 'jieba', 'dill',
          'seqeval', 'tokenizers', 'transformers']:
    v = show(n)
    print(f'  {n:<30} {v if v else "(未安装)"}')

print()
print('=' * 64)
print('2. paddlenlp 导入真实错误')
print('=' * 64)
try:
    import paddlenlp
    print('  ✓ import 成功, 版本:', getattr(paddlenlp, '__version__', '?'))
    print('  位置:', getattr(paddlenlp, '__file__', '?'))
except Exception as e:
    print(f'  ✗ {type(e).__name__}: {e}')
    print()
    print('  --- 完整 traceback ---')
    traceback.print_exc()

print()
print('=' * 64)
print('3. 逐个排查常见失败点')
print('=' * 64)
checks = [
    ('protobuf', lambda: __import__('google.protobuf')),
    ('tokenizers', lambda: __import__('tokenizers')),
    ('jieba', lambda: __import__('jieba')),
    ('seqeval', lambda: __import__('seqeval')),
    ('onnx', lambda: __import__('onnx')),
    ('dill', lambda: __import__('dill')),
]
for name, fn in checks:
    try:
        fn()
        print(f'  ✓ {name}')
    except Exception as e:
        print(f'  ✗ {name}: {type(e).__name__}: {e}')

print()
print('=' * 64)
print('4. 尝试 Taskflow 导入')
print('=' * 64)
try:
    from paddlenlp import Taskflow
    print('  ✓ from paddlenlp import Taskflow 成功')
except Exception as e:
    print(f'  ✗ {type(e).__name__}: {e}')
    traceback.print_exc()

print()
print('=' * 64)
print('5. 版本兼容性判读')
print('=' * 64)
pl = show('paddlenlp')
pd = show('paddlepaddle')
print(f'  paddle={pd}  paddlenlp={pl}')
if pl and pl.startswith('2.8'):
    print('  ⚠ paddlenlp 2.8.x 面向 paddle 2.6 时代，与 paddle 3.3 存在代差')
    print('    建议尝试: pip install "paddlenlp>=3.0.0b" -U')
    print('    或查看可用版本: pip index versions paddlenlp')
elif pl and pl.startswith('3.'):
    print('  ✓ paddlenlp 3.x 与 paddle 3.x 匹配')
else:
    print('  版本关系待人工判断')
