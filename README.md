# dieΠ

dieΠ 是一个本地优先、事件驱动的 A 股回测框架。它不只计算收益曲线，还重点回答三件事：
策略当时能看到什么、订单为什么这样成交，以及一份结果是否完整到可以比较。

当前版本是 `0.1.0` Alpha。项目品牌写作 dieΠ；Python 发行名和命令行入口写作 `diepi`。
公开 API 位于 `diepi.backtest`、`diepi.futures` 和 `diepi.artifacts`。

## 谁适合先用

普通用户可以使用 dieΠ，但真实行情需要自行合法取得、整理并负责。首批最适合的是已经
拥有本地 Parquet 行情，希望数据、策略和结果都留在自己电脑上的研究者。

当前正式支持的主路径是：

- 沪深北 A 股与 ETF/LOF 的日线现金研究；
- 自备本地数据的诊断、严格校验、CLI/Python 回测和结果检查；
- 随 Python 包或 wheel 安装的 PySide6 GUI。

分钟现金回测、独立并行和底层 Python 编排属于高级路径。`IC/IM/IF/IH` 的独立日线
期货引擎属于实验性近似研究，不与现金引擎共享账户。当前不提供 standalone `.exe`、
`.dmg` 或 Linux 桌面安装器。

dieΠ 不适合以下需求：自动下载行情、Excel/数据库的通用导入、tick/Level-2 队列还原、
实盘交易、股票与期货共享保证金账户，或内建参数网格与多策略调度。

## 安装

需要 Python 3.10 或更高版本。从源码目录安装核心功能：

```bash
python -m pip install -e .
```

需要 GUI：

```bash
python -m pip install -e ".[gui]"
```

从 PyPI 安装发布包时使用：

```bash
python -m pip install diepi
python -m pip install "diepi[gui]"
```

GUI 对应发行包的 `gui` 可选依赖；项目目前不承诺系统级桌面安装器。安装后先检查环境：

```bash
diepi doctor
```

`doctor` 只读检查 Python、核心依赖、可选 GUI 依赖以及数据/结果路径。尚未配置行情时会明确
报告 `NOT_CONFIGURED`/警告，不阻止 synthetic demo；显式传入不存在或不合格的数据根仍会
失败。

## 5 / 15 / 30 分钟首次体验

5 分钟 demo 可以从一个新的工作目录执行；15 / 30 分钟的真实切片示例必须从源码 checkout
或解压后的 sdist 项目根目录执行。若系统找不到 `diepi`，可把命令开头替换成
`python -m diepi`。

### 5 分钟：验证安装，并在 GUI 打开结果

```bash
diepi demo diepi_demo
diepi gui --data-root diepi_demo/market-data --results-root diepi_demo/results
```

第一条命令创建一个新工作区，生成 deterministic synthetic 数据，写入 dataset manifest，
严格校验并运行一次日线回测；目标目录已存在时拒绝覆盖。第二条命令打开同一数据根与结果
根：进入“历史记录”，双击 `synthetic_demo`，即可查看净值、回撤、交易和个股明细。只想生成
和校验时：

```bash
diepi demo diepi_demo --generate-only
```

重要：demo 中的价格、成交量、交易日和证券名称均由程序生成，不是真实行情，也不是某只
证券的抽样或匿名化历史。它只能验证安装、路径、数据契约、策略生命周期和结果流程；
demo 收益没有研究或投资含义。

### 15 分钟：用真实切片跑股票 + ETF 的 MA 示例

这一段只适用于源码 checkout 或 sdist；wheel 内不附带真实行情切片。从项目根目录依次执行：

```bash
diepi data validate --data-root examples/market_data_v1/data --symbols 600000.SH,000001.SZ,510300.SH,159915.SZ --start 20260101 --end 20260630 --price-mode dual
diepi examples copy ma-cross ./ma_cross_strategy.py
diepi run ./ma_cross_strategy.py --data-root examples/market_data_v1/data --results-root ./diepi_results --symbols 600000.SH,510300.SH --start 20260101 --end 20260630 --price-mode dual --stamp-duty auto --daily-open-previous-day-ratio 0.1 --name public-ma-mixed
diepi gui --data-root examples/market_data_v1/data --results-root ./diepi_results
```

