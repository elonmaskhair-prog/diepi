# dieΠ 用户手册

> 适用版本：`0.1.0`（Alpha）
> 本手册只描述当前仓库可以验证的行为。遇到“实验性”“部分支持”时，请先阅读[参考与边界](04-reference-and-boundaries.md)。
> 源码链接中的行号对应本文编写时的工作树；以后若代码移动，请优先按链接旁的类名或函数名搜索。

> 文档导航：[项目首页](../../README.md) · [目录](README.md) · [作者序（可选）](../../README.md#写在项目前为什么做-dieπ) ·
> [核心功能](02-core-features.md) · [用户手册](03-user-guide.md) · [参考与边界](04-reference-and-boundaries.md) ·
> [本地行情数据格式 v1](05-local-market-data-format-v1.md)

## 1. 先选择你的使用路径

dieΠ 的普通用户闭环包括诊断、数据校验、日线现金运行和 GUI；Python API 用于需要精确
控制的研究。首批用户仍需自行准备有权使用的本地行情。

| 你想做什么 | 建议入口 | 当前状态 |
| --- | --- | --- |
| 检查安装、数据根和 GUI 依赖 | `diepi doctor` | 正式支持；只读 |
| 无真实行情先走通完整流程 | `diepi demo` | 正式 onboarding；仅 generated synthetic |
| 复制一个可编辑的最小策略 | `diepi examples copy ma-cross` | wheel 内置 MA5/MA20 教学例 |
| 校验自己的日线数据范围 | `diepi data validate` | 正式支持；默认只读 |
| 用一个策略文件完成股票池回测 | CLI | 稳定主入口 |
| 精确控制单标的或共享资金组合 | Python API | 稳定主入口 |
| 将同一策略独立应用到多个标的 | `ParallelRunner` | 部分支持；不是组合账户 |
| 交互式编辑和查看图表 | PySide6 GUI | 正式支持；随 Python 包/wheel 安装 |

股指期货使用 `diepi.futures` 下的独立日线近似引擎，不与股票账户混合，属于实验范围。
分钟现金和独立并行属于高级路径。第一次使用建议从 synthetic demo 或少量日线标的开始；
需要定制引擎参数或自行组织实验时，再进入 Python API。

> 源码定位：[`pyproject.toml:5`](../../pyproject.toml#L5)（版本、依赖和命令入口）；[`diepi/backtest/engine/__init__.py:5`](../../diepi/backtest/engine/__init__.py#L5)（公开现金市场引擎）；[`diepi/futures/engine.py:210`](../../diepi/futures/engine.py#L210) — `FuturesEngine`。

## 2. 安装

### 2.1 环境要求

- Python 3.10 或更高版本；
- 核心依赖为 Pandas、NumPy 和 PyArrow；
- GUI 需要额外安装 PySide6 和 pyqtgraph；
- 开发测试需要 pytest。

从源码目录安装：

```bash
python -m venv .venv

# PowerShell
.venv\Scripts\Activate.ps1

# Linux/macOS shell
# source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -e .
```

需要 GUI 或测试工具时：

```bash
python -m pip install -e ".[gui]"
python -m pip install -e ".[dev]"
```

当前发行只公开项目自有命名空间 `diepi.backtest`、`diepi.futures`、
`diepi.artifacts` 和 `diepi.examples`，不再同时安装旧的通用顶层兼容包。升级旧工作区时，应先清理旧安装，
再在新的虚拟环境中安装并更新策略导入路径。

GUI 是正式可选功能，但交付边界是 Python 包或 wheel 加上 `gui` 依赖组。项目当前没有
standalone `.exe`、`.dmg` 或 Linux 桌面安装器，也不承诺由操作系统应用商店分发。

> 源码定位：[`pyproject.toml:12`](../../pyproject.toml#L12)（Python 版本）；[`pyproject.toml:21`](../../pyproject.toml#L21)（核心依赖）；[`pyproject.toml:27`](../../pyproject.toml#L27)（可选依赖）；[`pyproject.toml:32`](../../pyproject.toml#L32)（`diepi` 命令入口）；[`pyproject.toml:37`](../../pyproject.toml#L37)（当前包结构）。

### 2.2 先诊断，再运行 synthetic demo

```bash
diepi doctor
diepi demo diepi_demo
```

`doctor` 不写文件，也不导入 GUI；它检查 Python、核心依赖、可选 GUI 依赖以及解析后的
数据/结果路径。还没有真实数据时，数据根检查失败是可解释的状态。

`demo` 会创建全新的 `diepi_demo/`，生成确定性 synthetic 日线数据与 manifest，自校验后
默认运行一次回测。已有目录不会被覆盖。只生成和校验可使用：

```bash
diepi demo diepi_demo --generate-only
```

demo 的全部价格、成交量、交易日和证券标签均由程序生成，不是真实行情、匿名化样本或
可用于策略评价的历史。它只回答“安装和产品链路是否跑通”。

### 2.3 5 / 15 / 30 分钟闭环

5 分钟路径不需要真实行情；运行 demo 后立即用同一数据根和结果根打开 GUI：

```bash
diepi demo diepi_demo
diepi gui --data-root diepi_demo/market-data --results-root diepi_demo/results
```

进入“历史记录”，双击 `synthetic_demo` 查看净值、回撤和交易。15 分钟路径使用源码/sdist
附带的四证券真实格式切片；wheel 不包含该切片：

```bash
diepi data validate --data-root examples/market_data_v1/data --symbols 600000.SH,000001.SZ,510300.SH,159915.SZ --start 20260101 --end 20260630 --price-mode dual
diepi examples copy ma-cross ./ma_cross_strategy.py
diepi run ./ma_cross_strategy.py --data-root examples/market_data_v1/data --results-root ./diepi_results --symbols 600000.SH,510300.SH --start 20260101 --end 20260630 --price-mode dual --stamp-duty auto --daily-open-previous-day-ratio 0.1 --name public-ma-mixed
diepi gui --data-root examples/market_data_v1/data --results-root ./diepi_results
```

30 分钟路径把策略信号换成单独 CSV，但复用同一行情、现金引擎、结果根和 GUI：

```bash
python -c "from pathlib import Path; Path('signals_mixed.csv').write_text('date,symbol,target_weight\n20260106,600000.SH,0.5\n20260106,510300.SH,0.4\n20260302,600000.SH,0.2\n20260302,510300.SH,0.7\n20260601,600000.SH,0\n20260601,510300.SH,0\n', encoding='utf-8')"
diepi run --signals ./signals_mixed.csv --signals-format target --data-root examples/market_data_v1/data --results-root ./diepi_results --start 20260101 --end 20260630 --price-mode dual --stamp-duty auto --daily-open-previous-day-ratio 0.1 --name public-signals-mixed
diepi gui --data-root examples/market_data_v1/data --results-root ./diepi_results
```

GUI 历史会同时读取 `public-ma-mixed` 与 `public-signals-mixed`。双击运行看组合净值和原生
回撤，双击成交行看个股交易和经行情内容指纹核验的 K 线。源码/sdist GUI 的“载入公开
样例”按钮会填入与 15 分钟路径相同的完整配置；wheel 中该按钮因不含真实切片而禁用。

## 3. 准备本地数据

### 3.1 指定数据根目录

dieΠ 不会替你下载或上传行情。它读取你本地的约定式 Parquet 数据仓库；数据取得、授权、
清洗和经济含义由使用者负责。

PowerShell：

```powershell
$env:DATA_ROOT = (Resolve-Path ".\market-data").Path
```

Linux/macOS：

```bash
export DATA_ROOT=/path/to/market-data
```

新命令的显式 `--data-root` 优先于 `DATA_ROOT`。如果显式目录不存在，运行会报错，不会
悄悄回退到另一份数据；诊断和校验命令会返回结构化失败信息。未设置时，源码工作区仍
保留从仓库布局推导数据根的兼容行为，安装后的调用则应显式传参或设置环境变量。

> 源码定位：[`diepi/backtest/config.py:28`](../../diepi/backtest/config.py#L28) — `_detect_data_root()`；[`diepi/backtest/config.py:66`](../../diepi/backtest/config.py#L66) — `DATA_ROOT`、`PARQUET_ROOT` 和 `METADATA_ROOT`。

结果根采用同样的显式优先原则：`--results-root` 优先于 `DIEPI_RESULTS_DIR`，源码工作区
默认使用仓库内的 `diepi_results/`，安装后的调用默认使用当前目录下的 `diepi_results/`。
路径解析本身不创建目录；真正运行或保存时才按对应入口的不可覆盖规则写盘。

### 3.2 核心目录结构

本节只用于快速定位。每个 Parquet 的必需/可选字段、dtype、单位、日线与分钟
粒度、复权因子锚点和配套切片，以[本地行情数据格式 v1](05-local-market-data-format-v1.md)
为准。

常见的最小目录如下：

```text
DATA_ROOT/
└─ parquet/
   ├─ metadata/
   │  ├─ common/trade_cal.parquet                # 可选完整 override
   │  ├─ common/industry/mapping.parquet       # 行业池需要
   │  └─ stock/basic.parquet                   # 全市场/点时股票池需要
   └─ timeseries/
      ├─ daily/{symbol}.parquet                # 后复权日线
      ├─ daily_raw/{symbol}.parquet            # 不复权日线
      ├─ adj_factor/{symbol}.parquet            # 普通股票复权因子
      ├─ minute/{symbol}/{year}.parquet        # 后复权分钟线
      ├─ minute_raw/{symbol}/{year}.parquet    # 不复权分钟线
      └─ index_daily/...                       # Benchmark 需要
```

ETF/LOF 对应使用 `etf_daily`、`etf_daily_raw`、`etf_minute`、`etf_minute_raw`
和 `etf_adj_factor` 路由；日线还保留按交易日截面文件读取 ETF 的兼容路径。

现金引擎默认使用内置 `cn-a-share-2010-2026-v1` 市场时钟，覆盖
`20100101..20261231` 的每个自然日，所以该范围内不要求用户提供交易日历文件。本地
`trade_cal.parquet` 一旦存在，就作为完整 local override：框架不会把它与内置日期拼接，
并严格要求自然日连续、`is_open` 只有 0/1 且覆盖请求范围。覆盖期外没有合格 override
时会 fail closed；交易日始终不会从某只证券的行情行反推。

`dual` 模式需要同一标的、同一观察范围内的研究价、执行价和复权因子。内置
`DataProvider` 缺任一条轨或缺因子都会失败，不会自动退化；如果研究本来只需要单一价格
空间，应显式选择 `--price-mode hfq` 或 `--price-mode raw`。结果中带兼容 warning 的
单轨退化仅针对没有严格 pair API 的旧 provider，不应当作内置数据层的正常兜底。

核心行情原生格式是 Parquet。CSV 可用于交易清单和少量旧数据兼容，但不是通用行情存储后端；Excel、自定义目录适配器和通用分块读取不是当前稳定接口。

> 源码定位：[`diepi/backtest/data/cache_manager.py:61`](../../diepi/backtest/data/cache_manager.py#L61) — `CacheConfig.PARQUET_DIR_MAP`；[`diepi/backtest/data/cache_manager.py:165`](../../diepi/backtest/data/cache_manager.py#L165) — `ParquetReader`；[`diepi/backtest/data/cache_manager.py:263`](../../diepi/backtest/data/cache_manager.py#L263) — `_read_minute_data()`；[`diepi/backtest/data/data_provider.py:434`](../../diepi/backtest/data/data_provider.py#L434) — `DataProvider.get_daily()`；[`diepi/backtest/data/data_provider.py:486`](../../diepi/backtest/data/data_provider.py#L486) — `DataProvider.get_minute()`。

### 3.3 数据契约失败意味着什么

双轨行情会检查必需字段、时间键、重复记录、排序、OHLC 关系、`pre_close`、`amount` 以及两轨键集合。校验失败默认终止回测；不要为了“先跑出一个数”而删除检查。

当前契约不等于供应商数据质量认证。它不承诺自动发现所有缺失交易日、上市/退市越界记录或每个交易日的全部分钟槽。

按范围的数据校验同样是只读门禁：它不会下载、修复、排序、取交集或填充行情。校验通过
表示所请求标的、日期、频率和价格轨可以进入当前严格契约，不表示数据来源已经获授权，
也不证明行情在经济意义上真实或完整。真实研究数据始终由用户自己负责。

```bash
diepi data validate \
  --data-root /path/to/market-data \
  --symbols 000001.SZ \
  --start 20240101 \
  --end 20241231 \
  --price-mode dual
```

`--symbols` 可以重复，也可以传逗号分隔列表。默认不写任何文件；只有显式 `--report`
才会把确定性 JSON 报告写到用户指定路径。`--skip-manifest` 只跳过可选 manifest 身份检查，
不会关闭行情 pair 契约。

已有大型本地数据湖时，可以在本机生成一个限定标的和日期的私有工作区，而不复制整库：

```bash
diepi data extract \
  --source-data-root /path/to/source-root \
  --workspace /path/to/new-private-workspace \
  --symbols 159915.SZ,510300.SH \
  --start 20260112 \
  --end 20260515 \
  --include-metadata
```

`--source-data-root` 仍是包含 `parquet/` 的根目录。抽取器只读源文件、拒绝覆盖目标，先在
同级临时目录构建，再经 manifest 和范围校验后原子发布。它会保留范围前一交易日、
raw/HFQ 双轨，以及复权因子源文件首行锚点；不会读取或复制源交易日历，也不会搜索或
复制策略信号。输出 scope 固化内置日历身份。行情、因子和 basic 元数据只接受框架公开
的列集合；未知列会使抽取失败，不会被
静默带入工作区。basic 元数据必须有 `ts_code` 或 `symbol`，因此不会在身份列缺失时复制
整张表。Parquet/DataFrame 的不透明自定义 attributes 也会被剥离，不会绕过列边界进入
输出。输出在 POSIX 使用 `0700/0600`，Windows 使用仅对象所有者与 LocalSystem 可访问
的受保护 ACL。默认错误只给稳定错误码；需要查看本地路径和底层异常时显式加
`--verbose-errors`。
`extraction_scope.json` 不记录源绝对路径，并把数据标为用户提供、私有、默认不可再分发。
这只是技术上的最小化与脱敏，不会把供应商数据变成可公开数据；是否能上传、截图或转交
仍由用户的数据协议决定。

抽取器的并发模型是本机可信、单写者：运行期间不要让另一个进程替换源数据树或目标父
目录。实现会拒绝 symlink/junction/reparse point，核对文件与目录身份，并在 Windows、
Linux 和 macOS 使用不覆盖既有目标的原子发布原语；其他平台会 fail closed。但它不是
用来对抗同一账号下恶意进程持续抢占路径的系统级沙箱。

> 源码定位：[`diepi/backtest/data/contract.py:701`](../../diepi/backtest/data/contract.py#L701) — `DataQualityReport`；[`diepi/backtest/data/contract.py:825`](../../diepi/backtest/data/contract.py#L825) — `AlignedMarketData`；[`diepi/backtest/data/contract.py:2233`](../../diepi/backtest/data/contract.py#L2233) — `validate_and_align_pair()`。

## 4. 第一次回测

先从安装包复制一份可编辑、不会随仓库路径消失的策略：

```bash
diepi examples list
diepi examples copy ma-cross ./ma_cross_strategy.py
```

下面展示 raw-minimal 路径。一个显式标的只需对应 `daily_raw`（ETF/LOF 为
`etf_daily_raw`）Parquet，列为 `trade_date,open,high,low,close,pre_close,amount`；交易
日历由内置版本提供：

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

PowerShell 可以写成一行，或者使用反引号续行。日线策略若提交开盘集合竞价单，必须显式提供开盘容量；框架不会猜测“你大概能成交多少”。可选方式是固定金额帽 `--daily-open-cap-yuan`，或以前一交易日成交额的比例表示。

`--name` 必须是 1–128 个字符的可移植单路径标识：首字符为 ASCII 字母或数字，其余只可
使用 ASCII 字母、数字、点、下划线或连字符，也不能是系统保留名。输出索引或结果目录中
已有同名运行时，CLI 会拒绝覆盖；重复练习时请改用新名称，或省略该参数让框架生成带
时间戳的名称。

同一命令也可以写成一行：

```bash
diepi run ./ma_cross_strategy.py --data-root /path/to/market-data --symbols 000001.SZ --start 20240101 --end 20241231 --price-mode raw --daily-open-previous-day-ratio 0.1
```

示例是真正的相邻时点 crossing：截至 T-1 的 MA5 从不高于 MA20 变为高于时买入，从不低于
变为低于时卖出，订单进入 T 日开盘。它至少需要 21 个已完成日线观测；订单若被拒绝，
不会只因均线继续处于同一侧而每天重试。

原有 `diepi strategy.py ...` 仍作为 `diepi run strategy.py ...` 的兼容简写；新脚本和文档
使用显式 `run`，便于与 `doctor/data/demo/examples/gui` 区分。

### 4.1 三种输入，共用一条执行边界

一次组合回测从下列三种输入中选择一种；三者互斥，不是同时叠加的三层配置：

| 输入 | CLI | GUI | `date=T` 的核心时间语义 |
| --- | --- | --- | --- |
| 策略代码 | `diepi run strategy.py` | “策略代码” | 由策略生命周期决定；盘前、开盘后、分钟和日线回调的边界见第 6 节 |
| 简单 signals | `diepi run --signals signals.csv` | “signals CSV” | 完整清单在运行前冻结；T 行在 T 日盘前重放，提交 T 日开盘目标/动作 |
| 冻结 combo | `diepi run --combo-bundle DIR` | “冻结 combo” | T 日目标在盘前提交；运行前已知的 T 日 `close_sells` 在开盘后调度到 T 日收盘 |

CLI 和 GUI 的对称目标是：同一种输入使用同一个校验器、因果重放策略、现金引擎和工件
快照语义；两者的界面形态不必相同。框架真正执行的是规范化的订单、目标权重和定时收盘
意图。signals 的 target/action CSV 是推荐的内置适配器之一，不是唯一上游格式：研究者
可以在代码策略中直接生成意图，也可以先把数据库查询、模型结果或个人文件格式转换成
受支持的 signals/combo。框架不宣称能直接解释任意 Excel、数据库表或私有 CSV。

简单 signals 的 `date=T` 绝不表示“读取 T 日收盘后再回填 T 日成交”。如果一条信号使用了
T 日收盘价或完整 T 日 OHLC，执行日期至少应写成下一交易日；只有运行前已经冻结、并明确
需要在 T 日收盘退出的指令，才适合 combo 的 `close_sells`。代码策略则必须遵守第 6 节的
同一因果边界。

### 4.2 在同一现金组合中混合股票与 ETF/LOF

`PortfolioEngine` 的显式池可以同时包含规则簿支持的 A 股和 ETF/LOF；它们争用同一份现金
和冻结资源，但按 symbol 分别应用数据路由、两位/三位价格精度、涨跌停、申报单位和
T+0/T+1。例如：

```bash
diepi run ./my_portfolio_strategy.py \
  --data-root /path/to/market-data \
  --symbols 600000.SH,511010.SH \
  --start 20240102 --end 20241231 \
  --price-mode raw \
  --stamp-duty auto \
  --daily-open-previous-day-ratio 0.1
```

股票 raw 文件放 `daily_raw/`，ETF/LOF raw 文件放 `etf_daily_raw/`；默认 dual 模式再分别
使用 `daily/adj_factor` 与 `etf_daily/etf_adj_factor`。signals 未另传 `--symbols` 时从
清单推导 scope，combo 由 manifest 冻结 scope；GUI 的“指定证券”、signals 和 combo 也
遵循同一规则。`ALL_MARKET` 是股票主数据池，不会自动并入 ETF，因此混合研究必须给出显式
scope。

必须注意以下边界：

- 只有 `RuleBook.require_supported(..., CASH)` 接受的证券才能成交；REIT、指数和未知/歧义
  代码会失败，不能因为文件放进 `etf_*` 目录就把它当 ETF 交易；
- 股票默认 T+1；只有代码可无歧义确认的沪市 `511/513/518` ETF 默认 T+0，其他 ETF/LOF
  保守按 T+1，除非 Python API 注入证券主数据规则或显式 T+0 override；
- 默认 `stamp_duty=auto` 按交易日解析股票卖出税率，并令场内基金卖出免税；混合组合应
  保持该默认值或显式传 `auto`。固定数值是整个账户的一刀切口径；
- `transfer_fee_rate` 也是整个账户共享的数值，默认 0，当前没有按日期和品种自动切换；
- raw/raw 不应用因子公司行为覆盖。跨分红、送转或 ETF 分配范围时使用 dual，并理解当前
  因子模型是即时总回报再投资近似，不是逐项权益和税收引擎。

项目用完全合成数据回归同一正式 runner 中股票与 ETF 的共享现金成交、两条数据路由、
涨跌停 tick、T+0/T+1 和 `auto` 印花税；见
[`test_formal_runner_mixes_stock_and_etf_in_one_cash_portfolio`](../../tests/backtest/test_cli_artifact_integration.py)。

### 4.3 Combo bundle v1：完整输入契约

如果研究输入是三张已经冻结的组合信号表——盘前目标权重、当日收盘退出和完整交易日
scope——应使用 combo 入口。规范目录的最小结构是：

```text
my-combo/
├─ targets.csv
├─ close_sells.csv
├─ daily.csv
└─ diepi_combo.json       # 可选；严格身份 manifest
```

三份 CSV 必须是 UTF-8（可带 BOM）的普通文件，每份不超过 128 MiB；表头不能重复、带首尾
空格或空列名，每一数据行的字段数必须与表头相同。日期可写 `YYYYMMDD` 或 `YYYY-MM-DD`，
装载后统一为 `YYYYMMDD`；证券必须是 `000001.SZ` 这类六位代码加 `.SH/.SZ/.BJ`。权重是
`0..1` 的有限小数，例如 `0.25` 表示 25%，不是 25。

| 文件 | 必需列 | 约束与执行语义 |
| --- | --- | --- |
| `targets.csv` | `trade_date,symbol,target_weight` | 整张表至少一行；同日同标的唯一；日期必须在 `daily` 内；每日权重和不超过 1，并必须等于 `daily.invested_weight`。每个 `daily` 日期是一份完整组合目标：该日没出现的标的目标为 0，不是 signals 的“无指令”。目标在 T 日盘前提交到 T 日开盘。 |
| `close_sells.csv` | `trade_date,symbol` | 可以只有表头；同日同标的唯一；日期必须在 `daily` 内。可选 `exit_price` 只能留空或写 `close`。这些退出必须在回测开始前已知，T 日开盘后调度到 T 日收盘；同日列入这里的标的不会再执行开盘 target 调整。 |
| `daily.csv` | `date,invested_weight,cash_weight` | 每个请求区间内的引擎交易日恰好一行，日期唯一且严格递增；两个权重都在 `[0,1]`，且和为 1。若某日 `invested_weight=0` 且 targets 无该日行，表示显式空仓；遗漏引擎交易日会在执行时失败。 |

未知附加列目前不会参与执行（`close_sells.exit_price` 除外），但会作为原始字节进入工件
hash；为了让适配器和人工审查稳定，v1 输入建议只保留上表列。以下是可直接构造并验证的
三日最小例。先创建目录，再把三个代码块分别保存为对应文件：

`targets.csv`：

```csv
trade_date,symbol,target_weight
20260105,600000.SH,0.5
20260105,510300.SH,0.4
20260106,600000.SH,0.5
20260106,510300.SH,0.4
20260107,510300.SH,0.4
```

`close_sells.csv`：

```csv
trade_date,symbol,exit_price
20260106,600000.SH,close
```

`daily.csv`：

```csv
date,invested_weight,cash_weight
20260105,0.9,0.1
20260106,0.9,0.1
20260107,0.4,0.6
```

在不运行回测的情况下，用正式只读命令校验并查看派生 manifest；目录名若含空格等不可移植
字符，必须显式传一个 1–128 字符的安全 tag：

```bash
diepi combo validate my-combo --tag mixed-v1
diepi combo validate my-combo --tag mixed-v1 --json
```

该命令严格复用回放所用的 canonical `ComboReplayBundle` 装载器，只向 stdout 输出稳定、
不含源绝对路径的摘要；不会生成、补写或覆盖 `diepi_combo.json`。退出码 0 表示验证通过，
1 表示 bundle 无效，2 表示命令用法错误，3 保留给内部验证器故障。

`diepi_combo.json` **不是首次装载必需文件**。当规范三份 CSV 都存在但 manifest 缺失时，
装载器会从实际字节派生 scope、行数、SHA-256 和语义；tag 取安全的目录名，或取
`--combo-tag`。如果希望输入目录本身也带严格身份清单，可由同一装载结果生成：

```bash
python -c "from pathlib import Path; from diepi.backtest.cli.combo_bundle import load_combo_bundle; p=Path('my-combo'); b=load_combo_bundle(p, tag='mixed-v1'); (p/'diepi_combo.json').write_bytes(b.manifest_bytes())"
```

manifest 一旦存在，就必须小于 1 MiB，使用 schema `diepi.combo_replay_bundle` v1，且其 tag、
规范文件名、scope、行数、三份 CSV 的 SHA-256 和 semantics 必须与实际字节完全一致；多余或
缺失字段同样拒绝。无论源目录是否自带 manifest，成功运行的 `RunArtifact v1` 都会在
`inputs/combo/` 保存三份精确 CSV 和框架派生的规范 manifest，不记录源绝对路径。

运行完整 combo：

```bash
diepi run \
  --combo-bundle /path/to/my-combo \
  --data-root /path/to/market-data \
  --results-root ./diepi_results \
  --cash 10000 \
  --stamp-duty auto \
  --daily-open-cap-yuan 1000000000 \
  --daily-close-cap-yuan 1000000000 \
  --name frozen-combo
```

旧式 `new_combo_*_<tag>.csv` 目录仍可配 `--combo-tag`。证券范围由 bundle 冻结，不能用
`--symbols` 替换；未给 `--start/--end` 时采用 daily 的完整范围。目标权重在盘前提交，
预先已知的 T 日收盘退出在 T 日开盘后调用 `schedule_at_close`，不会错误推迟到 T+1。
GUI 的组合模式也可选择同一目录；此时编辑器代码不作为实际回放逻辑。文件在读取前后都会
核对普通文件身份，并按实际读取字节数再次执行上限检查。

默认输出目录为仓库下的 `diepi_results/`。一次成功运行会原子发布一个封闭集合的
`RunArtifact v1`，典型结构如下：

```text
diepi_results/
├─ index.csv                         # 结果根索引，不属于任一工件
└─ quickstart/
   ├─ manifest.json
   ├─ config.json
   ├─ provenance.json
   ├─ result.json
   ├─ inputs/
   │  ├─ strategy.py
   │  └─ signals.csv                 # 仅传入信号文件时
   ├─ tables/
   │  ├─ daily_values.json
   │  ├─ trades.json
   │  └─ positions.json
   ├─ evidence/
   │  ├─ target_execution.json
   │  ├─ cash_replay_seed.json
   │  └─ execution_event_journal.json
   ├─ strategy.py                    # manifest 列出的兼容快照
   ├─ summary.json                   # manifest 列出的兼容摘要
   ├─ equity_curve.csv               # 有日值时
   └─ orders.csv                     # 有成交时
```

`tables/`、`evidence/` 的精确成员取决于引擎和结果，比较证据也可能出现。原始信号会规范化
快照为 `inputs/signals.csv`；为兼容旧脚本，还会以安全化的原文件名单独列入 manifest。
`index.csv` 只收录可排名运行；即使索引更新失败，已经发布并验证的工件仍然有效。

不要只看终值。先打开兼容视图 `summary.json`，确认
`result_contract.status == "SUCCESS"`、`rankable == true` 和 `artifact_verified == true`；
机器消费时再调用 `ArtifactStore.load(运行目录)` 验证 manifest、所有成员 hash 与结果语义。
失败运行会尽力保存 `summary.json`、`error.log`、`diagnostics/traceback.txt` 等不可排名的
诊断工件；工件发布本身失败时不会覆盖已有目录。

比较两个现金回测工件时，使用正式的 run-to-run 入口，而不是 benchmark 指数比较：

```bash
diepi compare runs ./diepi_results/baseline ./diepi_results/candidate \
  --atol 0.00000001 \
  --rtol 0 \
  --report ./parity-report.json
```

比较器要求观察日期 scope 完全一致，不会偷偷取交集；逐日 cash/market value/total value、
规范化成交（含事件顺序、费用分项与 cash delta）、起始 seed、终态和摘要分别比较，并把“经济账本是否一致”与“指标
定义是否完整且一致”分开报告。
默认只接受通过 `ArtifactStore.load()` 验证的 v1。旧目录必须显式
`--allow-unverified-legacy`；此时顶层状态固定为 `UNVERIFIED`、退出码非零，账本子结论
只作共同字段诊断，绝不会把旧目录升级为已验证结果。直接比较 Python 原始对象时顶层为
`UNATTESTED`；只有重新验证的磁盘 RunArtifact 能成功认证。已验证但不可排名的结果顶层为
`NOT_RANKABLE`，也返回非零。`--report` 必须写到两个
运行目录之外，避免新增一个未列入 manifest 的文件而破坏原工件的封闭集合。

> 源码定位：[`diepi/cli.py:93`](../../diepi/cli.py#L93) — `main()`；[`diepi/cli.py:103`](../../diepi/cli.py#L103)（参数解析器与示例）；[`diepi/cli.py:168`](../../diepi/cli.py#L168)（`--name`）；[`diepi/backtest/cli/runner.py:99`](../../diepi/backtest/cli/runner.py#L99) — `_ensure_run_id_available`；[`diepi/cli.py:303`](../../diepi/cli.py#L303)（竞价帽参数）；[`diepi/backtest/cli/runner.py:552`](../../diepi/backtest/cli/runner.py#L552)（CLI 结果落盘）；[`diepi/backtest/result_contract.py:91`](../../diepi/backtest/result_contract.py#L91)（结果状态）；[`diepi/backtest/result_contract.py:370`](../../diepi/backtest/result_contract.py#L370) — `ResultContract.is_rankable`。

## 5. 编写策略

### 5.1 CLI 的模块级函数策略

CLI 最直接的写法是在 `.py` 文件中定义回调函数。仓库内的均线示例展示了这一形式：

```python
FAST_PERIOD = 5
SLOW_PERIOD = 20
TARGET_WEIGHT = 0.95


def moving_average_cross(close, fast_period=FAST_PERIOD, slow_period=SLOW_PERIOD):
    if fast_period <= 0 or slow_period <= 0 or fast_period >= slow_period:
        raise ValueError("periods must satisfy 0 < fast_period < slow_period")
    if close is None or len(close) < slow_period + 1:
        return False, False
    previous = close.iloc[:-1]
    previous_fast = previous.tail(fast_period).mean()
    previous_slow = previous.tail(slow_period).mean()
    current_fast = close.tail(fast_period).mean()
    current_slow = close.tail(slow_period).mean()
    return (
        previous_fast <= previous_slow and current_fast > current_slow,
        previous_fast >= previous_slow and current_fast < current_slow,
    )


def on_before_market_open(ctx):
    pool = ctx.get_stock_pool()
    for symbol in pool:
        daily = ctx.get_daily(symbol, days=SLOW_PERIOD + 1)
        if daily is None or len(daily) < SLOW_PERIOD + 1:
            continue

        crossed_up, crossed_down = moving_average_cross(
            daily["close"], FAST_PERIOD, SLOW_PERIOD
        )
        position = ctx.get_position(symbol)
        has_position = position is not None and position.shares > 0

        if crossed_down and has_position:
            ctx.order_target_percent(symbol, 0.0, when="open")
        elif crossed_up and not has_position:
            ctx.order_target_percent(symbol, TARGET_WEIGHT, when="open")

    return pool


def on_day(ctx, bars):
    pass
```

可以用重复的 `--param` 覆盖模块级简单变量：

```bash
diepi run my_strategy.py \
  --symbols 000001.SZ,600000.SH \
  --start 20240101 --end 20241231 \
  --daily-open-previous-day-ratio 0.1 \
  --param FAST_PERIOD=10 \
  --param TARGET_WEIGHT=0.2
```

覆盖发生在策略文件执行之后。因此，如果模块导入时已经用旧参数计算了另一个常量，那个派生值不会自动重算。

模块级编译名单包含公开生命周期回调，包括 `on_after_open` 和 `on_before_close`。也可以让
策略文件只保留一个 `PortfolioStrategy` 子类，由 CLI 自动选择或通过 Python API 运行。
不要在函数和类混合文件中同时定义回调，函数式回调会优先。`diepi run` 使用 portfolio
契约，因此单标的 `Strategy` 子类应交给 `BacktestEngine` Python API；并行/GUI 独立模式
会显式用 single 契约编译，能接受唯一的 `Strategy` 子类并拒绝 `PortfolioStrategy`。

> 源码定位：[`examples/ma_cross_strategy.py:16`](../../examples/ma_cross_strategy.py#L16)（函数式示例）；[`diepi/backtest/cli/runner.py:145`](../../diepi/backtest/cli/runner.py#L145) — `compile_strategy()`；[`diepi/backtest/cli/runner.py:183`](../../diepi/backtest/cli/runner.py#L183)（当前模块级回调名单）；[`diepi/backtest/cli/runner.py:232`](../../diepi/backtest/cli/runner.py#L232) — `run_backtest()`。

### 5.2 类策略

Python API 提供两个基类：

- `Strategy`：单标的账户；
- `PortfolioStrategy`：多个证券共享现金和持仓。

类策略适合需要保存内部状态、明确构造参数或使用完整生命周期的场景。

> 源码定位：[`diepi/backtest/strategy/base.py:58`](../../diepi/backtest/strategy/base.py#L58) — `Strategy`；[`diepi/backtest/strategy/portfolio_strategy.py:77`](../../diepi/backtest/strategy/portfolio_strategy.py#L77) — `PortfolioStrategy`。

## 6. 理解策略生命周期

时间语义是回测可信度的核心，不是装饰性的回调名称。

| 回调 | 策略可见数据 | 这里创建的订单 |
| --- | --- | --- |
| `on_init` | 没有活动模拟日期；无参历史和日历查询不可用 | 不应下单 |
| `on_before_market_open` | 截止 T-1 的历史数据 | 开盘单可进入 T 日开盘窗口 |
| `on_after_open`（仅日线） | T 日开盘观察值；日线历史仍截止 T-1 | CLOSE 单可进入 T 日收盘窗口；其他单按下一合法窗口处理 |
| `on_minute`（仅分钟） | 当前刚完成的分钟 Bar，闭区间历史可包含该 Bar | 最早在下一合法执行窗口成交，绝不回填当前 Bar |
| `on_before_close`（仅分钟） | 已完成的连续竞价数据；尚看不到收盘竞价 Bar | 可为独立的收盘窗口提交订单 |
| `on_day`（仅日线） | 完整 T 日日线 | 这里创建的订单最早 T+1 生效 |
| `on_after_market_close` | T 日回测结果 | 仅统计；禁止新建交易订单 |
| `on_finish` | 最终清理后的账户 | `on_init` 成功后保证配对一次 |

“下一个交易日”可以由已知日历查询；“下一个 Bar 的行情”不能由策略读取。若一个指标需要未来价格才能计算，它就不属于当前决策时点。

> 源码定位：[`diepi/backtest/strategy/base.py:107`](../../diepi/backtest/strategy/base.py#L107) 至 [`diepi/backtest/strategy/base.py:203`](../../diepi/backtest/strategy/base.py#L203)（单标的生命周期契约）；[`diepi/backtest/strategy/portfolio_strategy.py:136`](../../diepi/backtest/strategy/portfolio_strategy.py#L136) 至 [`diepi/backtest/strategy/portfolio_strategy.py:242`](../../diepi/backtest/strategy/portfolio_strategy.py#L242)（组合生命周期）；[`diepi/backtest/engine/backtest_engine.py:1522`](../../diepi/backtest/engine/backtest_engine.py#L1522)（分钟收盘前因果边界）。

## 7. 查询行情、账户和交易日历

常用上下文能力包括：

```python
daily = ctx.get_daily("000001.SZ", days=30)
minute = ctx.get_minute("000001.SZ", days=1)
position = ctx.get_position("000001.SZ")
positions = ctx.get_positions()
cash = ctx.get_cash()
total_value = ctx.get_total_asset()
pool = ctx.get_stock_pool()
```

无参查询的上界由当前回调的因果边界决定，而不是由本地文件里“能读到多远”决定。`on_init` 没有当前日期，因此依赖当前时点的无参查询会明确报错；需要预热时，把查询移至盘前回调，或直接使用 `DataProvider` 并给出明确日期。

分钟查询的 `current_time` 是已经完成的 Bar 边界，闭区间可以包含当前完成 Bar，但不包含未完成或未来分钟。

> 源码定位：[`diepi/backtest/engine/context.py:18`](../../diepi/backtest/engine/context.py#L18) — `Context`；[`diepi/backtest/engine/portfolio_context.py:23`](../../diepi/backtest/engine/portfolio_context.py#L23) — `PortfolioContext`；[`diepi/backtest/data/data_provider.py:434`](../../diepi/backtest/data/data_provider.py#L434) — `get_daily()`；[`diepi/backtest/data/data_provider.py:486`](../../diepi/backtest/data/data_provider.py#L486) — `get_minute()`。

## 8. 下单

### 8.1 常用接口

| 目的 | 单标的/组合上下文方法 | 说明 |
| --- | --- | --- |
| 开盘买卖 | `buy_at_open` / `sell_at_open` | 进入合法开盘窗口；日线需显式竞价容量 |
| 连续市价买卖 | `buy_at_market` / `sell_at_market` | 按下一合法执行 Bar 的保守价格路径处理 |
| 限价买卖 | `buy_at_price` / `sell_at_price` | 由 Bar OHLC 判断是否触价 |
| 收盘买卖 | `buy_at_close` / `sell_at_close` | 进入独立收盘竞价窗口 |
| 止损/止盈/突破 | `sell_stop_loss` / `sell_stop_profit` / `buy_stop` | 跳空时使用开盘价并叠加方向滑点 |
| 目标权重 | `order_target_percent` | 生成目标仓位意图 |
| 组合再平衡 | `rebalance` | 多标的共享账户按目标权重执行 |
| 撤单 | `cancel_order` 等上下文接口 | 释放对应冻结资源 |

数量通常可以通过 `shares`、`amount` 或 `percent` 表达，具体组合由方法签名约束。连续市价和买入 STOP 的预算型请求在保守预冻结超出现金时，会缩至最大可承担合法数量并留下审计字段；显式 `shares` 不会偷偷缩量，资金不足时整单拒绝。

> 源码定位：[`diepi/backtest/engine/context.py:91`](../../diepi/backtest/engine/context.py#L91) 至 [`diepi/backtest/engine/context.py:484`](../../diepi/backtest/engine/context.py#L484)（单标的交易接口）；[`diepi/backtest/engine/portfolio_context.py:161`](../../diepi/backtest/engine/portfolio_context.py#L161) 至 [`diepi/backtest/engine/portfolio_context.py:779`](../../diepi/backtest/engine/portfolio_context.py#L779)（组合交易与目标接口）；[`diepi/backtest/broker/order.py:25`](../../diepi/backtest/broker/order.py#L25)（订单类型与状态）。

### 8.2 当前 Bar 不等于免费时间机器

限价和 STOP 条件由 OHLC Bar 判断，不具备逐笔委托簿。固定的价格路径及优先级用于获得确定性结果，不表示真实市场一定按该顺序运行。限价单采用触价模型，不模拟队列；开盘跳空到更优价格时使用开盘改善价。

同一 Bar 的成交量预算在该 Bar 内共享；部分成交后的剩余订单只能使用后续合法 Bar 的新预算。

> 源码定位：[`diepi/backtest/broker/broker.py:2292`](../../diepi/backtest/broker/broker.py#L2292) — `Broker._execute_orders_with_path()`；[`diepi/backtest/broker/broker.py:2360`](../../diepi/backtest/broker/broker.py#L2360) — `_check_order_trigger()`；[`diepi/backtest/broker/broker.py:1300`](../../diepi/backtest/broker/broker.py#L1300) — `_bar_liquidity_cap()`；[`diepi/backtest/liquidity.py:106`](../../diepi/backtest/liquidity.py#L106) — `DailyAuctionLiquidityPolicy`。

## 9. 配置价格、成本和容量

### 9.1 价格模式

CLI 提供：

- `dual`：默认。策略读取后复权研究价，撮合使用不复权真实价；
- `hfq`：研究和执行均使用后复权价，主要用于兼容研究；
- `raw`：研究和执行均使用不复权价。

`dual` 并不还原每一笔真实公司行为。当前会把达到重要性阈值的复权因子跳变近似为免税、即时总回报再投资，并以现金补零股；该假设会写入结果契约。

`raw/raw` 不需要复权因子，也不会应用因子公司行为覆盖；它按用户提供的原始价格和
`pre_close` 原样建模。这个最小路径适合 onboarding、接口集成和确认无公司行为的短范围，
不能把除权跳空自动解释为总回报。跨分红送转范围的正式研究应优先使用默认 `dual`。

> 源码定位：[`diepi/backtest/config.py:91`](../../diepi/backtest/config.py#L91)（三条价格轨默认值）；[`diepi/backtest/engine/price_mode.py:67`](../../diepi/backtest/engine/price_mode.py#L67) — `_convert_price_for_execution()`；[`diepi/backtest/engine/price_mode.py:84`](../../diepi/backtest/engine/price_mode.py#L84) — `_adjust_positions_for_corporate_actions()`。

### 9.2 费用与滑点

CLI 默认值包括：

| 参数 | 默认值 | 注意事项 |
| --- | ---: | --- |
| `--slippage` | `0.001` | 比例滑点；具体作用窗口见结果 assumptions |
| `--commission` | `0.00025` | 佣金率 |
| `--min-commission` | `5.0` | 单笔最低佣金 |
| `--stamp-duty` | `auto` | 默认按 symbol/交易日解析：支持的股票适用对应历史卖出税率，ETF/LOF 免税；传固定非负数会对整个账户一刀切 |
| `--transfer-fee-rate` | `0` | 双边过户费；没有历史自动切换表 |
| `--liquidity-cap-ratio` | `0.8` | 单 Bar 最大可吃成交额比例，不是冲击成本模型 |
| `--trading-days` | `252` | 年化交易日基数；CLI 提示 A 股研究可按口径改为 `244` |
| `--risk-free-rate` | `0.03` | Sharpe Ratio 使用的年化无风险利率 |

开盘卖出按原始开盘价；开盘买入默认采用 `open+slip`。收盘买卖采用方向滑点。若使用兼容模式，请根据结果里的 assumptions 判断实际路径，而不是只凭参数名猜测。

费用分项默认以十进制“分”为单位做 `ROUND_HALF_UP`。例如精确佣金 `13.815` 会记为
`13.82`，这是账户多扣一分钱、收益更保守，不是多给账户一分钱。Python 的
`round(float, 2)` 不是截断：它采用 nearest/ties-to-even，并先受到二进制浮点近似影响，
不能稳定代表某个十进制券商政策。当前默认值是确定、可审计的建模假设，不宣称是交易所
统一清算规则；需要对齐真实账户时，应以券商协议和交割单校准费率、最低佣金、分项/合计
舍入阶段及拆单作用域。

> 源码定位：[`diepi/cli.py:143`](../../diepi/cli.py#L143) 至 [`diepi/cli.py:155`](../../diepi/cli.py#L155)（CLI 成本参数）；[`diepi/cli.py:296`](../../diepi/cli.py#L296)（印花税解析）；[`diepi/backtest/broker/fees.py:132`](../../diepi/backtest/broker/fees.py#L132) — `FeeSchedule`；[`diepi/backtest/broker/fees.py:250`](../../diepi/backtest/broker/fees.py#L250) — `FeeEngine`。

### 9.3 日线集合竞价容量

只要策略可能提交 OPEN 或 CLOSE 订单，就必须为对应窗口提供一种容量来源：

```bash
--daily-open-cap-yuan 1000000
--daily-close-cap-yuan 1000000
```

或：

```bash
--daily-open-previous-day-ratio 0.1
--daily-close-previous-day-ratio 0.1
```

固定金额适合外部已经给出容量预算的研究；前日成交额比例更易随标的流动性变化。两者都是上限模型，不是盘口成交概率模型。

> 源码定位：[`diepi/backtest/liquidity.py:60`](../../diepi/backtest/liquidity.py#L60) — `AuctionCapSpec`；[`diepi/backtest/liquidity.py:106`](../../diepi/backtest/liquidity.py#L106) — `DailyAuctionLiquidityPolicy`；[`diepi/cli.py:303`](../../diepi/cli.py#L303) 至 [`diepi/cli.py:324`](../../diepi/cli.py#L324)（集合竞价容量参数）。

## 10. Python API

### 10.1 单标的引擎

```python
from diepi.backtest.engine import BacktestEngine
from diepi.backtest.liquidity import AuctionCapSpec, DailyAuctionLiquidityPolicy
from diepi.backtest.strategy import Strategy


class MyStrategy(Strategy):
    def on_before_market_open(self, ctx):
        daily = ctx.get_daily(days=20)
        if len(daily) >= 20 and ctx.get_position() is None:
            ctx.buy_at_open(percent=0.2)


engine = BacktestEngine(
    symbol="000001.SZ",
    start_date="20240101",
    end_date="20241231",
    initial_cash=1_000_000,
    freq="daily",
    daily_auction_liquidity=DailyAuctionLiquidityPolicy(
        open_cap=AuctionCapSpec.previous_day_ratio(0.1),
    ),
)
result = engine.run(MyStrategy())

contract = result.result_contract
print(contract.status.value, contract.is_rankable)
print(result.to_dict())
```

上述策略提交开盘单，因此示例显式传入了开盘集合竞价容量策略。若还会提交收盘单，请同时设置 `close_cap`。

对象属性 `result.total_return`、`result.annual_return` 和 `result.win_rate` 等使用小数比率
（例如 `0.10` 表示 10%）；`result.to_dict()` 为便于展示，会把同名收益、回撤和胜率字段
乘以 100。CLI 的 `summary.json.metrics` 保存的是对象的原始小数比率，控制台才格式化为
百分数。消费结果时不要把三种表示混在同一计算中。

> 源码定位：[`diepi/backtest/engine/backtest_engine.py:623`](../../diepi/backtest/engine/backtest_engine.py#L623) — `BacktestEngine`；[`diepi/backtest/engine/backtest_engine.py:639`](../../diepi/backtest/engine/backtest_engine.py#L639)（构造参数）；[`diepi/backtest/engine/backtest_engine.py:568`](../../diepi/backtest/engine/backtest_engine.py#L568) — `BacktestResult.to_dict()` 的展示单位；[`diepi/backtest/engine/backtest_engine.py:973`](../../diepi/backtest/engine/backtest_engine.py#L973) — `run()`。

### 10.2 共享资金组合引擎

```python
from diepi.backtest.data import PoolSource
from diepi.backtest.engine import PortfolioEngine
from diepi.backtest.strategy import PortfolioStrategy


class MyPortfolioStrategy(PortfolioStrategy):
    def on_before_market_open(self, ctx):
        return ctx.get_stock_pool()

    def on_day(self, ctx, bars):
        # T 日完整 bar 已知；这里下的单最早 T+1 生效
        pass


engine = PortfolioEngine(
    start_date="20240101",
    end_date="20241231",
    initial_cash=1_000_000,
    freq="daily",
    benchmark="",
    pool_source=PoolSource.SPECIFIED,
    pool_symbols=["000001.SZ", "600000.SH"],
)
result = engine.run(MyPortfolioStrategy())
print(result.summary())
```

`SPECIFIED` 最容易复核。`ALL_MARKET` 会按 `list_date`/`delist_date` 进行点时成员过滤；历史 ST 状态缺失会告警。`INDUSTRY` 使用当前行业快照研究历史时强制不可排名。

示例显式关闭了默认的 `000300.SH` legacy 基准字段。该字段只是价格指数的区间收益，
不是经过严格同观察日、总回报口径校验的 `ComparisonBundle`，不应直接拿其
`benchmark_return`/`excess_return` 做严谨的策略排名。

> 源码定位：[`diepi/backtest/engine/portfolio_engine.py:401`](../../diepi/backtest/engine/portfolio_engine.py#L401) — `PortfolioEngine`；[`diepi/backtest/engine/portfolio_engine.py:2596`](../../diepi/backtest/engine/portfolio_engine.py#L2596)（legacy 基准收益路径）；[`diepi/backtest/data/stock_pool.py:23`](../../diepi/backtest/data/stock_pool.py#L23) — `PoolSource`；[`diepi/backtest/data/stock_pool.py:75`](../../diepi/backtest/data/stock_pool.py#L75) — `StockPool.get_pool()`。

### 10.3 独立多标的并行

`ParallelRunner` 给每个证券一份独立初始资金，并在独立进程中运行同一策略。它适合回答“同一个策略分别用于这些证券会怎样”，不能回答“这些证券共享一笔资金如何调仓”。

```python
from pathlib import Path
from diepi.backtest.engine import ParallelRunner
from diepi.backtest.liquidity import AuctionCapSpec, DailyAuctionLiquidityPolicy


def main():
    strategy_code = Path("my_single_strategy.py").read_text(encoding="utf-8")
    runner = ParallelRunner(
        symbols=["000001.SZ", "600000.SH"],
        start_date="20240101",
        end_date="20241231",
        initial_cash=1_000_000,  # 每只证券各自获得该金额
        freq="daily",
        max_workers=2,
        daily_auction_liquidity=DailyAuctionLiquidityPolicy(
            open_cap=AuctionCapSpec.previous_day_ratio(0.1),
        ),
    )
    parallel_result = runner.run(strategy_code)

    if parallel_result.is_rankable:
        print(parallel_result.top_performers)
    else:
        print(parallel_result.ranking_error)


if __name__ == "__main__":
    # Windows 的多进程 spawn 模式必须使用该保护。
    main()
```

`my_single_strategy.py` 由 `compile_strategy(..., strategy_kind="single")` 编译。可以使用
接收单标的 `Context` / `BarData` 的模块级回调，也可以定义唯一的 `Strategy` 子类；
`PortfolioStrategy` 会因契约不匹配而明确拒绝。模块级名单覆盖包括 `on_after_open` 和
`on_before_close` 在内的八个公开生命周期回调。承载 `ParallelRunner` 的启动脚本在
Windows 上必须保留上例的 `__main__` 保护。

只有全部请求证券成功且精确观测日一致时，平均指标和排名才可用。多策略、参数网格和多窗口实验目前需要调用方在框架外组织。

> 源码定位：[`diepi/backtest/cli/runner.py:145`](../../diepi/backtest/cli/runner.py#L145) — `compile_strategy()`；[`diepi/backtest/engine/parallel_runner.py:682`](../../diepi/backtest/engine/parallel_runner.py#L682)（子进程复用编译器）；[`diepi/backtest/engine/parallel_runner.py:727`](../../diepi/backtest/engine/parallel_runner.py#L727) — `ParallelRunner`；[`diepi/backtest/engine/parallel_runner.py:884`](../../diepi/backtest/engine/parallel_runner.py#L884) — `run()`。

## 11. 使用预计算交易清单

不想把信号逻辑放入策略文件时，可以让内置重放策略读取 CSV。

目标权重型：

```csv
date,symbol,target_weight
20240102,000001.SZ,0.30
20240102,600000.SH,0.20
20240103,000001.SZ,0.00
```

动作型：

```csv
date,symbol,action,percent
20240102,000001.SZ,buy,0.30
20240103,000001.SZ,sell,1.00
```

运行：

```bash
diepi run \
  --signals signals.csv \
  --signals-format auto \
  --start 20240101 --end 20241231 \
  --daily-open-previous-day-ratio 0.1
```

未传 `--symbols` 时，CLI 会从清单的 `symbol` 列推导股票池。目标权重型是声明式闭环调仓，
但它不是“当天未出现的标的自动归零”的完整持仓快照，而有三个必须记住的状态：

- `target_weight > 0`：把该标的调到目标权重；
- `target_weight == 0`：显式清仓；
- 当天没有该标的行：没有指令，保留当前持仓，绝不会从缺席推断卖出。

同日同标的重复时最后一行生效；每日目标权重之和大于 1 时装载即拒绝，不会静默归一化。
动作型更自由，但调用者要自行保证动作序列合理。动作型卖出不接受 `amount`，可使用
`shares`、`percent`，或留空表示卖出全部可卖数量。

> 源码定位：[`diepi/cli.py:99`](../../diepi/cli.py#L99)（`--signals` 与格式参数）；[`diepi/cli.py:228`](../../diepi/cli.py#L228)（一次装载、校验与冻结）；[`diepi/backtest/cli/signal_replay_template.py:9`](../../diepi/backtest/cli/signal_replay_template.py#L9)（目标权重三态契约）；[`diepi/backtest/cli/signal_replay_template.py:63`](../../diepi/backtest/cli/signal_replay_template.py#L63)（盘前重放逻辑）。

## 12. 读取结果

### 12.1 先读状态，再读收益

`ResultContract` 有五种状态：

- `SUCCESS`：覆盖完整，具备单结果排名资格；跨结果或参考腿比较仍须证明观察范围一致；
- `PARTIAL`：产生了部分观察值，但范围或假设不完整，不可排名；
- `INVALID`：请求本身没有可评价观察范围，例如整段位于未来；
- `FAILED`：运行或数据契约失败；
- `CANCELED`：用户或上层控制流主动停止，结果不可排名。

`warnings` 说明降级路径，`assumptions` 记录实际生效的撮合、费用、规则、复权和容量假设，`actual_interval` 与 `data_coverage` 说明真正观察到了什么。

```python
contract = result.result_contract

if not contract.is_rankable:
    print(contract.status.value)
    print(contract.reason)
    for warning in contract.warnings:
        print(warning.code, warning.message)
else:
    print(result.total_return)
```

`FAILED` 通常通过异常返回，因而调用者拿不到 `result`。Python API 可以在捕获异常后读取
`engine.last_result_contract`。CLI 在本次运行目录已经创建后发生引擎或数据契约异常时，会写入
`error.log` 后重新抛出错误。参数解析、策略路径校验、输出目录创建等更早阶段的失败，
不保证已有本次运行目录或 `error.log`：

```python
try:
    result = engine.run(MyStrategy())
except Exception:
    failed_contract = engine.last_result_contract
    if failed_contract is not None:
        print(failed_contract.status.value, failed_contract.reason)
    raise
```

> 源码定位：[`diepi/backtest/result_contract.py:91`](../../diepi/backtest/result_contract.py#L91) — `ResultStatus`；[`diepi/backtest/result_contract.py:258`](../../diepi/backtest/result_contract.py#L258) — `ResultContract`；[`diepi/backtest/engine/backtest_engine.py:1095`](../../diepi/backtest/engine/backtest_engine.py#L1095)（失败契约后重新抛出）；[`diepi/backtest/cli/runner.py:601`](../../diepi/backtest/cli/runner.py#L601)（CLI 失败路径）；[`diepi/backtest/cli/runner.py:603`](../../diepi/backtest/cli/runner.py#L603)（写入 `error.log`）；[`diepi/backtest/outcome.py:66`](../../diepi/backtest/outcome.py#L66) — `OutcomeTracker`。

### 12.2 当前核心指标

现金市场结果当前稳定提供：

- 初始和最终资产；
- 总收益率与年化收益率；
- Sharpe Ratio；
- 收盘净值最大回撤；
- 日内低点净值最大回撤；
- 日内高到低回撤；
- 成交笔数、闭合库存轮数和胜率；
- 每日资产、最终持仓、成交与现金审计证据。

年化收益和 Sharpe 默认按每年 `252` 个交易日、年化无风险利率 `0.03` 计算；这两个值都
是研究口径，不是市场真理，可分别用 `--trading-days` 和 `--risk-free-rate` 修改。
没有成交闭环或收益序列不足时，部分指标会是 `None`，而不是人为填成 0。Alpha、Beta、Sortino、Information Ratio 和跟踪误差不是当前统一结果契约的一部分。

> 源码定位：[`diepi/backtest/engine/backtest_engine.py:391`](../../diepi/backtest/engine/backtest_engine.py#L391) — `BacktestResult`；[`diepi/backtest/engine/portfolio_engine.py:104`](../../diepi/backtest/engine/portfolio_engine.py#L104) — `PortfolioResult`；[`diepi/backtest/metrics.py:408`](../../diepi/backtest/metrics.py#L408) — `MetricEngine`；[`diepi/cli.py:282`](../../diepi/cli.py#L282)（年化与无风险利率 CLI 口径）。

### 12.3 GUI 归档

GUI 运行完成后不会擅自写盘；点击“保存”才会把精确类型为 `PortfolioResult` 或
`ParallelResult` 的当前结果原子发布为 `RunArtifact v1`。历史页逐个验证 v1 目录后再展示
状态与排名资格；现有 `ResultStorage` 旧目录仍可只读加载，但固定显示为
`legacy · 未验证`、不可排名。被篡改或无法解析的目录不会伪装成可用历史记录。

CLI 与 GUI 使用相同 `--results-root` 时，CLI 自动发布的 v1 会直接出现在 GUI 历史页。
组合结果可查看资产净值、结果中原生记录的 `drawdown_close_nav`、成交、持仓、收益归因、
结果契约和执行事件日志；双击成交会定位到对应股票与日期，K 线页也提供该股成交明细。
独立并行排行可双击进入保存于工件中的完整 child 结果。GUI 不会从净值重新猜测缺失的回撤，
也不会在没有 journal 时编造订单状态或拒绝原因。

历史结果的 K 线是本地辅助视图，不属于工件内嵌数据。对于显式标的日线运行，CLI/GUI 会在
引擎运行前捕获实际 direct-file 行情来源的相对路径、长度和 SHA-256，运行后再次核对同一
快照，确认期间没有变化后才发布工件。历史页只有在当前 `data_root` 中对应显示价格轨文件
仍与该指纹一致时，才显示 K 线并叠加成交；缺失、改写、价格模式未知或非 direct-file 来源
都会明确禁用 K 线，但不影响已验证结果本身。历史工件不会从已验证日线继续下钻到未记录
指纹的分钟文件；分钟回测也不会借用日线指纹声称完整运行输入已被证明。工件不会记录绝对
`data_root`。

GUI 的删除操作只接受配置结果根下、能通过 v1 或 legacy 严格加载的直接子目录，并拒绝
链接和越界路径。旧目录不会因为 GUI 能打开而获得 manifest、provenance 或新的信任状态。

> 源码定位：[`save_gui_run` / `load_gui_run` — `diepi/backtest/ui/worker.py`](../../diepi/backtest/ui/worker.py)；[`discover_history_records` / `delete_history_record` — `diepi/backtest/ui/widgets/history_dialog.py`](../../diepi/backtest/ui/widgets/history_dialog.py)；[`MainWindow._on_save_result` — `diepi/backtest/ui/main_window.py`](../../diepi/backtest/ui/main_window.py)。

### 12.4 RunArtifact v1 与旧结果迁移

`RunArtifact v1` 是 CLI 与 GUI 新保存结果共用的工件格式，也提供显式 Python API。Python
调用方保存时先把结果放入 `RunOutcome`，并提供实际运行配置；若要声明源数据身份，还应
构造带 `SourceFingerprint` / 数据契约报告的 `RunProvenance`。现金结果可以使用
`RunOutcome.from_result()`，并行与期货结果分别使用 `build_parallel_outcome()` 和
`build_futures_outcome()`：

```python
from diepi.artifacts import ArtifactStore, EngineKind, RunOutcome

outcome = RunOutcome.from_result(
    result,
    engine_kind=EngineKind.CASH_PORTFOLIO,
)
path = ArtifactStore.save(
    outcome,
    "artifacts/research-001",
    config=run_config,
    provenance=run_provenance,
    strategy_source=strategy_source,
)
loaded = ArtifactStore.load(path)

print(loaded.artifact_verified)  # True：目录、manifest、成员和结果语义已校验
print(loaded.is_rankable)        # 还取决于 RunOutcome / ResultContract
```

目标目录已存在时保存会拒绝覆盖。加载会把目录当作封闭集合：manifest 未列出的文件、链接/
重解析点、长度或 SHA-256 不符、schema 或重建结果不一致都会失败。这里的
`artifact_verified=True` 是工件完整性结论，不是数据真实性、授权、恶意代码扫描或跨结果
可比性的证明。省略 provenance 时会明确记录 `data_identity_level=not_recorded`。

旧 `ResultStorage` 目录必须走显式降级入口：

```python
from diepi.artifacts import ArtifactStore, load_legacy_result

legacy = ArtifactStore.load_legacy("old-result")
# 等价：legacy = load_legacy_result("old-result")
assert legacy.artifact_verified is False
assert legacy.is_rankable is False
```

`LoadedLegacyRun` 只提供 `root`、`result`、只读 `config` 和 `strategy_source`。加载器先拒绝
目录中的链接/重解析点，再委托旧格式的严格解析器，但不会补造 manifest、outcome 或
provenance；即使旧结果内嵌 `SUCCESS ResultContract`，信任状态也不会提升。若研究必须进入
新工件链，应从受信任输入重新运行：CLI 会自动发布 v1，GUI 可点击“保存”，Python 调用方
也可显式保存。不要通过重命名、复制或手工补 manifest 来升级旧目录。

`diepi run` 的成功结果会自动通过 `ArtifactStore.save()` 发布；异常运行在目标尚未发布时
也会尽力保存结构化失败结果和 traceback，且永远不可排名。GUI 则只在用户点击“保存”时
调用同一工件 API。CLI 根目录中的 `summary/equity/orders` 是 manifest 覆盖的兼容视图，
规范内容位于 `inputs/`、`tables/`、`evidence/` 和根部四个 JSON 成员。旧目录仍必须走
legacy 入口，不能仅因目录中有结果契约就称为“已验证 RunArtifact”。

GUI 加载已验证的 `FAILED` RunArtifact 时，不会把 traceback 或空结果送入收益展示、比较或
排行。历史详情和失败诊断窗口显示结构化 `RunError`；traceback 只能从已通过 Artifact
长度/SHA-256 验证的角色读取，以只读、可复制文本显示，界面最多展示 256 KiB，超出时明确
标记为已验证前缀。这个诊断入口不依赖现金结果 adapter，因此已知但 GUI 不支持展示结果的
`engine_kind` 也不会导致失败工件加载崩溃。

> 源码定位：[`diepi/artifacts/storage.py`](../../diepi/artifacts/storage.py) — `ArtifactStore.save/load/load_legacy`、`LoadedRun` 与 `LoadedLegacyRun`；[`diepi/artifacts/adapters.py`](../../diepi/artifacts/adapters.py) — 四类结果 adapter；[`diepi/artifacts/provenance.py`](../../diepi/artifacts/provenance.py) — `RunProvenance`；[`diepi/backtest/cli/runner.py`](../../diepi/backtest/cli/runner.py) — CLI 成功/失败发布；[`diepi/backtest/ui/worker.py`](../../diepi/backtest/ui/worker.py) — GUI 保存/加载；[`tests/backtest/test_run_artifacts.py`](../../tests/backtest/test_run_artifacts.py) — 工件完整性与迁移回归。

## 13. 图形界面

安装 GUI 依赖后运行：

```bash
diepi gui \
  --data-root /path/to/market-data \
  --results-root ./diepi_results
```

GUI 可以在“策略代码 / signals CSV / 冻结 combo”中三选一，策略模式还可载入 wheel 内置
MA5/MA20 策略代码；源码/sdist 检测到真实切片时，“载入公开样例”会同时配置数据根、混合
股票+ETF、日期和执行假设，纯 wheel 环境则明确禁用。它能配置 `raw/dual/hfq`、标的、日期、显式日线开收盘容量、自动/固定印花税
和全账户过户费率。结果页展示资产净值、原生回撤、成交、持仓、订单事件、个股成交明细与
K 线，并能把组合结果或独立并行汇总保存成 v1 工件。同一结果根下也能直接验证并打开 CLI
结果；成交行和独立并行排行都支持双击下钻。GUI 是正式支持的 Python/wheel 入口，但不代表
所有高级引擎都已图形化；当前没有可配置的指标库、分钟级全流程工作台或 standalone 桌面
安装器。关键研究结论仍应回到保存的结果契约与工件验证状态。

历史 K 线只有在当前本地 direct-file 行情与工件中的内容指纹一致时才开放；不匹配时仍可
查看工件内的净值、交易、持仓、合同和 journal，但不会用当前行情冒充原回测行情，也不会
把未记录指纹的分钟文件纳入历史证据链。

若 CLI 工件含有当前 GUI 尚不认识的执行参数，历史页仍会验证并只读展示原结果，但明确
禁用“等价重跑”，避免悄悄丢掉新参数。实验性 `index_futures` v1 工件同样只读加载，并使用
专用的期货结果摘要；它不会被强行解释为现金组合，也不提供现金成交/K 线下钻或 GUI 重跑。

signals 模式在启动引擎前只读取并校验一次 CSV，执行时消费冻结的 `SignalReplayInput`；保存
工件使用同一份内存字节，不会再次打开原路径。重新加载后路径指向工件内部的
`inputs/signals.csv`。因此它与 CLI 具有相同的 T 日盘前重放和 T 日开盘意图语义。

组合模式中的“冻结 combo 目录”是正式输入：GUI 会校验其日期和证券 scope，运行时采用
内置因果回放策略，并在运行开始时把三张 CSV、规范 manifest 与实际执行的回放策略源码
冻结到内存上下文；点击保存只发布这次运行绑定的快照，不会重新读取可能已变化的原目录。
重新加载后路径指向工件内部的 `inputs/combo/`，不再依赖原机器目录。

关闭窗口时，程序会先请求后台回测、加载和保存线程停止；仍在运行时会阻止窗口直接销毁。

> 源码定位：[`diepi/backtest/ui/main_window.py:123`](../../diepi/backtest/ui/main_window.py#L123) — `MainWindow`；[`diepi/backtest/ui/main_window.py:413`](../../diepi/backtest/ui/main_window.py#L413)（启动回测）；[`diepi/backtest/ui/main_window.py:616`](../../diepi/backtest/ui/main_window.py#L616)（关闭与线程清理）；[`diepi/backtest/ui/worker.py:498`](../../diepi/backtest/ui/worker.py#L498) — `BacktestWorker`；[`diepi/backtest/ui/worker.py:606`](../../diepi/backtest/ui/worker.py#L606)（signals 冻结与执行适配）。

## 14. 常见问题

### `DATA_ROOT` 指向的目录不存在

检查路径拼写和进程实际继承到的环境变量。框架有意 fail-fast；不会自动换一套数据继续跑。

### 日线开盘单抛出 `AuctionLiquidityUnavailable`

为 OPEN 配置 `--daily-open-cap-yuan` 或 `--daily-open-previous-day-ratio`。CLOSE 同理使用对应的 close 参数。

### 结果有收益，但不可排名

检查 `result_contract.status`、`reason` 和 `warnings`。常见原因包括窗口截断、行业点时分类不可用、股票池成员失败或并行子任务观测日不一致。

### 我的策略函数没有被调用

CLI 的模块级函数名必须位于 `compile_strategy()` 白名单；或者文件中只定义一个 `PortfolioStrategy` 子类。函数和类混合时，模块级函数优先。

### 为什么市价买单被缩量

预算型市价/STOP 买单会按保守价格和完整费用预冻结。若原请求超过可用现金，框架缩到最大合法数量并记录 `auto_resized`；显式股数订单则拒绝。

### 为什么回测和真实成交不同

Bar 回测没有委托簿、排队顺序和真实冲击成本。查看 assumptions 中的价格路径、滑点、流动性帽、竞价容量和涨跌停规则，再做压力测试。

> 排查入口：[`diepi/backtest/liquidity.py:46`](../../diepi/backtest/liquidity.py#L46) — `AuctionLiquidityUnavailable`；[`diepi/backtest/data/contract.py:880`](../../diepi/backtest/data/contract.py#L880) — `DataContractError`；[`diepi/backtest/rulebook.py:236`](../../diepi/backtest/rulebook.py#L236) — `UnsupportedInstrumentError`；[`diepi/backtest/broker/order.py:308`](../../diepi/backtest/broker/order.py#L308) — `Order.reject()`。

## 15. 推荐的研究流程

1. 从少量 `SPECIFIED` 标的和短窗口开始，先确认策略生命周期。
2. 固定数据快照、价格模式、费用、滑点和竞价容量。
3. 检查 `result_contract`，不比较 PARTIAL/INVALID/FAILED/CANCELED 结果。
4. 查看订单、成交、现金审计与回撤口径，不只看总收益率。
5. 扩大股票池前，确认点时成员、历史 ST 和行业快照的限制。
6. 并行排名前，确认所有子任务的精确观测日相同。
7. 用更低流动性帽、更高交易成本和不同执行参数做压力测试。
8. 归档策略源码、参数、数据版本和最终工件；不要仅保存一张收益曲线截图。

下一步可以阅读[核心功能](02-core-features.md)理解这些约束为什么存在，进入
[参考与边界](04-reference-and-boundaries.md)查具体契约，或按
[本地行情数据格式 v1](05-local-market-data-format-v1.md)准备自己的 Parquet。
