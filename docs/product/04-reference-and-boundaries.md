# dieΠ 精准参考与能力边界

> 适用于当前 `0.1.0` 代码树（[`pyproject.toml:7 — project.version`](../../pyproject.toml#L7)），校对日期：2026-08-16。
> 本文是“查精确契约”的参考页，不是功能路线图。源码链接的行号对应本文编写时的工作树；以后若代码移动，请优先按链接中的稳定类名、函数名或常量名搜索。

> 文档导航：[项目首页](../../README.md) · [目录](README.md) · [作者序（可选）](../../README.md#写在项目前为什么做-dieπ) ·
> [核心功能](02-core-features.md) · [用户手册](03-user-guide.md) · [参考与边界](04-reference-and-boundaries.md) ·
> [本地行情数据格式 v1](05-local-market-data-format-v1.md)

## 1. 怎样理解“支持”

| 标记 | 本文中的含义 | 使用决策 |
| --- | --- | --- |
| ✅ 已支持 | 当前有公开入口、明确契约和失败边界；仍以用户提供正确数据为前提 | 可用于正常研究，但必须检查结果契约 |
| 🎯 正式范围 | 普通用户的自备数据、校验、日线现金回测、结果检查与 GUI 闭环 | 首次使用优先选择 |
| 🧰 高级范围 | 有实现，但需要理解分钟数据、并行或底层 API 的额外契约 | 先验证小范围并阅读对应章节 |
| 🧪 实验范围 | 近似模型或接口仍可能变化 | 不与正式日线结果混同承诺 |
| ⚠️ 部分支持 | 可运行，但模型、历史维度、数据频率或组合方式有明确缺口 | 只能在表中写明的前提下解读 |
| 🔌 兼容路径 | 为历史 provider/产物保留的退化路径，不具有完整的严格数据证据 | 先看 warning/assumption，不宜当成同等可比数据 |
| ❌ 不支持 | 规则簿显式拒绝，或当前入口没有实现该语义 | 不应靠更改代码、伪造类型或关闭检查来绕过 |

这些是“能力状态”，与一次运行的 `SUCCESS/PARTIAL/INVALID/FAILED/CANCELED` “结果状态”不是一回事。品种是否可执行由 [`diepi/backtest/rulebook.py:106 — InstrumentRule`](../../diepi/backtest/rulebook.py#L106) 与 [`diepi/backtest/rulebook.py:846 — RuleBook.require_supported`](../../diepi/backtest/rulebook.py#L846) 决定；结果状态及其不变式由 [`diepi/backtest/result_contract.py:91 — ResultStatus`](../../diepi/backtest/result_contract.py#L91) 和 [`diepi/backtest/result_contract.py:335 — ResultContract._validate_status_invariants`](../../diepi/backtest/result_contract.py#L335) 决定。

产品层的 🎯 正式范围是 A 股与 ETF/LOF 日线现金研究，包括 CLI、Python API 与随
Python 包/wheel 安装的 GUI。分钟现金与独立并行是 🧰 高级范围；独立股指期货日线近似
引擎是 🧪 实验范围。这里的分层不否认代码已经实现，而是说明首批公开承诺不同。

## 2. 品种 × 频率 × 引擎矩阵

### 2.1 主矩阵

| 品种 / 频率 | `BacktestEngine`<br>单标的现金账户 | `PortfolioEngine`<br>共享现金组合 | `ParallelRunner`<br>多个独立现金账户 | `FuturesEngine` | 精确边界 / 源码 |
| --- | --- | --- | --- | --- | --- |
| 沪/深 A 股，日线 | ✅ | ✅ | ✅ | ❌ | 现金引擎路由 [`diepi/backtest/rulebook.py:645 — _base_rule`](../../diepi/backtest/rulebook.py#L645)；单标的频率入口见 [`diepi/backtest/engine/backtest_engine.py:639 — BacktestEngine.__init__`](../../diepi/backtest/engine/backtest_engine.py#L639) |
| 北交所 A 股，日线 | ✅ | ✅ | ✅ | ❌ | 只在北交所会话覆盖期内支持；规则为最低 100 股、1 股递增。品种规则见 [`diepi/backtest/rulebook.py:645 — _base_rule`](../../diepi/backtest/rulebook.py#L645)，会话覆盖失败入口见 [`diepi/backtest/session_calendar.py:501 — SessionCalendar.get_rule`](../../diepi/backtest/session_calendar.py#L501) |
| A 股，分钟 | ✅，仅 `minute` | ✅，`minute/1min/5min/15min/30min/60min` | ✅，仅 `minute` | ❌ | 组合频率集见 [`diepi/backtest/engine/portfolio_engine.py:83 — MINUTE_FREQS`](../../diepi/backtest/engine/portfolio_engine.py#L83)；并行任务最终构造单标的引擎，见 [`diepi/backtest/engine/parallel_runner.py:693 — _run_single_backtest`](../../diepi/backtest/engine/parallel_runner.py#L693) |
| ETF / LOF，日线或分钟 | ✅ | ✅ | ✅ | ❌ | 现金引擎支持，价格三位小数；默认结算规则需看代码段/元数据。见 [`diepi/backtest/rulebook.py:645 — _base_rule`](../../diepi/backtest/rulebook.py#L645) 与 [`diepi/backtest/data/cache_manager.py:200 — ParquetReader.read`](../../diepi/backtest/data/cache_manager.py#L200) |
| A 股 + ETF/LOF，同一日线现金账户 | ❌（单标的） | ✅ | ❌（账户独立） | ❌ | 必须使用显式组合 scope；同一 Account 共享现金，但每个 symbol 独立应用数据路由、tick、涨跌停、T+0/T+1 与自动印花税。合成正式入口回归见 [`test_formal_runner_mixes_stock_and_etf_in_one_cash_portfolio`](../../tests/backtest/test_cli_artifact_integration.py) |
| REIT，任意现金频率 | ❌ | ❌ | ❌ | ❌ | 当前规则显式 `supported=False`，不能当 ETF 执行。见 [`diepi/backtest/rulebook.py:645 — _base_rule`](../../diepi/backtest/rulebook.py#L645) |
| 指数，日线 | ❌（不可交易） | ❌（不可交易） | ❌（不可交易） | ❌ | ⚠️ 可作独立“总回报参考指数”比较腿，但不是可成交标的。交易路由见 [`diepi/backtest/rulebook.py:645 — _base_rule`](../../diepi/backtest/rulebook.py#L645)，可比较性见 [`diepi/backtest/comparison/orchestration.py:178 — reference_total_return_excess`](../../diepi/backtest/comparison/orchestration.py#L178) |
| CFFEX 股指期货 `IC/IM/IF/IH`，日线 | ❌ | ❌ | ❌ | ⚠️ | 是独立的近似研究引擎，不是现金引擎的品种开关。产品表见 [`diepi/futures/constants.py:4 — PRODUCT_SPECS`](../../diepi/futures/constants.py#L4)，范围声明见 [`diepi/futures/result.py:22 — ENGINE_SCOPE`](../../diepi/futures/result.py#L22) |
| 期货，分钟 / tick | ❌ | ❌ | ❌ | ❌ | `FuturesEngine` 只消费日线 OHLC 和日频方向信号，见 [`diepi/futures/engine.py:304 — FuturesEngine.run`](../../diepi/futures/engine.py#L304) 与 [`diepi/futures/engine.py:475 — FuturesEngine._execute`](../../diepi/futures/engine.py#L475) |
| 现金 + 期货混合保证金账户 | ❌ | ❌ | ❌ | ❌ | 当前没有跨引擎账户、保证金或统一事件回放协议；现金引擎会拒绝期货路由。见 [`diepi/backtest/rulebook.py:52 — ExecutionEngine`](../../diepi/backtest/rulebook.py#L52) 和 [`diepi/backtest/engine/backtest_engine.py:1160 — BacktestEngine._init_engine`](../../diepi/backtest/engine/backtest_engine.py#L1160) |

这里的 ✅ 只表示该“品种 × 频率 × 引擎”单元格有可用实现，不代表入口在所有策略写法和
工作流上都已成熟。例如 `ParallelRunner` 能执行这些品种，但其策略编译接口和用途仍按
“部分支持”管理，具体限制见下节及[用户手册](03-user-guide.md#103-独立多标的并行)。

### 2.2 引擎间最容易混淆的边界

- `PortfolioEngine` 是多标的共享现金、共享冻结资源账户；动态股票池也在同一账户中。显式 pool
  可混合规则簿支持的 A 股和 ETF/LOF，但 `ALL_MARKET` 是股票主数据池，不会自动把 ETF
  合并进来。入口见 [`diepi/backtest/engine/portfolio_engine.py:401 — PortfolioEngine`](../../diepi/backtest/engine/portfolio_engine.py#L401)，正式 runner 的合成混合回归见 [`test_formal_runner_mixes_stock_and_etf_in_one_cash_portfolio`](../../tests/backtest/test_cli_artifact_integration.py)。
- `ParallelRunner` 是“每个 symbol 一个 `BacktestEngine`”，每个子任务都拿到完整 `initial_cash`；平均收益只是子结果的算术平均，不是资金加权的组合曲线。见 [`diepi/backtest/engine/parallel_runner.py:693 — _run_single_backtest`](../../diepi/backtest/engine/parallel_runner.py#L693) 和 [`diepi/backtest/engine/parallel_runner.py:1033 — ParallelRunner._aggregate_results`](../../diepi/backtest/engine/parallel_runner.py#L1033)。
- `FuturesCombiner` 只能把已校验、日历完全相同的期货腿合并；它不能与现金结果混合。见 [`diepi/futures/combiner.py:145 — FuturesCombiner.combine`](../../diepi/futures/combiner.py#L145)。

## 3. 数据原生格式、目录和必需字段

本节是实现索引。面向数据生产者的精确列名、dtype、单位、文件粒度、因子锚点、
切片和 Agent 适配边界，以[本地行情数据格式 v1](05-local-market-data-format-v1.md)为准。

### 3.1 数据根目录

数据根按“显式参数 > `DATA_ROOT` > 源码工作区父目录 > 当前目录”解析；结果根按
“显式参数 > `DIEPI_RESULTS_DIR` > 源码工作区的 `diepi_results/` > 当前目录的
`diepi_results/`”解析。路径解析本身不创建目录或修改环境变量。默认运行器真正构造数据层时仍对错误的 `DATA_ROOT`
fail-fast；doctor 和 data validate 则需要能够报告坏路径，所以不会因为导入配置模块而
提前退出。时序根目录是 `DATA_ROOT/parquet/timeseries`，元数据根目录是
`DATA_ROOT/parquet/metadata`。见 [`diepi/runtime.py — RuntimePaths`](../../diepi/runtime.py)、
[`diepi/backtest/config.py — _detect_data_root`](../../diepi/backtest/config.py) 与
[`diepi/backtest/data/cache_manager.py — CacheConfig.from_data_root`](../../diepi/backtest/data/cache_manager.py)。

现金市场的原生行情后端是 Parquet。代码原生识别的核心布局如下；实际最小集合依引擎、频率和股票池而异：

```text
DATA_ROOT/
└─ parquet/
   ├─ metadata/
   │  ├─ common/trade_cal.parquet        # 可选完整 local override
   │  ├─ common/industry/mapping.parquet
   │  └─ stock/basic.parquet
   └─ timeseries/
      ├─ daily/{symbol}.parquet
      ├─ daily_raw/{symbol}.parquet
      ├─ minute/{symbol}/{year}.parquet
      ├─ minute_raw/{symbol}/{year}.parquet
      ├─ etf_daily/{symbol}.parquet
      ├─ etf_daily_raw/{symbol}.parquet
      ├─ etf_minute/{symbol}/{year}.parquet
      ├─ etf_minute_raw/{symbol}/{year}.parquet
      ├─ adj_factor/{symbol}.parquet
      └─ etf_adj_factor/{symbol}.parquet
```

完整分类到子目录的映射见 [`diepi/backtest/data/cache_manager.py:31 — CacheConfig`](../../diepi/backtest/data/cache_manager.py#L31) 和 [`diepi/backtest/data/cache_manager.py:61 — CacheConfig.PARQUET_DIR_MAP`](../../diepi/backtest/data/cache_manager.py#L61)。单文件按 `{category}/{symbol}.parquet` 读取，分钟文件按 `{category}/{symbol}/*.parquet` 合并；代码同时尝试 `000001.SZ` 和 `000001_SZ` 文件名。见 [`diepi/backtest/data/cache_manager.py:242 — ParquetReader._read_single_file`](../../diepi/backtest/data/cache_manager.py#L242) 与 [`diepi/backtest/data/cache_manager.py:263 — ParquetReader._read_minute_data`](../../diepi/backtest/data/cache_manager.py#L263)。

独立市场时钟仍是现金引擎硬依赖，但默认实现已经内置：
`cn-a-share-2010-2026-v1` 覆盖 `20100101..20261231`，身份、覆盖期和内容 SHA-256 会进入
校验报告与结果 assumptions。`trade_cal.parquet` 不再是必需文件；存在时它是完整本地
override，不与内置版本拼接，并须满足自然日连续、同日状态一致、`is_open∈{0,1}` 和请求
范围覆盖。内置与 override 选择见 [`diepi/backtest/data/calendar.py`](../../diepi/backtest/data/calendar.py)
和 [`diepi/backtest/data/data_provider.py`](../../diepi/backtest/data/data_provider.py)。
`stock/basic.parquet` 用于证券主数据、上市区间和股票池，`industry/mapping.parquet` 只有在
行业查询/股票池时需要。主数据与行业映射入口见
[`diepi/backtest/data/cache_manager.py`](../../diepi/backtest/data/cache_manager.py)。

ETF 日线还有截面兼容布局 `parquet/section/etf_daily/{date}.parquet` 和 `etf_daily_raw`，但它是专用回退，不是通用行情适配器。见 [`diepi/backtest/data/cache_manager.py:82 — CacheConfig.ETF_CROSS_SECTION_DIR`](../../diepi/backtest/data/cache_manager.py#L82) 和 [`diepi/backtest/data/cache_manager.py:299 — ParquetReader._read_etf_cross_section`](../../diepi/backtest/data/cache_manager.py#L299)。

### 3.2 现金行情字段契约

| 轨道 | 标准时间键 | 必需字段 | `amount` 源单位 | 备注 / 源码 |
| --- | --- | --- | --- | --- |
| 日线 strategy | `trade_date` 或等价的无时区日级 index | `open, high, low, close` | 若存在，千元 | 策略轨默认是后复权 `hfq`。字段规则见 [`diepi/backtest/data/contract.py:1561 — _validate_required_columns`](../../diepi/backtest/data/contract.py#L1561) |
| 日线 execution | 同上 | `open, high, low, close, pre_close, amount` | 千元 | 执行轨默认是 `raw`，对齐后 `amount` 统一为元。适配见 [`diepi/backtest/data/data_provider.py:639 — DataProvider.get_aligned_pair`](../../diepi/backtest/data/data_provider.py#L639) |
| 分钟 strategy | `trade_time` 或等价的无时区分钟 index | `open, high, low, close, pre_close` | 若存在，元 | 旧文件缺 `pre_close` 时只可从同 symbol、同价格轨的日线补充，来源会记入报告。见 [`diepi/backtest/data/data_provider.py:827 — DataProvider._enrich_minute_pre_close`](../../diepi/backtest/data/data_provider.py#L827) |
| 分钟 execution | 同上 | `open, high, low, close, pre_close, amount` | 元 | 分钟内 `pre_close` 必须按交易日恒定。见 [`diepi/backtest/data/contract.py:1478 — _validate_optional_market_columns`](../../diepi/backtest/data/contract.py#L1478) |

两轨还必须满足以下不变式：

- 时间键集合完全相同、唯一且单调递增；验证器不会为了跑通而自动排序、取交集、填充或丢行。见 [`diepi/backtest/data/contract.py:1239 — _normalize_track_index`](../../diepi/backtest/data/contract.py#L1239) 与 [`diepi/backtest/data/contract.py:2233 — validate_and_align_pair`](../../diepi/backtest/data/contract.py#L2233)。
- OHLC 必须是有限正数且满足 high/low 包络关系；`pre_close` 必须是有限正数（明确免检日除外）；`amount` 必须是有限非负数。见 [`diepi/backtest/data/contract.py:1478 — _validate_optional_market_columns`](../../diepi/backtest/data/contract.py#L1478) 和 [`diepi/backtest/data/contract.py:1561 — _validate_required_columns`](../../diepi/backtest/data/contract.py#L1561)。
- 若数据携带 `symbol` 或 `ts_code`，每行必须与请求 symbol 一致。若 strategy/execution 处于不同价格空间，还必须提供完整 `adj_factor` 快照并通过 AFI-1 价格恒等式，不会用 1 代替缺失因子。见 [`diepi/backtest/data/contract.py:1327 — _validate_symbol_columns`](../../diepi/backtest/data/contract.py#L1327) 与 [`diepi/backtest/data/data_provider.py:639 — DataProvider.get_aligned_pair`](../../diepi/backtest/data/data_provider.py#L639)。

默认价格轨是 strategy=`hfq`、execution=`raw`，见 [`diepi/backtest/config.py:91 — PRICE_MODE_STRATEGY`](../../diepi/backtest/config.py#L91) 与 [`diepi/backtest/config.py:93 — PRICE_MODE_EXECUTION`](../../diepi/backtest/config.py#L93)。两轨只有一轨可用、且 provider 没有严格 pair API 时，引擎可走 🔌 兼容路径；结果会增加 `DATA_CONTRACT_COMPATIBILITY_PATH` warning 和 `data.contract_path=legacy_provider_compatibility` assumption。见 [`diepi/backtest/engine/backtest_engine.py:846 — BacktestEngine._mark_data_contract_compatibility`](../../diepi/backtest/engine/backtest_engine.py#L846)。

显式 `raw` 日线的最小单标的文件只需
`trade_date,open,high,low,close,pre_close,amount`；`vol`、复权轨和复权因子可省略。此时
策略与执行都在 raw 空间，引擎不会读取因子或应用因子公司行为覆盖，结果 assumption 为
`corporate_action.adjustment_factor_model=disabled_same_price_space`。这是公开、确定的模型
边界，不是对跨除权范围总回报正确性的承诺。

### 3.3 Dataset manifest、校验与 synthetic demo

可选的 `diepi_dataset.json` 使用逻辑表值而不是 Parquet writer 字节布局计算身份。data
validate 可以核对 manifest、实际选择的 bundled/local 日历身份、证券元数据提示和所请求的严格行情 pair；它不会
下载、修复、排序、取交集或填充数据。通过只证明该 scope 的契约就绪，不证明数据授权、
供应商真实性或经济正确性。见 [`dataset_manifest.py`](../../diepi/backtest/data/dataset_manifest.py)
与 [`validation_service.py`](../../diepi/backtest/data/validation_service.py)。

```bash
diepi data validate --data-root PATH --symbols CODE[,CODE...] --start YYYYMMDD --end YYYYMMDD
```

默认校验 profile 仅支持 `daily`；价格模式是 `dual/hfq/raw`。未传 `--report` 时不写文件；
`--skip-manifest` 不等于跳过 DC-1/AFI-1。校验通过退出 0，契约未通过退出 1，参数或读写错误
退出 2。

`diepi demo [workspace]` 原子生成新目录并拒绝覆盖，默认在校验后执行一次 synthetic 日线
回测；`--generate-only` 只生成和校验。其 dataset kind 固定为 `synthetic_demo`，证券名称
明确为 `SYNTHETIC_DEMO_NOT_REAL`。这些值不是真实、匿名化或抽样行情。

### 3.4 期货数据与信号

默认期货数据目录位于 `PARQUET_ROOT/futures_daily` 和 `PARQUET_ROOT/futures_continuous`。单品种合约文件是 `futures_daily/{product}_contracts.parquet`；映射法可从 `futures_continuous/{product}_continuous.parquet` 读取滚动表。见 [`diepi/futures/engine.py:198 — _default_dir`](../../diepi/futures/engine.py#L198)、[`diepi/futures/contract.py:190 — ContractSelector.__init__`](../../diepi/futures/contract.py#L190) 和 [`diepi/futures/contract.py:129 — _load_roll_schedule`](../../diepi/futures/contract.py#L129)。

| 输入 | 必需内容 | 边界 / 源码 |
| --- | --- | --- |
| 合约日线 | 装载硬需 `ts_code, trade_date`；进入对应执行路径时还需有限正数 `open/high/low/close`；`volume_t1` 选约额外需要 T-1 `vol`。每合约的 `expiry_date` 必须在原表或独立到期表中明确给出 | 合约/日期必须唯一；不会用下载的最后一行推断到期日。见 [`diepi/futures/contract.py:72 — _load_expiry_schedule`](../../diepi/futures/contract.py#L72)、[`diepi/futures/contract.py:180 — ContractSelector`](../../diepi/futures/contract.py#L180)、[`diepi/futures/contract.py:301 — ContractSelector.get_price`](../../diepi/futures/contract.py#L301) 与 [`diepi/futures/contract.py:414 — ContractSelector._select_by_volume_t1`](../../diepi/futures/contract.py#L414) |
| 独立交易日历 | 可读取的日期列 `trade_date/cal_date/date`；若有 `is_open`，要能证明覆盖区间 | 不会从合约行情反推“应有交易日”。见 [`diepi/futures/engine.py:30 — _load_trading_calendar`](../../diepi/futures/engine.py#L30) |
| 滚动映射（`mapping`法） | `trade_date, mapping_ts_code` | 映射引用未知或过期合约时失败。见 [`diepi/futures/contract.py:129 — _load_roll_schedule`](../../diepi/futures/contract.py#L129) |
| 信号 | `trade_date, direction`，方向仅 `LONG/SHORT/FLAT` | `strict` 要求每个交易日一条；稀疏输入只能显式选 `event` 或 `ffill`。见 [`diepi/futures/engine.py:304 — FuturesEngine.run`](../../diepi/futures/engine.py#L304) 和 [`diepi/futures/engine.py:435 — FuturesEngine._validate_signals`](../../diepi/futures/engine.py#L435) |

## 4. 策略生命周期与数据可见性

### 4.1 回调时间线

| 阶段 | 可见数据 | 订单最早生效时点 | 源码 |
| --- | --- | --- | --- |
| `on_init` | `ctx.current_date/current_time` 还是 `None`；通过 Context 读历史数据会明确报错 | 不建议在此下单；用于策略状态初始化 | [`diepi/backtest/strategy/base.py:107 — Strategy.on_init`](../../diepi/backtest/strategy/base.py#L107)；边界检查见 [`diepi/backtest/engine/context.py:570 — Context._get_daily_boundary`](../../diepi/backtest/engine/context.py#L570) |
| `on_before_market_open(T)` | 日线至 T-1；分钟数据至 T-1 全日 | T 的首个合法窗口；`OPEN` 在 T 开盘窗口 | [`diepi/backtest/strategy/base.py:122 — Strategy.on_before_market_open`](../../diepi/backtest/strategy/base.py#L122) 与 [`diepi/backtest/engine/backtest_engine.py:1240 — BacktestEngine._run_day`](../../diepi/backtest/engine/backtest_engine.py#L1240) |
| T 开盘撮合 | 执行轨的 raw open；尚未把全日 high/low/close 暴露给策略 | 消费先前已有效的 `OPEN` 单 | [`diepi/backtest/engine/backtest_engine.py:1632 — BacktestEngine._run_daily_bar`](../../diepi/backtest/engine/backtest_engine.py#L1632) |
| `on_after_open(T)` | 仅 `OpenBarData(symbol, trade_time, open)`；Context 历史日线仍截至 T-1，也不暴露完整 09:30 分钟 bar | 日线模式的 `CLOSE` 可参与 T 收盘；其他类型顺延到下一合法窗口 | [`diepi/backtest/strategy/base.py:18 — OpenBarData`](../../diepi/backtest/strategy/base.py#L18) 与 [`diepi/backtest/strategy/base.py:153 — Strategy.on_after_open`](../../diepi/backtest/strategy/base.py#L153) |
| `on_minute(T, bar)` | `bar` 是刚完成的策略轨 bar；`ctx.get_minute()` 可见至该 bar，不含下一根 | 下一个合法执行窗口，绝不回填当前 bar | [`diepi/backtest/strategy/base.py:134 — Strategy.on_minute`](../../diepi/backtest/strategy/base.py#L134) 与 [`diepi/backtest/engine/context.py:591 — Context._get_minute_boundary`](../../diepi/backtest/engine/context.py#L591) |
| `on_before_close(T)` | 仅已完成的连续交易观测；独立收盘集合竞价 bar 还未可见 | `CLOSE` 可参与紧接的收盘窗口；其他类型顺延 | [`diepi/backtest/strategy/base.py:164 — Strategy.on_before_close`](../../diepi/backtest/strategy/base.py#L164) 与 [`diepi/backtest/engine/backtest_engine.py:1294 — BacktestEngine._run_minute_bars`](../../diepi/backtest/engine/backtest_engine.py#L1294) |
| `on_day(T, bar)` | 完整 T 日策略轨 OHLC | 此回调已在 T 所有撮合后；新单最早 T+1 | [`diepi/backtest/strategy/base.py:173 — Strategy.on_day`](../../diepi/backtest/strategy/base.py#L173) 与 [`diepi/backtest/engine/backtest_engine.py:1632 — BacktestEngine._run_daily_bar`](../../diepi/backtest/engine/backtest_engine.py#L1632) |
| `on_after_market_close(T)` | T 日已完成日线/分钟数据 | 用于统计和保存；盘后新建盘中单会被拒绝 | [`diepi/backtest/strategy/base.py:192 — Strategy.on_after_market_close`](../../diepi/backtest/strategy/base.py#L192) |
| `on_finish` | 最终 Context 状态；未完成订单已先清理 | `on_init` 成功后恰好调用一次；不是交易回调 | [`diepi/backtest/strategy/base.py:203 — Strategy.on_finish`](../../diepi/backtest/strategy/base.py#L203) 和 [`diepi/backtest/engine/backtest_engine.py:973 — BacktestEngine.run`](../../diepi/backtest/engine/backtest_engine.py#L973) |

`PortfolioStrategy` 的同名回调具有相同因果语义，只是 `bar` 变成按 symbol 组织的 `PortfolioBarData/PortfolioOpenBarData`，且 `on_before_market_open` 可返回当日活动股票池。见 [`diepi/backtest/strategy/portfolio_strategy.py:16 — PortfolioOpenBarData`](../../diepi/backtest/strategy/portfolio_strategy.py#L16)、[`diepi/backtest/strategy/portfolio_strategy.py:33 — PortfolioBarData`](../../diepi/backtest/strategy/portfolio_strategy.py#L33) 和 [`diepi/backtest/strategy/portfolio_strategy.py:77 — PortfolioStrategy`](../../diepi/backtest/strategy/portfolio_strategy.py#L77)。

### 4.2 可见性的两条硬规则

1. 回调的 `bar` 与 `ctx.get_daily/get_minute` 读到的是 strategy 轨；账户撮合、涨跌停和费用用 execution 轨。两轨转换由 [`diepi/backtest/engine/price_mode.py:12 — PriceModeMixin`](../../diepi/backtest/engine/price_mode.py#L12) 和 [`diepi/backtest/data/data_provider.py:639 — DataProvider.get_aligned_pair`](../../diepi/backtest/data/data_provider.py#L639) 约束。
2. Context 会对用户给出的 `end_date/end_time` 再做因果截断，不会因为用户显式填了未来日期就放行。单标的边界实现见 [`diepi/backtest/engine/context.py:570 — Context._get_daily_boundary`](../../diepi/backtest/engine/context.py#L570) 和 [`diepi/backtest/engine/context.py:591 — Context._get_minute_boundary`](../../diepi/backtest/engine/context.py#L591)；组合实现见 [`diepi/backtest/engine/portfolio_context.py:783 — PortfolioContext._get_daily_boundary`](../../diepi/backtest/engine/portfolio_context.py#L783) 和 [`diepi/backtest/engine/portfolio_context.py:804 — PortfolioContext._get_minute_boundary`](../../diepi/backtest/engine/portfolio_context.py#L804)。

### 4.3 现金主入口的三种输入

三种方式互斥，CLI 与 GUI 的目标是对同一种输入使用相同的校验、重放、现金引擎和工件
快照语义：

| 输入适配器 | CLI | `date=T` / 执行语义 |
| --- | --- | --- |
| 策略代码 | `diepi run strategy.py` | 由第 4.1 节的生命周期控制；策略在合法回调中直接生成规范意图 |
| 简单 signals CSV | `diepi run --signals signals.csv` | 清单在运行前冻结；T 行于 T 日盘前重放并提交 T 日开盘 target/action |
| 冻结 combo | `diepi run --combo-bundle DIR` | T 日 target 盘前提交；运行前已知的 T 日 `close_sells` 在开盘后调度到 T 日收盘 |

引擎的执行边界是规范化订单、目标权重和定时收盘意图；target/action CSV 只是推荐的内置
适配器，不是任意上游格式的通用解析器。数据库查询、模型输出或个人文件格式需要由用户代码
先转换成这些意图或受支持的 signals/combo。简单 signals 若使用 T 日收盘价或完整 T 日
OHLC 生成，执行日期至少应写成下一交易日，不能把 T 日结果回填成 T 日成交。

## 5. 订单、状态、有效期与成交假设

### 5.1 订单类型

公开类型仅有 `OPEN/CLOSE/MARKET/LIMIT/STOP/STOP_PROFIT`，见 [`diepi/backtest/broker/order.py:25 — OrderType`](../../diepi/backtest/broker/order.py#L25)。

| 类型 | 触发与成交价 | 重要边界 / 源码 |
| --- | --- | --- |
| `OPEN` 卖 | raw `bar.open`，不加滑点 | 开盘卖与开盘买故意不对称；开盘卖的当前契约见 [`diepi/backtest/broker/broker.py:3455 — Broker.execute_open_orders`](../../diepi/backtest/broker/broker.py#L3455) |
| `OPEN` 买 | `auto + open+slip` 默认为 `open×(1+slippage)`；可显式选 `open`；`legacy` 保留旧冻结/定量语义 | 估算默认按涨停价，不按已知开盘价倒推可买量。见 [`diepi/backtest/broker/broker.py:3636 — Broker._execute_open_buy_legacy`](../../diepi/backtest/broker/broker.py#L3636) 和 [`diepi/backtest/broker/broker.py:3690 — Broker._execute_open_buy_auto`](../../diepi/backtest/broker/broker.py#L3690) |
| `MARKET` | 在对应 bar 的撮合点定价：买=`high×(1+slippage)`，卖=`low×(1-slippage)` | 这是最坏 bar 近似，不是真实逐笔市价单。见 [`diepi/backtest/broker/broker.py:2360 — Broker._check_order_trigger`](../../diepi/backtest/broker/broker.py#L2360) |
| `LIMIT` | 买价触及 low/卖价触及 high 即视为可成交；普通情况以限价成交，开盘跳空穿过限价时给更优的 open | 无排队、无委托簿优先级；仍受公共 bar 流动性帽。见 [`diepi/backtest/broker/broker.py:2360 — Broker._check_order_trigger`](../../diepi/backtest/broker/broker.py#L2360) |
| `STOP` | 卖向下穿、买向上穿；普通触发以 stop 价加方向滑点，开盘跳空以 open 加方向滑点 | 触发模型只基于 OHLC 路径，不模拟 stop 成为市价单后的订单簿冲击。见 [`diepi/backtest/broker/broker.py:2360 — Broker._check_order_trigger`](../../diepi/backtest/broker/broker.py#L2360) |
| `STOP_PROFIT` | 当前只是卖向上穿止盈，价格语义同 stop 的方向滑点 | 公开入口是 [`diepi/backtest/broker/broker.py:2118 — Broker.sell_stop_profit`](../../diepi/backtest/broker/broker.py#L2118) |
| `CLOSE` | 买=`close×(1+slippage)`，卖=`close×(1-slippage)` | 收盘两向都加方向滑点；独立集合竞价窗口只执行一次，余量撤销。见 [`diepi/backtest/broker/broker.py:4352 — Broker.execute_close_orders`](../../diepi/backtest/broker/broker.py#L4352) |
| target / rebalance | 记录目标意图，在 `open` 或 `close` 快照上做减仓优先、再对买单共享现金缩放 | 它不是新 `OrderType`；结果会保留 intent/achievement 证据。见 [`diepi/backtest/broker/broker.py:698 — Broker.submit_target_intent`](../../diepi/backtest/broker/broker.py#L698) 和 [`diepi/backtest/broker/broker.py:4167 — Broker._execute_target_close_batch`](../../diepi/backtest/broker/broker.py#L4167) |

撮合前先按品种和生效日校验 raw OHLC 与合法价格带；行情、规则或 stress override 互相
矛盾时整次运行 fail fast，不会把 bar 或成交价静默改写。显式带外 LIMIT 订单拒绝；只有
模型滑点沿不利方向越界时才饱和到边界（BUY 上限、SELL 下限），反方向越界属于结算不变量
错误。涨/跌停锁定仍会对该 bar 做方向性流动性 veto。当前滑点仍嵌入 modeled effective
price，可能不是最小报价单位上的逐笔成交打印；它不应被解释为交易所真实成交记录。

### 5.2 订单状态和有效期

| 状态 | 含义 |
| --- | --- |
| `PENDING` | 对象已创建，尚未提交 |
| `SUBMITTED` | 已提交，等待合法窗口 |
| `PARTIAL` | 已有部分成交，仍有余量 |
| `FILLED` | 全部成交，终态 |
| `REJECTED` | 成交前整单被拒绝；已有成交的订单不能再 retag 为拒绝 |
| `CANCELLED` | 未成交余量被撤销，可以保留已有部分成交 |

状态集和状态转换见 [`diepi/backtest/broker/order.py:41 — OrderStatus`](../../diepi/backtest/broker/order.py#L41)、[`diepi/backtest/broker/order.py:228 — Order.submit`](../../diepi/backtest/broker/order.py#L228)、[`diepi/backtest/broker/order.py:235 — Order.fill`](../../diepi/backtest/broker/order.py#L235)、[`diepi/backtest/broker/order.py:308 — Order.reject`](../../diepi/backtest/broker/order.py#L308) 和 [`diepi/backtest/broker/order.py:318 — Order.cancel`](../../diepi/backtest/broker/order.py#L318)。

当前公开有效期只有 `DAY`。`Order.eligible_from` 决定最早可撮合时点，`expire_date` 标记归属交易日；日终撤销本日未完成余量并释放冻结资源。当前没有 GTC/IOC/FOK。字段见 [`diepi/backtest/broker/order.py:52 — Order`](../../diepi/backtest/broker/order.py#L52)，日终处理见 [`diepi/backtest/broker/broker.py:1805 — Broker.cancel_day_end_orders`](../../diepi/backtest/broker/broker.py#L1805)。

### 5.3 数量、冻结和流动性

- 买入参数同时给出时优先级是 `shares > amount > percent`。连续 `MARKET/STOP` 买单的 `amount/percent` 是预算意图：仅当保守冻结上界超过现金时缩到最大可支付整手，并保留 `requested_* / auto_resized / resize_reason` 审计字段；显式 `shares` 仍是整单拒绝。见 [`diepi/backtest/broker/order.py:52 — Order`](../../diepi/backtest/broker/order.py#L52) 和 [`diepi/backtest/broker/broker.py:2649 — Broker._create_buy_order_inner`](../../diepi/backtest/broker/broker.py#L2649)。
- 买单的最低申报量/递增单位由规则簿与 `lot_size` 共同解析；新委托不能因现金帽缩放成低于 minimum 的申报，但流动性导致的“部分成交”可低于最低申报量。见 [`diepi/backtest/broker/broker.py:2527 — Broker._lot_rule`](../../diepi/backtest/broker/broker.py#L2527) 和 [`diepi/backtest/broker/broker.py:4495 — Broker._execute_order`](../../diepi/backtest/broker/broker.py#L4495)。
- 连续 bar 默认最多消费 `bar.amount × liquidity_cap_ratio`（默认 0.8），同 symbol 多单共享帽；分钟 `amount` 缺失不会猜测流动性。见 [`diepi/backtest/broker/broker.py:1300 — Broker._bar_liquidity_cap`](../../diepi/backtest/broker/broker.py#L1300) 和 [`diepi/backtest/broker/broker.py:1330 — Broker._get_available_amount`](../../diepi/backtest/broker/broker.py#L1330)。
- 日线 OHLCV 不能证明开盘/收盘集合竞价的成交额，因此使用某个日线竞价窗口时，必须为该窗口显式配置固定元上限或“前一交易日全日成交额比例”；缺失时 fail fast。见 [`diepi/backtest/liquidity.py:46 — AuctionLiquidityUnavailable`](../../diepi/backtest/liquidity.py#L46) 和 [`diepi/backtest/liquidity.py:106 — DailyAuctionLiquidityPolicy`](../../diepi/backtest/liquidity.py#L106)。
- 现金市场只能卖已持有、已可用份额，没有现金融资、融券、裸卖空或借券模型。卖单创建入口见 [`diepi/backtest/broker/broker.py:3070 — Broker._create_sell_order_inner`](../../diepi/backtest/broker/broker.py#L3070)。

## 6. 费用、交易规则和复权假设

### 6.1 现金引擎默认值

| 项目 | 默认值 / 语义 | 源码 |
| --- | --- | --- |
| 佣金 | `0.00025`，单父订单累积成交额，最低 `5.00` 元 | [`diepi/backtest/broker/fees.py:132 — FeeSchedule`](../../diepi/backtest/broker/fees.py#L132) 和 [`diepi/backtest/broker/fees.py:314 — FeeEngine.calculate_fill`](../../diepi/backtest/broker/fees.py#L314) |
| 印花税 | Python 引擎构造器为兼容旧调用仍默认数值 `0.001`；`diepi run` CLI 默认传 `auto`。固定数值仅卖出收取且对账户一刀切 | [`diepi/backtest/engine/backtest_engine.py:639 — BacktestEngine.__init__`](../../diepi/backtest/engine/backtest_engine.py#L639) 和 [`diepi/cli.py — _configure_run_parser`](../../diepi/cli.py) |
| 印花税 `auto` | CLI 默认：场内基金免征；普通股票在 2023-08-28 前为 0.001，该日起为 0.0005；无法解析时保守回退 0.001 | [`diepi/backtest/broker/account.py:199 — Account.resolve_stamp_rate`](../../diepi/backtest/broker/account.py#L199) |
| 过户费 | 买卖双边按成交额计，默认 `0.0`；当前是全账户单一费率，不做按日期或品种的自动切换 | [`diepi/backtest/broker/fees.py:132 — FeeSchedule`](../../diepi/backtest/broker/fees.py#L132) 和 [`diepi/backtest/broker/fees.py:314 — FeeEngine.calculate_fill`](../../diepi/backtest/broker/fees.py#L314) |
| 金额舍入 | 每个费用分项以分为单位 `ROUND_HALF_UP` | [`diepi/backtest/broker/fees.py:76 — _round_money`](../../diepi/backtest/broker/fees.py#L76) |
| 滑点 | 现金默认比例 `0.001`；按第 5.1 节的窗口/方向嵌入成交价 | [`diepi/backtest/broker/broker.py:158 — Broker`](../../diepi/backtest/broker/broker.py#L158) 和 [`diepi/backtest/engine/backtest_engine.py:174 — _add_execution_model_assumptions`](../../diepi/backtest/engine/backtest_engine.py#L174) |

每次运行的实效费率、滑点、竞价模式、流动性帽、价格带快照、会话快照和 T+0 override 都会写入 `ResultContract.assumptions`，不应只靠外部配置文件猜测。统一入口见 [`diepi/backtest/engine/backtest_engine.py:174 — _add_execution_model_assumptions`](../../diepi/backtest/engine/backtest_engine.py#L174)。

`diepi run` 的股票与 ETF/LOF 混合账户应保持默认 `stamp_duty=auto`，也可以为审计清晰显式
写出 `--stamp-duty auto`；固定数值会对账户内所有品种一刀切。`transfer_fee_rate` 仍是全账户共享值，因此当前不能自动表达
一个混合账户中随品种和日期变化的不同过户费率。

`ROUND_HALF_UP` 是 diePi 的默认十进制模型，不是全国统一的券商清算断言。旧实现常见的
`round(binary_float, 2)` 会同时混入 ties-to-even 和浮点表示误差；它有时向上、有时向下，
不能称为稳定的“保守截断”。严格迁移应同时冻结舍入模式、分项/合计阶段、父订单/成交
作用域、最低佣金和品种生效日，而不是只换一个 `round` 函数。

### 6.2 品种规则

- 同一 `PortfolioEngine` 可以逐 symbol 应用以下规则并共享现金，但每个 symbol 都必须先通过
  `RuleBook.require_supported(..., CASH)`；混放 REIT、指数、期货或未知代码会失败，不能靠数据
  所在目录改变品种身份。
- 普通 A 股买入默认 100 股起、100 股递增；科创板 `688/689` 为 200 股起、1 股递增；北交所为 100 股起、1 股递增；ETF/LOF 为 100/100。见 [`diepi/backtest/rulebook.py:645 — _base_rule`](../../diepi/backtest/rulebook.py#L645)。
- A 股默认 T+1；沪市 `511/513/518` 开头 ETF 可从代码无歧义推到 T+0；其他 ETF/LOF 保守默认 T+1，除非证券主数据覆盖或显式 `t0_overrides`。Broker 的当日可卖判定直接消费 `InstrumentRule.settlement`。见 [`diepi/backtest/rulebook.py:645 — _base_rule`](../../diepi/backtest/rulebook.py#L645) 和 [`diepi/backtest/broker/broker.py:2514 — Broker._is_t0`](../../diepi/backtest/broker/broker.py#L2514)。
- 股票价格精度默认 2 位，ETF/LOF/REIT 规则记录为 3 位；涨跌停线按生效日规则和整数 tick 舍入。请不要把 `limit_pct_overrides` 理解为“支持未知品种”，它只是已支持品种的压力参数。见 [`diepi/backtest/rulebook.py:982 — LimitBandService`](../../diepi/backtest/rulebook.py#L982) 和 [`diepi/backtest/broker/broker.py:2467 — Broker._get_limit_pct`](../../diepi/backtest/broker/broker.py#L2467)。
- 现金分钟会话是有生效日的静态快照；时间戳不在任一会话窗口内会失败，不会混入相邻 bar。见 [`diepi/backtest/session_calendar.py:425 — SessionCalendar`](../../diepi/backtest/session_calendar.py#L425) 和 [`diepi/backtest/engine/minute_resampler.py:240 — resample_minute_data`](../../diepi/backtest/engine/minute_resampler.py#L240)。

### 6.3 复权因子与公司行为边界

raw 执行轨与 hfq 策略轨不同时，引擎会在交易日开始使用已校验调整因子比率，把它解释为“即时总回报再投资，零碎权益以现金代替”；假设不收股息税。因子比率与 1 的差异小于 `1e-5` 时视为非实质漂移，不调整仓位。raw/raw 与 hfq/hfq 单价格空间不启用该覆盖。见 [`diepi/backtest/engine/price_mode.py:9 — ADJUSTMENT_FACTOR_MATERIALITY`](../../diepi/backtest/engine/price_mode.py#L9)、[`diepi/backtest/engine/price_mode.py:12 — PriceModeMixin`](../../diepi/backtest/engine/price_mode.py#L12) 和 [`diepi/backtest/broker/broker.py:1195 — Broker.apply_adjustment_factor_total_return`](../../diepi/backtest/broker/broker.py#L1195)。

这不是逐项公司行为引擎：它没有独立股息、税收批次、配股选择权、停牌公告或权益登记日模型。有这些需求时，必须把当前假设写入研究限制；结果假设由 [`diepi/backtest/engine/backtest_engine.py:174 — _add_execution_model_assumptions`](../../diepi/backtest/engine/backtest_engine.py#L174) 固化。

## 7. 结果状态、warning 和可排名性

### 7.1 结果状态

| 状态 | 必须满足的契约 | 是否可排名 |
| --- | --- | --- |
| `SUCCESS` | 有至少一个观测；实际观测 ID 与预期 ID 集合完全相同；coverage=1.0；无终态 reason | ✅，对单个 `ResultContract` 而言 |
| `PARTIAL` | 有正数个观测，但覆盖不完整，或引擎为一个明确研究偏差强制降级；必须有 reason | ❌ |
| `INVALID` | 没有可声称的实际区间；常见于无预期交易日、无有效行情或空股票池 | ❌ |
| `FAILED` | 声明了运行范围后出现引擎异常；必须有 reason，可保留已观测进度 | ❌ |
| `CANCELED` | 外部主动取消的终态表示；必须有 reason | ❌ |

不变式见 [`diepi/backtest/result_contract.py:258 — ResultContract`](../../diepi/backtest/result_contract.py#L258) 和 [`diepi/backtest/result_contract.py:335 — ResultContract._validate_status_invariants`](../../diepi/backtest/result_contract.py#L335)；从实际观测冻结成上述状态的逻辑见 [`diepi/backtest/outcome.py:288 — OutcomeTracker.finalize_completed`](../../diepi/backtest/outcome.py#L288)。

### 7.2 warning 不是状态，assumption 不是备注

- warning 是非终态的机器可读诊断；同一 code 不能重复。assumption 是模型语义的不可变字符串快照；同一 key 不能冲突。见 [`diepi/backtest/result_contract.py:146 — ResultWarning`](../../diepi/backtest/result_contract.py#L146) 和 [`diepi/backtest/result_contract.py:161 — ResultAssumption`](../../diepi/backtest/result_contract.py#L161)。
- `WINDOW_TRUNCATED` 表示请求结束日超过已完成收盘/本地数据快照。单结果可能对“裁剪后的实际范围”仍是 `SUCCESS`，但不能装作覆盖了原请求窗口。警告添加处见 [`diepi/backtest/engine/backtest_engine.py:758 — BacktestEngine._new_outcome_tracker`](../../diepi/backtest/engine/backtest_engine.py#L758)。
- `DATA_CONTRACT_COMPATIBILITY_PATH` 表示没有严格 DC-1 pair 证据。`UNIVERSE_ST_HISTORY_UNAVAILABLE` 表示全市场/行业池没有历史 ST 状态；`UNIVERSE_INDUSTRY_SNAPSHOT_BIAS` 表示行业成分是当前快照，该情况会把组合结果强制降为 `PARTIAL`。见 [`diepi/backtest/engine/portfolio_engine.py:610 — PortfolioEngine._add_universe_contract_evidence`](../../diepi/backtest/engine/portfolio_engine.py#L610) 和 [`diepi/backtest/engine/portfolio_engine.py:667 — PortfolioEngine._finalize_completed_outcome`](../../diepi/backtest/engine/portfolio_engine.py#L667)。

### 7.3 什么时候才能比较或排名

1. 单标的/组合结果的最小条件是 `result.result_contract.is_rankable is True`，而该属性仅在状态为 `SUCCESS` 时为真。见 [`diepi/backtest/result_contract.py:370 — ResultContract.is_rankable`](../../diepi/backtest/result_contract.py#L370)、[`diepi/backtest/engine/backtest_engine.py:562 — BacktestResult.is_rankable`](../../diepi/backtest/engine/backtest_engine.py#L562) 和 [`diepi/backtest/engine/portfolio_engine.py:296 — PortfolioResult.is_rankable`](../../diepi/backtest/engine/portfolio_engine.py#L296)。
2. `SUCCESS` 不会把 `sharpe_ratio=None` 或 `win_rate=None` 伪造成 0。无足够收益样本/零波动时 Sharpe 可为 `None`；没有已平的库存往返时胜率可为 `None`。字段定义见 [`diepi/backtest/engine/backtest_engine.py:391 — BacktestResult`](../../diepi/backtest/engine/backtest_engine.py#L391)。
3. “策略总回报 - 参考指数总回报”只在策略 contract 为 `SUCCESS`、参考腿为 `SUCCESS`、实际观测日历/范围完全相同时返回数值；否则返回 `None`。见 [`diepi/backtest/comparison/orchestration.py:178 — reference_total_return_excess`](../../diepi/backtest/comparison/orchestration.py#L178)。
4. 当前现金结果还校验 target intent/achievement 完整性，并以执行事件日志做现金回放与终值核对；严格现行结果不应只依赖一张成交 CSV。见 [`diepi/backtest/engine/backtest_engine.py:427 — BacktestResult._validate_target_execution`](../../diepi/backtest/engine/backtest_engine.py#L427)、[`diepi/backtest/engine/backtest_engine.py:444 — BacktestResult._validate_cash_audit`](../../diepi/backtest/engine/backtest_engine.py#L444) 和 [`diepi/backtest/broker/replay.py:2121 — CashAuditBundle`](../../diepi/backtest/broker/replay.py#L2121)。

GUI 的 v1 保存边界要求对象类型精确为 `PortfolioResult` 或 `ParallelResult`；对应 adapter 会在
写盘前重新校验结果语义和已有 evidence。GUI 历史加载对 v1 调用 `ArtifactStore.load()`，
对旧目录调用 `ArtifactStore.load_legacy()`，不会把缺失的契约/审计 evidence 猜成现行证据。
见 [`diepi/backtest/ui/worker.py — save_gui_run/load_gui_run`](../../diepi/backtest/ui/worker.py) 和
[`diepi/backtest/ui/widgets/history_dialog.py`](../../diepi/backtest/ui/widgets/history_dialog.py)。

### 7.4 RunArtifact 与 legacy 信任边界

| 目录类型 | 入口 | 加载后的信任状态 | 精确边界 |
| --- | --- | --- | --- |
| `RunArtifact v1` | `ArtifactStore.load()` / `load_run_artifact()` | 完整验证通过后 `artifact_verified=True`；`is_rankable` 还要求 `RunOutcome` 可排名 | manifest、列出成员的长度/SHA-256、schema、adapter 语义全部核验；拒绝链接、重解析点和未列出成员 |
| `ResultStorage` 旧格式 | `ArtifactStore.load_legacy()` / `load_legacy_result()` | 固定 `artifact_verified=False`、`is_rankable=False` | 只读返回 `root/result/config/strategy_source`；拒绝链接/重解析点，但没有 manifest/outcome/provenance，也不补造证据 |
| 当前 CLI 结果目录 | `ArtifactStore.load()`；同一结果根也可由 GUI 历史页打开；`summary/equity/orders` 仅作兼容读取 | `diepi run` 发布前完成 v1 自校验；重新消费仍应再次加载验证 | 规范成员是根部 manifest/config/provenance/result、`inputs/`、`tables/`、`evidence/`；兼容成员也列入 manifest |
| 当前 GUI 保存目录 | GUI 历史页或 `ArtifactStore.load()` | 点击保存后发布 v1；GUI 未保存的内存结果没有目录级状态 | 支持组合与独立并行结果；历史页把旧格式明确显示为 legacy 未验证；parallel 排行可下钻 child |

`RunArtifact v1` 保存使用同级暂存目录、自校验后发布且不覆盖已有目标。当前四种
`EngineKind`（cash single、cash portfolio、cash parallel、index futures）均有显式 adapter。
v1 的资源边界固定为：`manifest.json` 不超过 4 MiB、payload 不超过 16,384 个、单个
payload 不超过 128 MiB、全部 payload 的声明/实际字节合计不超过 512 MiB（二进制 MiB）。
保存端不会发布越界工件；加载端在读取任何 payload 内容前，先校验 manifest 中的成员数、
单成员声明长度、声明总长度，以及全部列出成员的实际文件长度和实际总长度。成员读取本身
仍使用同一硬上限并复查文件状态，因此稀疏大文件、巨额 `byte_length`、成员数量炸弹和检查
后增长都 fail closed。该边界同时适用于 GUI 历史页，不因 engine kind 或调用入口而放宽。
`artifact_verified` 只证明目录满足 manifest 和 schema，不是签名、恶意内容扫描、数据授权
或跨运行可比性证明。provenance 没有 source 但有数据契约报告时是
`contract_reports_only`；两者都没有才是 `not_recorded`。

显式标的的日线运行中，direct-file 日线来源可额外进入 provenance：运行前后必须得到完全相同的相对
路径、长度和 SHA-256，才允许发布该来源证据。GUI 读取历史结果时只在当前本地显示价格轨
仍匹配该指纹的情况下开放 K 线与成交叠加；它不会把 `data_root` 绝对路径写入工件，也不会
把重新加载的当前行情提升为工件内证据。非 direct-file fallback 或缺少指纹时只禁用 K 线，
不改变结果工件自身的验证状态。历史工件的日线验证不会外推为分钟数据验证；未记录分钟
来源时，GUI 不提供历史分钟下钻。

`LoadedLegacyRun` 的不信任状态不能被旧目录内嵌的 `SUCCESS ResultContract` 提升。旧格式若
需要进入新工件链，应从可信输入重新运行（CLI 会自动发布 v1，GUI 可点击保存），或由可信
Python 调用方显式保存，而不是重命名、复制或手工补一个 manifest。见
[`diepi/artifacts/storage.py`](../../diepi/artifacts/storage.py) 的
`ArtifactStore` / `LoadedLegacyRun`，以及 [`diepi/artifacts/adapters.py`](../../diepi/artifacts/adapters.py)
的 engine adapter。

## 8. 并行协议边界

| 边界 | 当前契约 | 源码 |
| --- | --- | --- |
| 工作单元 | 每个 symbol 在子进程中独立编译策略、创建 `BacktestEngine`、使用一份完整 `initial_cash` | [`diepi/backtest/engine/parallel_runner.py:693 — _run_single_backtest`](../../diepi/backtest/engine/parallel_runner.py#L693) |
| 进程边界 | 子进程只发回 closed-world、versioned 的原始数值 payload；父进程重新检查精确 key、版本、有限数、result contract、target evidence、cash audit 和观测 ID | [`diepi/backtest/engine/parallel_runner.py:81 — _validate_wire_root`](../../diepi/backtest/engine/parallel_runner.py#L81) 和 [`diepi/backtest/engine/parallel_runner.py:186 — _serialize_backtest_result_wire`](../../diepi/backtest/engine/parallel_runner.py#L186) |
| 可排名子结果 | 只接受严格恢复且自身可排名的 `SUCCESS`；子结果请求窗口、初始现金或观测范围不匹配即记为 error | [`diepi/backtest/engine/parallel_runner.py:422 — _restore_backtest_result`](../../diepi/backtest/engine/parallel_runner.py#L422) 和 [`diepi/backtest/engine/parallel_runner.py:1033 — ParallelRunner._aggregate_results`](../../diepi/backtest/engine/parallel_runner.py#L1033) |
| 汇总可排名性 | 所有请求 symbol 都成功、无 error、无 `WINDOW_TRUNCATED`、每个子结果的有序观测日集完全一致，才生成平均和 Top/Worst | [`diepi/backtest/engine/parallel_runner.py:517 — ParallelResult`](../../diepi/backtest/engine/parallel_runner.py#L517) 和 [`diepi/backtest/engine/parallel_runner.py:1033 — ParallelRunner._aggregate_results`](../../diepi/backtest/engine/parallel_runner.py#L1033) |
| 比较腿 | wire v2 保留观测日 ID，但不保留子结果的日 NAV/参考序列，所以并行汇总 `comparisons=None`，参考超额不可生成 | [`diepi/backtest/engine/parallel_runner.py:186 — _serialize_backtest_result_wire`](../../diepi/backtest/engine/parallel_runner.py#L186) 和 [`diepi/backtest/engine/parallel_runner.py:517 — ParallelResult`](../../diepi/backtest/engine/parallel_runner.py#L517) |
| 停止 | 未收集到终态的 symbol 记入 `errors`；不会用默认 0% 结果填位 | [`diepi/backtest/engine/parallel_runner.py:884 — ParallelRunner.run`](../../diepi/backtest/engine/parallel_runner.py#L884) |

并行策略可以写成模块级函数，或唯一的 `Strategy` 子类；回调必须按单标的
`Context`/`BarData` 使用。并行运行器复用 `compile_strategy(..., strategy_kind="single")`，
`PortfolioStrategy` 会在编译阶段因契约不匹配而拒绝。见 [`diepi/backtest/engine/parallel_runner.py:682 — _compile_strategy_in_subprocess`](../../diepi/backtest/engine/parallel_runner.py#L682)、
[`diepi/backtest/engine/parallel_runner.py:693 — _run_single_backtest`](../../diepi/backtest/engine/parallel_runner.py#L693) 和
[`diepi/backtest/cli/runner.py — compile_strategy`](../../diepi/backtest/cli/runner.py)。

并行是计算隔离，不是安全沙箱。策略字符串最终由 Python `exec` 执行，所以只能运行信任的本地策略代码；不要把 `ProcessPoolExecutor` 当成文件系统、网络或系统调用隔离。见 [`diepi/backtest/engine/parallel_runner.py:682 — _compile_strategy_in_subprocess`](../../diepi/backtest/engine/parallel_runner.py#L682) 和 [`diepi/backtest/cli/runner.py:145 — compile_strategy`](../../diepi/backtest/cli/runner.py#L145)。

## 9. 期货边界

`FuturesEngine` 自己把范围命名为 `approximate_index_futures_research`：PnL 是价差×合约乘数×手数，不是交易所逐日盯市账户。该声明见 [`diepi/futures/engine.py:210 — FuturesEngine`](../../diepi/futures/engine.py#L210) 和 [`diepi/futures/result.py:22 — ENGINE_SCOPE`](../../diepi/futures/result.py#L22)。

| 主题 | 已实现 | 明确未实现 / 边界 | 源码 |
| --- | --- | --- | --- |
| 产品 | `IC/IM/IF/IH`，静态乘数和保证金率 | 非 CFFEX 品种、历史可变乘数/保证金表 | [`diepi/futures/constants.py:4 — PRODUCT_SPECS`](../../diepi/futures/constants.py#L4) |
| 合约选择 | `volume_t1` 用 T-1 成交量排名；合约数据日历的首日无 T-1 时只能用已提供的 mapping 回退，否则无可选合约。`mapping` 法始终用显式映射；到期日必须来自明确日程 | 不用 T 日成交量、不把下载末日当到期日、不在已选合约缺 T 日数据时悄悄改选第二名 | [`diepi/futures/contract.py:378 — ContractSelector.select`](../../diepi/futures/contract.py#L378) 和 [`diepi/futures/contract.py:414 — ContractSelector._select_by_volume_t1`](../../diepi/futures/contract.py#L414) |
| 入退场 | 方向变化在日开盘执行；滚动在当日收盘先平旧、再开新；回测末日强制平仓 | 无分钟执行、无集合竞价容量、无订单簿 | [`diepi/futures/engine.py:475 — FuturesEngine._execute`](../../diepi/futures/engine.py#L475) |
| 成本 | 默认每边名义金额佣金 `0.000023`，每边对称 `0.2` 指数点滑点 | 无开平今/平昨差异，无品种历史费率表 | [`diepi/futures/cost.py:9 — CostModel`](../../diepi/futures/cost.py#L9) |
| 保证金 | 每日收盘用 close 做一次维持保证金检查；不足时在该 close 平仓 | 不使用交易所 settle，无盘中追保/强平，无逐日盯市现金结算 | [`diepi/futures/result.py:23 — DEFAULT_ASSUMPTIONS`](../../diepi/futures/result.py#L23) 和 [`diepi/futures/engine.py:469 — FuturesEngine._check_margin`](../../diepi/futures/engine.py#L469) |
| 风险低点 | 多头以日 low、空头以日 high 构造保守 `nav_worst`，用于最大回撤 | 它是日内压力标记，不会触发盘中强平 | [`diepi/futures/engine.py:475 — FuturesEngine._execute`](../../diepi/futures/engine.py#L475) 和 [`diepi/futures/engine.py:936 — FuturesEngine._build_result`](../../diepi/futures/engine.py#L936) |
| 事件日志 | 记录收盘滚动和收盘保证金检查，且同一 close 的顺序是 roll 后 margin | 是诊断日志，不是完整的 order/fill/NAV 回放协议 | [`diepi/futures/engine.py:360 — FuturesEngine._result_assumptions`](../../diepi/futures/engine.py#L360) 和 [`diepi/futures/journal.py:1 — module contract`](../../diepi/futures/journal.py#L1) |
| 结果 | 结果自校验后再带 `ResultContract`；验证状态与运行结果状态分开 | `validation_state=VALID` 不能替代 `result_contract.status=SUCCESS` | [`diepi/futures/result.py:76 — FuturesResult`](../../diepi/futures/result.py#L76) 和 [`diepi/futures/result.py:122 — FuturesResult.validate`](../../diepi/futures/result.py#L122) |

## 10. 明确不支持或仅部分支持的事项

### 10.1 ⚠️ 部分支持

- **全市场股票池**：上市/退市区间可按时点筛选，但历史 ST 状态不可用；不能用当前名称伪造历史 ST 过滤。见 [`diepi/backtest/engine/portfolio_engine.py:610 — PortfolioEngine._add_universe_contract_evidence`](../../diepi/backtest/engine/portfolio_engine.py#L610)。
- **行业股票池**：行业成分只是当前快照与时点上市区间的交集；因为历史成分缺失，完成运行也强制 `PARTIAL`。见 [`diepi/backtest/engine/portfolio_engine.py:667 — PortfolioEngine._finalize_completed_outcome`](../../diepi/backtest/engine/portfolio_engine.py#L667)。
- **双轨兼容**：严格 provider 会 fail fast；只有旧 provider 缺 pair API 时才可走兼容路径，并必须保留 warning。见 [`diepi/backtest/engine/backtest_engine.py:846 — BacktestEngine._mark_data_contract_compatibility`](../../diepi/backtest/engine/backtest_engine.py#L846)。
- **日线竞价**：可执行，但使用者必须提供因果安全的容量假设；引擎不猜当日竞价成交额。见 [`diepi/backtest/liquidity.py:106 — DailyAuctionLiquidityPolicy`](../../diepi/backtest/liquidity.py#L106)。
- **复权公司行为**：是因子总回报再投资假设，不是公告/税批次引擎。见 [`diepi/backtest/engine/price_mode.py:12 — PriceModeMixin`](../../diepi/backtest/engine/price_mode.py#L12)。
- **股指期货**：仅日线、独立、近似研究账户；详见第 9 节和 [`diepi/futures/result.py:23 — DEFAULT_ASSUMPTIONS`](../../diepi/futures/result.py#L23)。
- **版本标识**：发行元数据、Python 包、CLI 归档和 GUI 当前统一显示 `0.1.0`。版本号仍
  不能单独证明源码与数据快照，严谨复现还应记录首次公开后的 Git commit 或安装工件 hash。见
  [`pyproject.toml:7 — project.version`](../../pyproject.toml#L7)、
  [`diepi/__init__.py:3 — __version__`](../../diepi/__init__.py#L3)、
  [`diepi/backtest/cli/runner.py:495 — diepi_version`](../../diepi/backtest/cli/runner.py#L495) 和
  [`diepi/backtest/ui/screens/welcome_screen.py:104 — version_label`](../../diepi/backtest/ui/screens/welcome_screen.py#L104)。

### 10.2 ❌ 不支持

- REIT、指数、未知证券的现金执行，以及把期货 symbol 塞进现金引擎。见 [`diepi/backtest/rulebook.py:846 — RuleBook.require_supported`](../../diepi/backtest/rulebook.py#L846)。
- 期货分钟/tick、期货与现金共享账户、交易所 settle 逐日盯市、盘中追保/强平。见 [`diepi/futures/result.py:23 — DEFAULT_ASSUMPTIONS`](../../diepi/futures/result.py#L23)。
- 现金融资融券、卖空/借券、现金多币种。卖单只使用已有可用份额，见 [`diepi/backtest/broker/broker.py:3070 — Broker._create_sell_order_inner`](../../diepi/backtest/broker/broker.py#L3070)。
- GTC/IOC/FOK、修改原单、委托簿队列位置、逐笔市场冲击。当前类型和 DAY 终止入口见 [`diepi/backtest/broker/order.py:25 — OrderType`](../../diepi/backtest/broker/order.py#L25) 和 [`diepi/backtest/broker/broker.py:1805 — Broker.cancel_day_end_orders`](../../diepi/backtest/broker/broker.py#L1805)。
- 自动下载/修复行情、对任意 CSV/Excel 的通用导入、用另一轨或邻近日静默填数据缺口。严格契约见 [`diepi/backtest/data/contract.py:2233 — validate_and_align_pair`](../../diepi/backtest/data/contract.py#L2233)。
- 不受信任策略代码的安全沙箱。实际编译入口使用 Python `exec`，见 [`diepi/backtest/cli/runner.py:145 — compile_strategy`](../../diepi/backtest/cli/runner.py#L145)。

## 11. 公共命令与写盘边界

| 命令 | 用途 | 默认写盘行为 |
| --- | --- | --- |
| `diepi doctor` | 检查 Python、依赖、GUI 依赖和解析路径 | 无；只读 |
| `diepi data validate` | 校验显式 symbol/date/price-mode scope | 无；只有 `--report` 才写指定 JSON |
| `diepi data extract` | 从用户有权使用的本地 Parquet 生成限定范围私有工作区 | 创建全新 workspace，拒绝覆盖；不复制 signals，默认标记不可再分发 |
| `diepi demo [workspace]` | 生成、校验并默认运行 synthetic demo | 创建全新 workspace，拒绝覆盖；默认回测发布 v1，`--generate-only` 不运行回测 |
| `diepi examples list/copy` | 列出或复制 wheel 内置策略示例 | `list` 只读；`copy` 创建一个新策略文件并拒绝覆盖 |
| `diepi run strategy.py ...` / `--signals` / `--combo-bundle` | 正式现金主入口；combo 支持盘前目标与同日收盘退出 | 在 results root 原子发布 v1；combo 的规范输入一并快照，拒绝同名覆盖 |
| `diepi compare runs baseline candidate` | 对两个现金结果做 run-to-run 账本（含费用/cash delta）与完整指标定义比较 | 默认只读；`--report` 只能写在两个运行目录之外，且默认拒绝覆盖；任一 legacy 时顶层 `UNVERIFIED`，任一不可排名时 `NOT_RANKABLE`，均退出非零 |
| `diepi gui` | 启动正式支持的 PySide6 本地界面 | 运行本身不写盘；点击保存才发布 v1；需要 `gui` 可选依赖 |

`data extract` 的发布原语在 Windows 使用原生不覆盖 rename，在 Linux 使用
`renameat2(RENAME_NOREPLACE)`，在 macOS 使用 `renamex_np(RENAME_EXCL)`；缺少对应原语
的平台会 fail closed。它拒绝源、目标父目录和既有目标中的 symlink/junction/reparse
point，并在读写前后复核文件/目录身份。安全模型仍是本机可信、单写者：同一账号下的其他
进程不得在抽取期间持续替换源树或目标父目录；此能力不是敌对本地进程沙箱。

根命令的退出码约定是 0 成功、1 校验/不可排名、2 使用错误、3 未分类内部错误、130 用户
中断。`data validate` 把契约失败记为 1、输入/读写错误记为 2。GUI 是 Python 包/wheel 的
正式入口，但没有 standalone installer。旧的 `diepi strategy.py ...` 只是 run 简写。

## 12. 异常排查入口

| 现象 | 先看什么 | 稳定入口 |
| --- | --- | --- |
| 运行前报 data root 不存在 | 先运行 `diepi doctor`；检查 `--data-root` 与环境变量，显式错路径不会回退 | [`diepi/runtime.py — RuntimePaths.resolve`](../../diepi/runtime.py) 与 [`diepi/backtest/data/cache_manager.py — CacheManager`](../../diepi/backtest/data/cache_manager.py) |
| `Parquet not found` / 返回空 DataFrame | 核对 symbol 的点/下划线文件名、raw/hfq 子目录和分钟年份文件 | [`diepi/backtest/data/cache_manager.py:200 — ParquetReader.read`](../../diepi/backtest/data/cache_manager.py#L200) |
| `DataContractError` | 查 `error.report.status`、`issue_codes`、每个 issue 的 `track/field/sample_keys`；不要只截取第一条文本 | [`diepi/backtest/data/contract.py:701 — DataQualityReport`](../../diepi/backtest/data/contract.py#L701) 和 [`diepi/backtest/data/contract.py:880 — DataContractError`](../../diepi/backtest/data/contract.py#L880) |
| raw OHLC 与价格带冲突 | 核对品种、生效日、`pre_close`、公司行为因子和 `limit_pct_overrides`；修正数据/规则，不能靠夹价继续 | [`diepi/backtest/broker/broker.py — Broker.validate_execution_bar_price_band`](../../diepi/backtest/broker/broker.py) |
| `UnsupportedInstrumentError` | 读 `error.rule.kind/venue/engine/supported`，确认是元数据分类错还是选错引擎 | [`diepi/backtest/rulebook.py:236 — UnsupportedInstrumentError`](../../diepi/backtest/rulebook.py#L236) |
| 日线开/收盘单报 `AuctionLiquidityUnavailable` | 为实际使用的窗口配置 fixed-yuan 或 previous-day-ratio；前日成交额缺失时 ratio 模式也会失败 | [`diepi/backtest/liquidity.py:46 — AuctionLiquidityUnavailable`](../../diepi/backtest/liquidity.py#L46) 和 [`diepi/backtest/liquidity.py:128 — DailyAuctionLiquidityPolicy.resolve`](../../diepi/backtest/liquidity.py#L128) |
| 会话日期/分钟时间失败 | 分清“交易日历”与“盘中会话快照”；核对品种、venue、日期和时区是否符合覆盖区间 | [`diepi/backtest/session_calendar.py:79 — SessionCalendarError`](../../diepi/backtest/session_calendar.py#L79) 和 [`diepi/backtest/session_calendar.py:523 — SessionCalendar.session_for_timestamp`](../../diepi/backtest/session_calendar.py#L523) |
| 订单整天不成交 | 查 `status/reject_reason/eligible_from/expire_date/frozen_*`，再查价格带 veto、bar `amount`、共享流动性已用额和 T+1 可用份额 | [`diepi/backtest/broker/order.py:325 — Order.to_dict`](../../diepi/backtest/broker/order.py#L325) 和 [`diepi/backtest/broker/broker.py:2408 — Broker._check_limit`](../../diepi/backtest/broker/broker.py#L2408) |
| 结果有数字但不可排名 | 先看 `result_contract.status/reason/warnings/actual_interval/data_coverage`，不要先看总回报 | [`diepi/backtest/result_contract.py:370 — ResultContract.is_rankable`](../../diepi/backtest/result_contract.py#L370) |
| 旧目录能加载但 `artifact_verified=false` | 这是 `load_legacy` 的固定迁移语义；旧 `SUCCESS` 也不能提升目录信任，应从可信输入重新运行，由 CLI 自动发布或在 GUI/Python 中保存 v1 | [`diepi/artifacts/storage.py — LoadedLegacyRun`](../../diepi/artifacts/storage.py) |
| `ArtifactStore.load` 拒绝目录 | 检查是否有未列出文件、链接/重解析点、长度/hash 篡改或 schema/语义不一致；不要手工绕过校验 | [`diepi/artifacts/storage.py — ArtifactStore.load`](../../diepi/artifacts/storage.py) |
| 结果现金、成交次数或费用对不上 | 以 `cash_audit.seed + event_journal` 回放视图为证据；检查 `CashReplayError`，不要手工修结果数字 | [`diepi/backtest/broker/replay.py:48 — CashReplayError`](../../diepi/backtest/broker/replay.py#L48) 和 [`diepi/backtest/broker/replay.py:2121 — CashAuditBundle`](../../diepi/backtest/broker/replay.py#L2121) |
| 并行回测有子项成功但汇总不可排名 | 查 `errors`、`ranking_error`、`ranking_scope`、有序观测 ID hash 以及是否有 `WINDOW_TRUNCATED` | [`diepi/backtest/engine/parallel_runner.py:517 — ParallelResult`](../../diepi/backtest/engine/parallel_runner.py#L517) 和 [`diepi/backtest/engine/parallel_runner.py:1033 — ParallelRunner._aggregate_results`](../../diepi/backtest/engine/parallel_runner.py#L1033) |
| 期货运行前失败 | 依次核对独立交易日历覆盖、合约到期表、滚动表、信号 coverage 和 OHLC/vol | [`diepi/futures/engine.py:304 — FuturesEngine.run`](../../diepi/futures/engine.py#L304) 与 [`diepi/futures/contract.py:180 — ContractSelector`](../../diepi/futures/contract.py#L180) |
| `FuturesValidationError` | 同时看 `FuturesResult.validation_errors/validation_state` 和 `engine.last_result_contract`；载荷自校验与运行终态是两层 | [`diepi/futures/result.py:51 — FuturesValidationError`](../../diepi/futures/result.py#L51) 和 [`diepi/futures/result.py:122 — FuturesResult.validate`](../../diepi/futures/result.py#L122) |

## 13. 研究发布前的最小核对清单

1. 品种由 `RuleBook.require_supported` 通过，且选的引擎与 `InstrumentRule.engine` 一致；见 [`diepi/backtest/rulebook.py:846 — RuleBook.require_supported`](../../diepi/backtest/rulebook.py#L846)。
2. strategy/execution 两轨通过 DC-1，没有未解释的兼容 warning；见 [`diepi/backtest/data/contract.py:2233 — validate_and_align_pair`](../../diepi/backtest/data/contract.py#L2233)。
3. 日线竞价每个实际使用窗口都有明示流动性帽；见 [`diepi/backtest/liquidity.py:106 — DailyAuctionLiquidityPolicy`](../../diepi/backtest/liquidity.py#L106)。
4. 阅读结果里的实效费率、滑点、价格带、会话、复权和数据覆盖 assumptions；见 [`diepi/backtest/engine/backtest_engine.py:174 — _add_execution_model_assumptions`](../../diepi/backtest/engine/backtest_engine.py#L174)。
5. 只在结果契约和对应消费者的更严格范围检查都通过后排名；见 [`diepi/backtest/result_contract.py:370 — ResultContract.is_rankable`](../../diepi/backtest/result_contract.py#L370) 和 [`diepi/backtest/engine/parallel_runner.py:552 — ParallelResult.is_rankable`](../../diepi/backtest/engine/parallel_runner.py#L552)。
6. 对声称为 `RunArtifact v1` 的目录，必须由 `ArtifactStore.load()` 得到
   `artifact_verified=True`；legacy 的固定 false 不能用内嵌 `SUCCESS` 绕过。
7. 将本文第 9、10 节的近似与不支持项写进研究报告，不用“回测成功”代替模型边界披露。