这次运行把沪市股票 `600000.SH` 与宽基 ETF `510300.SH` 放在同一现金组合中。GUI 中进入
“历史记录”，双击 `public-ma-mixed`；再双击成交行，可以查看该标的的成交记录和经行情指纹
核验的 K 线。源码/sdist GUI 也提供“载入公开样例”按钮，可一次填入同一配置；“载入
MA5/20 策略”按钮只载入代码和执行假设，不会偷偷替换你的数据根、标的或日期。

### 30 分钟：让策略信号与回测解耦

先创建一份小型目标权重信号。下面这条 Python 命令在 PowerShell、cmd、Bash 中都可直接执行：

```bash
python -c "from pathlib import Path; Path('signals_mixed.csv').write_text('date,symbol,target_weight\n20260106,600000.SH,0.5\n20260106,510300.SH,0.4\n20260302,600000.SH,0.2\n20260302,510300.SH,0.7\n20260601,600000.SH,0\n20260601,510300.SH,0\n', encoding='utf-8')"
diepi run --signals ./signals_mixed.csv --signals-format target --data-root examples/market_data_v1/data --results-root ./diepi_results --start 20260101 --end 20260630 --price-mode dual --stamp-duty auto --daily-open-previous-day-ratio 0.1 --name public-signals-mixed
diepi gui --data-root examples/market_data_v1/data --results-root ./diepi_results
```

GUI 历史中现在会同时出现代码策略 `public-ma-mixed` 和信号回放 `public-signals-mixed`；两者
都使用同一份已验证 `RunArtifact v1` 结果契约。signals 的 `date=T` 表示 T 日盘前已知、T 日
开盘提交。需要同时表达盘前目标和预先确定的当日收盘退出时，使用下文的 combo v1，而不是
把 T 日收盘信息回填成 T 日开盘信号。

## 使用自己的本地数据

dieΠ 不下载、不上传，也不附带完整行情库。公开源码和 sdist 仅包含一份经项目所有者
确认可公开的四证券、半年真实行情格式切片；运行时 wheel 不包含该切片。现金引擎读取
以下约定式布局：
每个 Parquet 的精确字段名、dtype、单位、日线/分钟分片、复权因子锚点和切片
示例见 `docs/product/05-local-market-data-format-v1.md`。

```text
DATA_ROOT/
└─ parquet/
   ├─ metadata/
   │  ├─ common/trade_cal.parquet        # 可选：完整本地日历覆盖
   │  └─ stock/basic.parquet             # 全市场股票池需要
   └─ timeseries/
      ├─ daily/{symbol}.parquet
      ├─ daily_raw/{symbol}.parquet
      └─ adj_factor/{symbol}.parquet
```

现金引擎内置独立的 A 股交易日历 `cn-a-share-2010-2026-v1`，覆盖
`20100101..20261231`，因此这段范围内不必准备 `trade_cal.parquet`。若该文件存在，它会
作为完整 local override 使用，不与内置日历拼接；日期不连续、`is_open` 非 0/1 或范围
不足都会 fail closed。

默认 `dual` 模式要求研究用 HFQ 轨、原始撮合轨和复权因子在所请求范围内严格对齐。先对
具体标的和日期做只读校验：

```bash
diepi data validate \
  --data-root /path/to/market-data \
  --symbols 000001.SZ \
  --start 20240101 \
  --end 20241231 \
  --price-mode dual
```

Windows PowerShell 可以把命令写成一行。校验不会下载、排序、取交集、填充或修复数据；
通过只证明该请求范围满足当前结构与执行契约，不证明数据授权、供应商真实性或经济含义。
真实数据及其研究适用性始终由用户负责。

已有较大的兼容数据湖时，可以只在本机抽取限定日期和标的的私有工作区：

```bash
diepi data extract \
  --source-data-root /path/to/market-data \
  --workspace ./my-private-sample \
  --symbols 159915.SZ,510300.SH \
  --start 20260112 \
  --end 20260515 \
  --include-metadata
```

