# lab-batch — 体检报告批量处理

把成批的体检报告图片转成结构化指标，判定异常，生成可读的解读。

识别与判定在本机完成，不依赖网络；解读可接本地模型，也可纯模板输出。

---

## 为什么做这个

企业每年收发大量体检报告，大多是图片或扫描件，指标散落在表格里。既不能汇总，
也没法按人跟踪趋势，人工录入成本高且容易出错。

这个项目处理的是「从图片到结构化数据」这一段。核心取向是：能识别的尽量识别，
识别不出来的明确标出来，而不是猜一个看起来合理的数字。

---

## 处理链路

| 阶段 | 脚本 | 做什么 |
|---|---|---|
| 识别 | `03_export_ocr.py` | 图片 → PaddleOCR-VL → 文本块与表格 HTML |
| 解析 | `04_parse_predictions.py` | 文本块 → 指标（名称、数值、单位、参考范围） |
| 判定 | `31_judge_abnormal.py` | 数值 vs 参考区间 → normal / abnormal / unknown |
| 分级 | `45_grade_severity.py` | 危急值 + 偏离幅度 → critical / marked / moderate / mild |
| 解读 | `35_llm_explain.py` | 结构化结果 → 分段解读，附自动校验 |
| 存储 | `34_store.py`、`43_explain_store.py` | SQLite：指标、趋势、引用补全、解读 |
| 评测 | `07_evaluate.py` | 与标注对比，算字段级命中率 |

---

## 快速开始

需要 Python 3.10、PaddlePaddle 3.x 与 PaddleOCR-VL。

### 安装 PaddlePaddle

PaddleOCR-VL 依赖 PaddlePaddle 3.x。CPU 与 GPU 版的安装源不同，按实际环境选一个。

CPU：

```bash
pip install paddlepaddle
```

GPU（按 CUDA 版本选安装源，下面以 CUDA 12.6 为例）：

```bash
pip install paddlepaddle-gpu -i https://www.paddlepaddle.org.cn/packages/stable/cu126/
```

CUDA 版本与安装源的对应关系见官方安装页：<https://www.paddlepaddle.org.cn/install/quick>

装完验证一下：

```bash
python -c "import paddle; paddle.utils.run_check()"
```

看到 `PaddlePaddle is installed successfully!` 即为可用。

### 安装其余依赖

```bash
pip install -r requirements.txt
```

### 跑一份报告

```bash
# 识别
python tools/03_export_ocr.py --scan-src --src ./images --out-dir ./artifacts

# 解析成结构化指标
python tools/04_parse_predictions.py --artifacts ./artifacts --out predictions.jsonl

# 判定异常
python tools/31_judge_abnormal.py --predictions predictions.jsonl --out flagged.jsonl

# 严重度分级
python tools/45_grade_severity.py --predictions flagged.jsonl

# 生成解读
python tools/35_llm_explain.py --predictions flagged.jsonl --out explanations.jsonl --check
```

入库：

```bash
python tools/34_store.py --db lab.db --init
python tools/34_store.py --db lab.db --import flagged.jsonl
python tools/43_explain_store.py --db lab.db --predictions flagged.jsonl --from-jsonl explanations.jsonl
```

没有 API key 时，用 `--mock` 走假回复验证后续流程，或用 `--tier template` 输出纯模板解读。

---

## 解读与护栏

解读接大模型，但模型不负责判断严不严重：

- 严重度由规则引擎按危急值和偏离幅度给出，不由模型决定
- 模型只负责把确定的分级解释成人话
- 输出再过一遍自动校验，不合规整份退回模板

自动校验覆盖五类：个体化断言（"说明你感染了"）、确定性断言（"确诊"）、
用药建议、虚假安慰（"无需担心"），以及与严重度不符的措辞。
另有数值追溯与免责声明检查。

模板输出只陈述事实，读者能一眼看出它是清单；而一段编造的解释，读者看不出它错在哪。

---

## 设计取舍

**抽取和解读分两步。** 抽取要求"只抄不算"，解读要求"能说人话"，两者约束方向相反。
混在一个 prompt 里，模型会为了解释通顺而补出不存在的数值。

**识别不出的字段留空，不猜。** 没有参考范围的指标判为 unknown，而不是 normal。
用三态而非两态，是承认"判断不了"也是一种结论。

**严重度判定不交给模型。** 同一组数值不同次调用可能给出不同紧迫性，无法审计也无法复现。
规则引擎做不到解释，但判级比模型可靠。

**解读不通过校验就整份退回。** 删掉违规的几句会破坏上下文，也无法判断剩下部分是否可信。

---

## 明确不做的事

不给诊断结论、不给用药建议、不评价整体健康状况、不把指标异常直接等同于疾病、
不对缺参考范围的项目给倾向性判断。

解读结果仅供健康科普参考，不构成诊断，不能替代医生面诊。

---

## 项目结构

```
tools/     按编号排列的处理脚本
  00-02    环境检查与 OCR 冒烟
  03-07    识别、解析、评测
  19-30    大模型抽取实验、名称补全
  31-33    异常判定与标注链路
  34       数据落库（SQLite）
  35       解读生成与校验
  36-42    OCR 质量检查、变体生成、性能测试
  43       解读入库
  44       解读结果对比
  45       严重度分级
docs/      内部开发文档，不随仓库发布
```

---

## 测试

各模块自带自检，改代码后跑一遍：

```bash
python tools/31_judge_abnormal.py --selftest
python tools/34_store.py --selftest
python tools/35_llm_explain.py --selftest
python tools/43_explain_store.py --selftest
python tools/45_grade_severity.py --selftest
```

---

## 数据说明

仓库不含任何体检报告原图与识别结果。

测试建议使用真实报告单经脱敏后的图片。公开数据集往往对表格做过较强脱敏，
字段被遮挡或重排，识别效果不能代表实际场景——版面结构一旦被破坏，
测出来的准确率对部署没有参考价值。

脱敏应覆盖姓名、证件号、住院号、条码等身份字段，同时完整保留表格结构、
指标名称与数值。只涂掉身份信息、不动表格本身，才能测出真实的抽取能力。

早期版本用公开数据集（MedRepBench）搭过评测闭环，`tools/` 里保留了适配与
口径相关的脚本。该数据集脱敏程度较重，表格结构受影响，识别效果不具代表性，
因此当前测试不再以它为准。

OCR 使用 PaddleOCR-VL。

---

## 许可证

MIT，见 [LICENSE](LICENSE)。
