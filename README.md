# lab-batch — 体检报告批量处理

把成批的体检报告图片转成结构化指标，判定异常，生成可读的解读。

仓库里既有一套可直接调用的处理脚本，也有一个 Django 界面把整条链路包起来：
批量上传、队列处理、员工查看、群体统计。整套可以部署在企业管理的内网服务器上，
报告数据不出内网。

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
| 界面 | Django | 上传、队列调度、权限隔离、进度追踪、群体统计 |
| 识别 | `03_export_ocr.py` | 图片 → PaddleOCR-VL → 文本块与表格 HTML |
| 解析 | `04_parse_predictions.py` | 文本块 → 指标（名称、数值、单位、参考范围） |
| 判定 | `31_judge_abnormal.py` | 数值 vs 参考区间 → normal / abnormal / unknown |
| 分级 | `45_grade_severity.py` | 危急值 + 偏离幅度 → critical / marked / moderate / mild |
| 解读 | `35_llm_explain.py` | 结构化结果 → 分段解读，附自动校验 |
| 存储 | `34_store.py`、`43_explain_store.py` | SQLite：指标、趋势、引用补全、解读 |
| 评测 | `07_evaluate.py` | 与标注对比，算字段级命中率 |

---

## 快速开始

需要 Python 3.10 或更高版本。识别环节需要 NVIDIA 显卡与 PaddlePaddle 3.x；
解析、判定、解读、入库只用标准库，不装这些也能跑。

### 安装依赖

`requirements.txt` 是 NVIDIA 显卡下的完整依赖。`paddlepaddle-gpu` 只发布在 Paddle
官方源，必须同时指定该源，否则 pip 找不到匹配的 wheel：

```bash
pip install -r requirements.txt \
  --extra-index-url https://www.paddlepaddle.org.cn/packages/stable/cu126/
```

`cu126` 对应 CUDA 12.6。其他 CUDA 版本把路径里的 `cu126` 换掉即可，
对应关系见官方安装页：<https://www.paddlepaddle.org.cn/install/quick>

装完验证：

```bash
python -c "import paddle; print(paddle.__version__, paddle.device.is_compiled_with_cuda())"
```

输出 `3.3.0 True` 即为可用。首次运行识别时会自动下载模型权重到
`~/.paddlex/official_models`，约 2 GB。

paddle 自带的 cuDNN 是 9.5，运行时可能提示版本与编译时不一致：

```
The installed Paddle is compiled with CUDNN 9.9, but CUDNN version in your machine is 9.5
```

这条警告不影响结果。想提速可以单独升级：

```bash
pip install --upgrade "nvidia-cudnn-cu12==9.27.0.42"
```

实测升级后模型加载从 20.6 秒降到 9.6 秒，单页推理从 83 秒降到 61 秒。
不要把它写进 `requirements.txt`，paddle 3.3.0 精确依赖 9.5.1.17，锁别的版本会解析失败。

### 国产显卡

在天数 Iluvatar 等国产卡上运行时不装 `paddlepaddle-gpu`，改用平台提供的版本：

```bash
pip install paddlepaddle==3.3.0 paddle-iluvatar-gpu==3.3.0
```

再设置三个环境变量：

```bash
export PADDLE_DEVICE=iluvatar_gpu
export PADDLE_XCCL_BACKEND=iluvatar_gpu
export PADDLE_PDX_MODEL_SOURCE=aistudio
```

设备与模型源由 `tools/ocr_env.py` 解析：显式设置了 `PADDLE_DEVICE` 就以它为准，
未设置时自动检测 CUDA，都没有则退回 CPU。模型源同理，NVIDIA 环境默认
`modelscope`，国产卡用平台内置的 `aistudio`。

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

解析完成时会打印统计，其中「无表格」「无项目」不为零属正常现象：样本里如果有
被遮挡或残缺的报告，本来就读不出表格，这类样本用于检验系统会不会瞎猜。

入库：

```bash
python tools/34_store.py --db lab.db --init
python tools/34_store.py --db lab.db --import flagged.jsonl
python tools/43_explain_store.py --db lab.db --predictions flagged.jsonl --from-jsonl explanations.jsonl
```

没有 API key 时，用 `--mock` 走假回复验证后续流程，或用 `--tier template` 输出纯模板解读。

上面这些产物（`predictions.jsonl`、`flagged.jsonl`、`explanations.jsonl`、`lab.db`）
已在 `.gitignore` 中排除，不会被提交。它们含检验数值，也不应上传。

---

## Web 系统

仓库根目录是 Django 项目，把上面的脚本串成可用的批量处理界面。

### 角色与可见范围

三种角色：

- 受检员工：只看本人的报告与解读，首次登录需签署知情同意
- 企业管理层：可上传、看群体统计、看每份报告的处理状态与数量统计
- 运维人员：同管理层，另可进管理后台

企业管理层与运维**不能查看具体指标、解读与原图**。他们能看到报告列表，
但列表只显示编号与状态，不显示工号；点开报告只有处理进度与「项目总数、异常项、
无法判断、已分级」四个数字，拿不到任何一项的名称或数值。这是为了满足
「不需要医学判断能力的人也不该看到个人健康数据」这条约束。