抽取器只读源目录，拒绝覆盖目标，自动保留前一交易日、raw/HFQ 双轨和复权因子锚点，
使用内置日历完成自校验，因此不读取或复制源 `trade_cal.parquet`。输出默认标记为私有、
不可再分发；它不会发现或复制策略
信号。行情和元数据遇到框架公开集合之外的未知列会 fail closed，避免把研究注释或策略
字段静默带入输出；Parquet/DataFrame 的不透明自定义 attributes 也会被剥离。POSIX 输出
使用私有模式，Windows 输出使用受保护 ACL；默认错误不
回显源绝对路径，排查时可显式加 `--verbose-errors`。运行期间源树和目标父目录应由当前
进程独占；抽取器会拒绝链接/重解析点并使用不覆盖目标的原子发布，但它不是防御同账号
恶意进程的沙箱。限制数量和时间不等于取得数据再分发许可。

## 运行日线现金回测

安装包内置一份可编辑的 MA5/MA20 严格交叉示例；先复制到自己的工作区：

```bash
diepi examples list
diepi examples copy ma-cross ./ma_cross_strategy.py
```

最少的 raw 日线工作区只需所选标的一个 Parquet 文件（股票放在 `daily_raw/`，ETF/LOF
放在 `etf_daily_raw/`），字段为 `trade_date,open,high,low,close,pre_close,amount`。例如：

```bash
diepi run ./ma_cross_strategy.py \
  --data-root /path/to/market-data \
  --results-root ./diepi_results \
  --symbols 000001.SZ \
  --start 20240101 \
  --end 20241231 \
  --price-mode raw \
  --daily-open-previous-day-ratio 0.1 \
  --name quickstart
```

示例用截至 T-1 的已完成日线识别 MA5 上穿/下穿 MA20，并在 T 日开盘下单；至少需要
21 个已完成观测才能形成第一个交叉判断。它会提交日线开盘单，因此必须显式声明集合竞价
容量。上例中的 10% 只是演示假设，不是推荐参数。`raw` 模式不需要复权因子，也不会执行
因子公司行为覆盖；它按原始价格序列原样建模。跨除权除息范围的正式研究应使用默认
`dual` 并提供严格对齐的 HFQ/raw/因子三件套。

`diepi run` 有三种互斥的输入方式，CLI 与 GUI 的目标是保持同一语义：

- **策略代码**：在受信任的本地 Python 回调中计算并提交交易意图；
- **signals CSV**：内置适配器读取 `date,symbol,target_weight` 或
  `date,symbol,action`，把已预计算清单转换为开盘目标/动作；
- **冻结 combo**：读取 `targets + close_sells + daily`（可选严格 manifest），同时表达盘前目标与
  运行前已经确定的当日收盘退出。

框架的执行边界是规范化后的下单、目标权重和定时收盘意图，不是某一种 CSV。数据库、模型
输出或个人文件格式可以先由用户代码转换成这些意图，或转换成上述受支持的 signals/combo
格式；diePi 不会直接猜测或解析任意 Excel、数据库表和私有信号格式。signals 的 `date=T`
表示该行在 **T 日盘前已经可知并于 T 日开盘提交**，不是读取 T 日收盘后再回填 T 日成交；
若信号由 T 日收盘数据产生，应把执行日期写为下一交易日。预先已知且确实要在 T 日收盘执行
的退出应使用 combo 的 `close_sells`，或在代码策略的合法生命周期回调中显式调度。

显式组合池可以把受规则簿支持的 A 股与 ETF/LOF 放进同一个 `PortfolioEngine`，共同使用
一笔现金，例如 `--symbols 600000.SH,511010.SH --stamp-duty auto`。每个 symbol 仍分别走
股票/场内基金数据目录，并分别应用价格精度、涨跌停、申报单位和 T+0/T+1；`auto` 会按
日期向股票卖出计印花税并对场内基金免税。未传显式 scope 的“全市场”池是股票池，不会
自动把 ETF 合并进来。`transfer_fee_rate` 目前仍是整个账户共享的单一费率，没有按日期或
品种自动切换；需要这种精度时必须把它作为模型限制披露。

`--name` 是 1–128 个字符的可移植标识：首字符为 ASCII 字母或数字，其余只可用
ASCII 字母、数字、点、下划线或连字符。名称已存在时框架拒绝覆盖；也可以省略名称，让
框架生成运行标识。

源码 checkout 中的 `examples/ma_cross_strategy.py` 与 wheel 内置版本有字节一致性测试；
安装 wheel 后不要依赖仓库相对路径，使用 `diepi examples copy`。旧的“策略路径作为首个
参数”仍是 `diepi run` 的兼容简写，新脚本和文档使用显式 `run`。

冻结信号由“每日目标权重 + 预先确定的当日收盘退出 + 完整 daily 覆盖”组成时，使用
combo 入口，而不是把收盘退出塞进 `on_day`：

```bash
diepi combo validate ./my-combo --tag reviewed-v1 --json

diepi run --combo-bundle ./my-combo \
  --data-root /path/to/market-data \
  --results-root ./diepi_results \
  --start 20260112 --end 20260515 \
  --daily-open-previous-day-ratio 0.1 \
  --daily-close-previous-day-ratio 0.1 \
  --name combo-replay
```

规范目录必须有 `targets.csv`、`close_sells.csv`、`daily.csv`；`diepi_combo.json` 是可选的
严格身份清单，存在时必须与三份 CSV 完全一致。旧式 `new_combo_*_<tag>.csv` 目录可配
`--combo-tag`。`diepi combo validate` 严格复用运行时装载器，只读取输入并向终端输出稳定、
不含源绝对路径的摘要；它不会生成或覆盖 manifest。退出码 0 表示有效、1 表示 bundle
无效、2 表示命令用法错误。完整字段、单位、日期覆盖、空目标语义和最小构造例见
`docs/product/03-user-guide.md` 的“Combo bundle v1”一节。框架在盘前执行目标调仓，在开盘之后
因果地提交已冻结的当日收盘单，并把三份输入及 manifest 写入结果工件。GUI 的“本地数据”
面板也可选择同一 combo 目录。manifest 上限为 1 MiB，每份 CSV 上限为 128 MiB；超限
或读取期间身份变化会 fail closed。

一次成功的 CLI 运行会原子发布一个已自校验的 `RunArtifact v1`。规范成员包括根目录的
`manifest.json`、`config.json`、`provenance.json`、`result.json`，`inputs/strategy.py`，有
信号输入时的 `inputs/signals.csv`，以及按结果类型生成的 `evidence/` 和 `tables/`。为方便
现有脚本查看，根目录仍保留 manifest 已列出的 `strategy.py`、`summary.json`、
`equity_curve.csv` 和有成交时的 `orders.csv` 兼容视图。失败运行也会尽力发布带诊断信息、
不可排名的 v1 工件；若失败本身发生在工件发布阶段，则不会覆盖或伪造目标目录。

先检查 `summary.json` 中的 `result_contract.status`、`rankable`、`warnings` 和 `assumptions`；
需要消费规范成员时，用 `diepi.artifacts.ArtifactStore.load()` 重新验证目录。只有 manifest、
成员 hash 与结果语义全部通过后，加载对象的 `artifact_verified` 才为 true。进程退出成功和
工件完整都不等于任意跨结果比较成立。

旧的 `ResultStorage` 目录只能通过 `ArtifactStore.load_legacy()` 或 `load_legacy_result()`
只读加载，并始终是 `artifact_verified=false`、`is_rankable=false`；目录内即使带有
`SUCCESS` 结果契约也不会自动升级信任等级。

两个 v1 现金结果可以用正式 parity 报告比较：

```bash
diepi compare runs ./diepi_results/baseline ./diepi_results/candidate \
  --report ./parity.md
```

比较器不会自动取日期交集，并把现金/市值/净值/成交的经济账本差异与年化、回撤等指标
定义差异分开报告；完整账本还核对起始 seed、成交事件顺序、费用分项、现金变动和终态。
Python 原始结果只能得到顶层 `UNATTESTED` 的诊断投影；正式成功认证会重新从磁盘验证两侧
RunArtifact，而不信任调用方可变的内存缓存。旧目录必须显式使用
`--allow-unverified-legacy`，此时即使共同字段的诊断投影为 `EXACT`，命令顶层仍为
`UNVERIFIED` 并返回非零，不会因此变成已验证。`--json` 输出以 `command_report` 和
`report_path` 分离，摘要只覆盖前者。已验证但 `PARTIAL/INVALID/FAILED/CANCELED`
等不可排名结果同样只能得到顶层 `NOT_RANKABLE` 诊断，不能成功认证 parity。