多个单位共用一个部署实例，业务表都带 `tenant_id`，按行隔离。越权在视图层拦截，
员工访问他人的报告返回 403，上传与统计对员工同样拒绝。

### 处理流程

上传后立刻入队，worker 取到任务依次执行识别、解析、判定、解读，四段自动串联，
前一段成功才入队下一段。

队列是 MySQL 里的一张表，取任务用 `SELECT ... FOR UPDATE SKIP LOCKED`，
多个 worker 并发不会取到同一个。失败按 30 秒乘尝试次数的间隔退避重试，
三次耗尽才置为失败。

报告状态随之流转：已上传 → 处理中 → 已完成或失败。重试过程中保持处理中，
只有彻底失败才标记失败。

### 进度追踪

列表页每 10 秒自动刷新，顶部提示还有几份在处理中，失败的份数单独列出。
报告详情页在未完成时多出一张任务表，逐个环节列出状态、尝试次数、起止时间
与错误原因。

### 群体统计

按部门汇总人数与异常人数。不足 10 人的分组只说明样本不足，不给具体数字，
避免通过小样本反推个人情况。阈值由 `MIN_GROUP_SIZE` 控制。

### 部署

环境为 Python 3.10、MySQL 8.0、PyMySQL。数据库地址与账号从环境变量读取，
变量名见 `.env.example`。

启动前有一件事容易漏：MySQL 需要加载时区表。Django 在 `USE_TZ` 打开时会生成
`CONVERT_TZ(..., 'UTC', 'Asia/Shanghai')`，时区表为空时该函数返回 NULL，
管理后台按日期筛选会报 `Database returned an invalid datetime value`。

Linux 与 Docker 镜像自带 `mysql_tzinfo_to_sql`：

```bash
mysql_tzinfo_to_sql /usr/share/zoneinfo | mysql -u root mysql
```

Windows 官方安装包不带这个工具，可安装官方的时区 SQL 包，或只补用到的最小记录：

```sql
INSERT INTO mysql.time_zone (Time_zone_id, Use_leap_seconds) VALUES (1,'N'),(2,'N');
INSERT INTO mysql.time_zone_name (Name, Time_zone_id) VALUES ('UTC',1),('Asia/Shanghai',2);
INSERT INTO mysql.time_zone_transition_type
  (Time_zone_id, Transition_type_id, Offset, Is_DST, Abbreviation)
VALUES (1,0,0,0,'UTC'), (2,0,28800,0,'CST');
```

中国自 1991 年起没有夏令时，Asia/Shanghai 固定为 +08:00，两条记录足够。
只有要处理 1991 年以前的日期时，才需要完整的时区数据。

然后：

```bash
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

队列消费需要另开一个进程，且必须常驻：

```bash
python manage.py run_worker
```

少这个进程，上传的报告会一直停在「处理中」。Windows 可用任务计划、
Linux 用 systemd 配成开机自启。

识别依赖 GPU 与 PaddlePaddle。没有 GPU 时上传的报告会以 `no_gpu` 失败，
界面上能看到具体原因，其余功能不受影响。模型权重首次运行自动下载到
`~/.paddlex/official_models`，约 2 GB，换机器部署时可以整个目录拷过去。

上传的图片按文件名解析工号，必须命名为「工号_姓名」。
像 `TempDragFile_20261022_094238.png` 这类截图工具的默认名会被解析成同一个工号，
不同人的报告混在一起。上传页有提示，服务端也会给出警告。

报告处理完成后，用下面的命令按工号批量创建员工账号并绑定体检人：

```bash
python manage.py create_employees --dry-run     # 预览
python manage.py create_employees --password <统一初始密码>
```

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

config/    Django 配置与路由
apps/      业务模块
  core      组织、审计日志、公共工具
  accounts  账号、角色、知情同意
  reports   报告、指标、解读、上传
  pipeline  任务队列与处理链路
  stats     群体统计
templates/ 页面模板
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

Django 侧的回归测试覆盖上传校验、队列调度、权限隔离、统计抑制、
解读引用对齐与图片清理：

```bash
python manage.py test
```

---

## 数据说明

仓库不含任何体检报告原图与识别结果。

本机实测（RTX 4060 Laptop 8GB，PaddleOCR-VL 3.7.0）：30 份自采报告，
单页识别 55 到 226 秒，平均 121 秒；抽出 720 项指标，其中 91 项判定为异常。
识别行为与云端环境逐份一致，同一批图在不同机器上抽出的指标数相同。

测试建议使用真实报告单经脱敏后的图片。脱敏应覆盖姓名、证件号、住院号、条码等
身份字段，同时完整保留表格结构、指标名称与数值。只涂掉身份信息、不动表格本身，
才能测出真实的抽取能力。

公开数据集（MedRepBench）不作为基准评测：它脱敏程度较重，表格结构受影响，
算出的准确率对部署没有参考价值。

但它保留了一个用途——测试残缺报告的识别。该数据集的打码效果正好模拟了
复印件不清、字段被遮挡的真实情况，可用于检验系统遇到不完整输入时会不会瞎猜。
`tools/` 中 05、20、21 三个脚本为此保留。

OCR 使用 PaddleOCR-VL。

---

## 许可证

MIT，见 [LICENSE](LICENSE)。