## 图形界面

安装 `gui` 可选依赖后运行：

```bash
diepi gui \
  --data-root /path/to/market-data \
  --results-root ./diepi_results
```

GUI 是正式支持的本地 Python/wheel 入口，可编辑策略、载入内置 MA5/MA20 示例、配置
`raw/dual/hfq` 与日线开收盘容量，并查看资产净值、结果自带的收盘净值回撤、成交、持仓、
订单事件和个股明细。CLI 和 GUI 指向同一个 `--results-root` 时，CLI 自动发布的
`RunArtifact v1` 会出现在 GUI 历史页；独立并行排行也可双击进入完整 child 结果。

历史工件中的资产、交易和审计证据不依赖原行情目录。K 线本身不嵌入工件：只有当前
`--data-root` 中对应日线 direct-file 与日线运行前后锁定并写入工件的 SHA-256 指纹一致时，
GUI 才允许显示 K 线并叠加成交；缺文件或内容变化只会禁用这项辅助视图，不会把当前数据
伪装成历史证据。分钟运行不会借用日线指纹声称输入已被证明，历史工件也不会继续下钻到
未被该证据覆盖的分钟文件。对组合结果或独立并行汇总点击
“保存”会发布 v1；历史页验证 v1 后再展示，并把可读取的旧 `ResultStorage` 明确标成未验证。
GUI 不是安全沙箱，也不意味着所有高级/实验引擎均已
图形化。重要结论仍应回到结果契约和工件验证状态。

## 先知道的研究边界

- “本地优先”描述内置读写路径；受信任的本地 Python 策略和第三方依赖仍可能自行联网。
- 现金撮合基于 OHLC/bar，不模拟委托簿排队。流动性帽是容量上限，不是冲击成本模型。
- raw OHLC 与品种/生效日价格带冲突时运行会失败，不会靠双向夹价制造一个可成交结果；
  `limit_pct_overrides` 是压力参数，不是修补未知证券规则的捷径。
- 日线集合竞价必须显式配置容量；费用和部分历史规则需要研究者复核。
- 默认费用按分使用 Decimal half-up；这是确定、可审计的模型假设，并非全国统一的券商
  佣金尾数规则。实盘校准应以券商协议和交割单为准。
- 双价格轨失败时不会静默取交集或用另一轨补齐。
- 只有完整 `SUCCESS` 结果具备基础排名资格；跨结果比较还需证明观察范围一致。
- 历史回测不能消除前视偏差、幸存者偏差、过拟合、数据错误和真实交易风险。

## 文档与参与

PyPI 会直接渲染本 README，因此这里不使用依赖某个尚未公布仓库地址的相对链接。仓库内
的深入文档路径如下：

- `docs/product/01-author-note.md`：作者序与项目缘起；
- `docs/product/02-core-features.md`：因果、双轨、规则、审计与可比性；
- `docs/product/03-user-guide.md`：完整安装、数据、策略、运行和结果手册；
- `docs/product/04-reference-and-boundaries.md`：精确支持矩阵、默认值和失败边界；
- `docs/product/05-local-market-data-format-v1.md`：官方 Parquet 目录、字段、单位、切片与 Agent 适配契约；
- `CONTRIBUTING.md`：贡献与测试要求；
- `SECURITY.md`：安全报告方式；
- `THIRD_PARTY_NOTICES.md`：运行时、GUI 与开发依赖的许可证披露；
- `CHANGELOG.md`：用户可见变更。

本文不虚构尚未存在的仓库 URL、邮箱或下载地址。正式公开渠道建立后，再用实际地址替换
上述纯文字路径。

## 免责声明

dieΠ 是回测研究工具，不构成投资建议，也不承诺任何收益。历史回测不能替代对数据质量、
来源权利、前视偏差、过拟合、容量假设和真实交易风险的独立检查。
