# dieΠ 核心功能：把回测假设变成可检查的证据

> 适用版本：`0.1.0`（Alpha）。本文只描述当前代码树中已经存在的行为，
> 不把 Roadmap 或架构设想写成现成功能。源码链接后的行号对应本文编写时的工作树；
> 后续代码移动时，请优先搜索链接中给出的稳定符号名。

> 文档导航：[项目首页](../../README.md) · [目录](README.md) · [作者序（可选）](01-author-note.md) ·
> [核心功能](02-core-features.md) · [用户手册](03-user-guide.md) · [参考与边界](04-reference-and-boundaries.md) ·
> [本地行情数据格式 v1](05-local-market-data-format-v1.md)

dieΠ 的差异化不在于“又能算一条净值曲线”，而在于把研究中最容易被忽略的几条
边界放进执行路径：本地数据的来源、策略当时能看到什么、研究价格与真实成交价格如何
对应、订单为何成交或没有成交，以及一份结果是否完整到足以参与比较。

面向首批普通用户的正式范围，是“用户自备本地数据 → 校验 → A 股/ETF/LOF 日线现金
回测 → 检查并保存结果证据”，CLI、Python API 和随 Python 包安装的 GUI 都服务这条闭环。
分钟现金、独立并行、股指期货近似引擎和底层自定义编排属于高级或实验范围；已有实现不
等于它们与日线主路径具有相同的产品承诺。当前没有 standalone 桌面安装器。

当前四个执行入口分工如下：

| 入口 | 当前用途 | 不是它的用途 | 源码定位 |
| --- | --- | --- | --- |
| 单标的现金引擎 | A 股或 ETF/LOF 的日线/1 分钟事件回测 | 多标的共享资金、期货保证金 | [`BacktestEngine` — `diepi/backtest/engine/backtest_engine.py:623`](../../diepi/backtest/engine/backtest_engine.py#L623) |
| 组合现金引擎 | 多标的共享现金与冻结资源；支持日线及 `minute/1min/5min/15min/30min/60min` | 每个标的独立一份本金 | [`PortfolioEngine` — `diepi/backtest/engine/portfolio_engine.py:401`](../../diepi/backtest/engine/portfolio_engine.py#L401) |
| 独立并行运行器 | 把同一策略、同一区间和同一参数分发到多个标的，各自独立回测 | 统一资金组合、参数网格或多策略实验编排 | [`ParallelRunner` — `diepi/backtest/engine/parallel_runner.py:727`](../../diepi/backtest/engine/parallel_runner.py#L727) |
| 股指期货引擎 | `IC/IM/IF/IH` 的独立日线近似研究 | 分钟期货、现金与期货混合账户、交易所级结算还原 | [`FuturesEngine` — `diepi/futures/engine.py:210`](../../diepi/futures/engine.py#L210) |

现金引擎中，一次回测的主处理链如下。期货使用第 8 节说明的独立引擎，不经过这条
现金账户处理链。

```text
本地 Parquet
    ↓  DataProvider：双轨读取 + 数据契约校验
策略可见数据（默认 hfq）
    ↓  Strategy / PortfolioStrategy：因果生命周期
带时间资格的订单
    ↓  Broker + RuleBook + FeeEngine + 流动性帽（raw 撮合）
账户 / 持仓 / 成交 / 审计事件
    ↓
Result + ResultContract ──→ 完整 SUCCESS 才具备基础排名资格
```

## 1. 本地 Parquet 数据：研究数据留在自己的工作区

**用户收益。** 核心行情读取不依赖托管服务，也不会要求把研究数据上传到远端。
数据目录、策略代码与运行结果都可以留在自己的机器和版本管理边界内。

**当前行为。** 内置现金行情以 `DATA_ROOT` 为数据根入口；时序数据和元数据分别位于
`DATA_ROOT/parquet/timeseries` 与 `DATA_ROOT/parquet/metadata`。日线通常按证券单文件
读取，分钟线按证券目录下的年度 Parquet 文件合并；进程内使用按证券键控的 LRU 缓存。
独立交易日历是现金引擎推进日期的硬依赖，而 2010–2026 的默认日历随包内置；框架不会
拿某只证券的行情行推断交易日。本地日历存在时是严格的完整 override。

**默认与边界。** 新命令优先消费显式 `--data-root`，其次才读取 `DATA_ROOT`；源码工作区
仍保留历史目录推导。运行入口在真正读取数据时对缺失目录 fail-fast，诊断和校验入口则把
错误整理成可读报告，而不是在导入模块时中断。查询接口支持按证券和日期范围过滤，但读取通常是先载入一个证券文件或年度
文件，再在内存中筛选，不等价于通用 Parquet predicate pushdown 或流式扫描。

**已知限制。** 当前稳定行情后端是约定式 Parquet。CSV 主要用于信号输入和遗留兼容，
Excel、自定义目录适配器、数据库连接器、自动联网下载与通用分块迭代读取都不是现有的
公开能力。框架也不能阻止用户策略自行发起网络请求，因此“本地”描述的是内置数据层，
不是对任意策略代码的沙箱保证。

内置的 scoped data validation 会核验实际选择的日历身份、所请求标的与日期、raw/HFQ 双轨、复权
因子以及可选 dataset manifest。它不下载、不排序、不取交集、不填充也不修复数据；通过
只证明当前请求范围满足结构和执行契约，不证明数据授权、供应商真实性或经济含义正确。
synthetic demo 使用确定性生成值并带 manifest，只用于验证安装和产品路径。

`diepi data extract` 可以从用户已有的兼容数据湖生成全新的限定范围私有工作区。它保留
前一交易日、双价格轨和复权因子首行锚点，按股票/ETF 路由可选元数据，并使用内置日历在
原子发布前自校验；它不会发现或复制策略信号。输出中的来源 scope 已脱敏并明确标为
用户提供、默认不可再分发。该功能减少本地体积和路径泄露，不授予任何数据再分发权。

源码与测试证据：

- [`RuntimePaths.resolve` — `diepi/runtime.py`](../../diepi/runtime.py)：显式路径、环境变量与兼容默认值的无副作用解析。
- [`validate_local_data` — `diepi/backtest/data/validation_service.py`](../../diepi/backtest/data/validation_service.py)：只读、按范围的数据校验。
- [`extract_local_data` — `diepi/backtest/data/extraction_service.py`](../../diepi/backtest/data/extraction_service.py)：本地最小范围抽取、原子发布与自校验。
- [`generate_synthetic_demo` — `diepi/demo/generator.py`](../../diepi/demo/generator.py)：确定性 synthetic workspace 生成与自校验。
- [`ParquetReader` — `diepi/backtest/data/cache_manager.py:165`](../../diepi/backtest/data/cache_manager.py#L165)：约定式 Parquet 布局和读取实现。
- [`MemoryCache` — `diepi/backtest/data/cache_manager.py:85`](../../diepi/backtest/data/cache_manager.py#L85)：进程内 LRU 缓存。
- [`diepi.backtest.data.calendar`](../../diepi/backtest/data/calendar.py)：内置日历身份与完整 local override 契约。

## 2. 因果时间边界：先完成观测，再允许策略决策

**用户收益。** 策略回调与订单生效时间由引擎控制，减少“读到了完整 T 日数据，却又在
T 日成交”或“刚看完当前分钟，又回填到同一根 bar”这类隐蔽未来函数。

**当前行为。** 现金策略的主要因果约束是：

- `on_before_market_open(T)` 只看见截至 T-1 的历史数据；盘前订单可从 T 的首个合法
  窗口生效。
- 日线 `on_after_open(T)` 只接收受限的当日开盘观测；`CLOSE` 单可以参加 T 日收盘
  窗口，其他普通订单按生命周期顺延。
- `on_day(T)` 在 T 日全部撮合完成后才接收完整 T 日 bar；其中创建的订单最早 T+1
  生效。
- `on_minute(T, bar)` 接收刚完成的 bar；本回调创建的订单最早进入下一有效执行窗口，
  不会回填当前 bar。
- 对存在独立收盘竞价的会话，`on_before_close` 在 14:58 触发，此时可见的
  `current_bar` 仍是最后一根连续交易 bar（14:57）；14:58–15:00 的竞价数据尚未暴露。

策略代码、简单 signals CSV 和冻结 combo 是三个互斥的正式输入适配器，最终都归一为订单、
目标权重或定时收盘意图，再交给同一现金引擎。CSV 不是引擎唯一能理解的上游边界：数据库
查询、模型输出或个人格式应由用户代码先转换成规范意图或受支持的 signals/combo，框架不会
猜测任意表结构。简单 signals 的 `date=T` 表示清单已在运行前冻结，并于 T 日盘前重放、
提交 T 日开盘意图；若信号使用了 T 日收盘或完整 OHLC，执行日期至少应写成下一交易日。

冻结 combo 的内置回放遵循同一边界：盘前读取 T 日目标并提交开盘调仓，预先冻结的
T 日收盘退出则在 `on_after_open(T)` 调度；不会在已完成撮合的 `on_day(T)` 中提交并错误
顺延到 T+1。targets/close_sells/daily 与规范 manifest 会随运行工件完整快照。

分钟时间戳按“分钟结束时刻”解释。内置会话把 09:30 作为独立开盘竞价观测，上午连续
区间为 09:31–11:30，下午首根连续 bar 是 13:01；需要收盘竞价的制度区间将
13:01–14:57 与聚合后的 14:58–15:00 竞价 bar 分开。

**默认与边界。** 会话规则按交易所、品种与生效日期选择，并把来源版本和快照 hash 写入
结果假设。严格重采样会拒绝非交易时段、重复、乱序；输入一旦包含收盘竞价窗口记录，
缺少 15:00 终点也会被拒绝。
现金市场的“哪一天开市”来自独立交易日历，不是 `SessionCalendar` 的职责。默认使用
内置、版本化的 2010–2026 A 股日历；用户提供 `trade_cal.parquet` 时，它是完整本地
override，而不是从单个标的行情反推市场时钟。

**已知限制。** 内置现金会话覆盖从沪深 2006-07-01、北交所 2021-11-15 起保守声明；
覆盖前日期会失败。CLI/GUI 共用的模块级函数式编译路径覆盖八个公开生命周期回调，包括
`on_after_open` 和 `on_before_close`。类策略必须与调用方显式选择的 `portfolio` 或 `single`
契约匹配，不能把组合回调对象交给单标的引擎。

源码与测试证据：

- [`Strategy` 回调契约 — `diepi/backtest/strategy/base.py:107`](../../diepi/backtest/strategy/base.py#L107)：每个回调的数据可见性和最早成交边界。
- [`BacktestEngine._run_minute_bars` — `diepi/backtest/engine/backtest_engine.py:1294`](../../diepi/backtest/engine/backtest_engine.py#L1294)：单标的分钟推进和收盘竞价隔离。
- [`SessionCalendar` — `diepi/backtest/session_calendar.py:425`](../../diepi/backtest/session_calendar.py#L425)：有效期化的现金市场会话选择。
- [`_closing_auction_layout` — `diepi/backtest/session_calendar.py:303`](../../diepi/backtest/session_calendar.py#L303)：14:57 连续交易端点与 14:58–15:00 竞价窗口。
- [`compile_strategy` — `diepi/backtest/cli/runner.py:145`](../../diepi/backtest/cli/runner.py#L145)：模块级函数式策略当前的回调注入范围。
- [`test_single_minute_closing_auction_is_not_exposed_to_callback` — `tests/backtest/test_c0_event_causality_synthetic.py:1284`](../../tests/backtest/test_c0_event_causality_synthetic.py#L1284)：竞价 bar 不提前暴露的回归证据。

## 3. 双价格轨：研究连续性与真实成交约束各归其位

**用户收益。** 指标和信号可以在连续的复权价格上计算，而成交、涨跌停、费用与成交额
容量仍使用真实价格空间。这样既减少除权跳空对研究信号的干扰，也避免用复权价“成交”。

**当前行为。** 默认策略轨是后复权 `hfq`，执行轨是不复权 `raw`。`DataProvider` 会独立
读取两轨，然后要求相同证券、相同频率下的时间键集合完全一致、唯一且已排序；不会静默
取交集、补行或拿另一条轨替代缺失轨。不同价格空间还必须提供完整复权因子，并通过价格
恒等关系验证。日线源 `amount` 明确按千元输入，分钟源按元输入，对齐后的执行数据统一为元。

**默认与边界。** 当前正式价格空间只有 `raw` 和 `hfq`。严格双轨路径的验证报告、复权
因子身份、单位来源与兼容告警会进入结果审计信息。旧 provider 若没有严格 pair API，
引擎可以进入显式标记的兼容路径；这类结果应先检查 warning/assumption，再决定是否比较。

**已知限制。** 前复权 `qfq` 不是正式价格空间。双轨精度取决于用户提供的 raw、hfq 与
复权因子能否构成一致快照。发生因子跳变时，当前持仓处理采用“免税、即时总收益再投资，
零股折现”的研究近似；它不还原现金分红税、配股选择、送转条款或逐项公司行为公告。

源码与测试证据：

- [`PRICE_MODE_STRATEGY` / `PRICE_MODE_EXECUTION` — `diepi/backtest/config.py:91`](../../diepi/backtest/config.py#L91)：默认 `hfq/raw` 价格轨。
- [`DataProvider.get_aligned_pair` — `diepi/backtest/data/data_provider.py:639`](../../diepi/backtest/data/data_provider.py#L639)：成对读取、单位归一与复权因子绑定。
- [`validate_and_align_pair` — `diepi/backtest/data/contract.py:2233`](../../diepi/backtest/data/contract.py#L2233)：严格键一致性和数据契约。
- [`Broker.apply_adjustment_factor_total_return` — `diepi/backtest/broker/broker.py:1195`](../../diepi/backtest/broker/broker.py#L1195)：因子跳变的持仓近似模型。
- [`test_portfolio_minute_strategy_lane_timestamp_mismatch_fails_before_callback` — `tests/backtest/test_c0_event_causality_synthetic.py:1120`](../../tests/backtest/test_c0_event_causality_synthetic.py#L1120)：两轨时间键不一致时回调前失败。

## 4. A 股与 ETF/LOF 规则：默认保守，无法证明时不猜

**用户收益。** 同一份策略不需要自己散落实现交易单位、T+1、涨跌停有效期和集合竞价
时段；有效规则及其快照身份还能随结果保存，便于日后复核。

**当前行为。** 现金规则簿覆盖沪深北 A 股以及当前明确分类为 ETF/LOF 的场内基金，
提供执行引擎路由、价格小数位、买入最低数量/递增单位、结算方式和印花税豁免信息。
涨跌停服务按代码、交易所与日期处理普通 A 股、创业板改革前后、科创板、北交所及已收录
的 20% 场内基金代码，并对合法价格带执行 tick 舍入。新股豁免日历由 `list_date` 和实际
交易日生成；持仓层分别跟踪总持仓、冻结股数和未解锁股数以实施 T+1。

raw 执行 bar 在策略回调或订单撮合前还会与当日有效价格带核对。若 OHLC 证明
`limit_pct_overrides` 或规则选择不可能成立，运行直接失败并报告 symbol/date/band；不会
用双向夹价造出一个看似合法的成交。显式带外 LIMIT 拒绝，只有 modeled slippage 沿交易
方向的不利一侧越界时才饱和到边界。

**默认与边界。** 普通股票默认 T+1；规则簿能明确识别的沪市 `511/513/518` ETF 为
T+0，其余混合代码段保守按 T+1，除非证券主数据或 `t0_overrides` 明确覆盖。普通整手默认
100 股；科创板和北交所使用各自的最低申报量与递增规则。分钟 bar 的默认成交额帽是该 bar
成交额的 80%；日线开盘/收盘竞价没有可观测的独立成交额时，必须通过
`DailyAuctionLiquidityPolicy` 显式给出固定金额或上一日成交额比例，框架不会猜容量。

**已知限制。** 历史 ST/*ST 状态没有权威点时序输入，框架不会自动重建其历史 5% 价格带；
需要研究者提供静态覆盖并披露假设。20% 基金名单是截至 2026-08-07 的显式快照，冻结日后
新发基金可能回退成普通 10% 规则。北交所涨跌停覆盖从 2021-11-15 起，之前不会伪造旧市场
规则。注册制前上市首日特殊带宽目前近似为“首日免涨跌停校验”，并非完整 44%/64% 机制；
恢复上市、重新上市和长期停牌复牌的特殊规则也没有自动推断。

源码与测试证据：

- [`RuleBook` — `diepi/backtest/rulebook.py:792`](../../diepi/backtest/rulebook.py#L792)：品种分类与支持性门禁。
- [`LimitBandService` — `diepi/backtest/rulebook.py:982`](../../diepi/backtest/rulebook.py#L982)：有效期化涨跌停规则及快照边界。
- [`compute_limit_exempt_dates` — `diepi/backtest/engine/listing_rules.py:34`](../../diepi/backtest/engine/listing_rules.py#L34)：基于 `list_date` 的新股豁免日历。
- [`Position.available_shares` / `Position.settle_t1` — `diepi/backtest/broker/position.py:100`](../../diepi/backtest/broker/position.py#L100)：T+1 可卖数量与解锁。
- [`DailyAuctionLiquidityPolicy` — `diepi/backtest/liquidity.py:106`](../../diepi/backtest/liquidity.py#L106)：日线竞价容量必须显式声明。
- [`test_rules_expose_effective_window_and_snapshot_identity` — `tests/backtest/test_limit_rules.py:86`](../../tests/backtest/test_limit_rules.py#L86)：规则有效日期与快照身份回归。

## 5. 订单、资金原子性与事件审计：失败不能留下半笔状态

**用户收益。** 一次成交同时涉及订单状态、现金/股数冻结、持仓成本、费用、bar 容量和
审计事件。dieΠ 把这些更新放在同一回滚边界内；中途任一不变量失败时，不会只改了
现金却没改订单，或留下重复事件 ID。

**当前行为。** 下单时会冻结相应现金或可卖股数；撮合时先校验价格、涨跌停、资金、持仓、
申报单位和剩余 bar 成交额，再计算本次可成交数量。连续撮合订单部分成交后可保留剩余委托
和相应冻结；OPEN/CLOSE 订单只使用各自一次集合竞价窗口，未成交余量随即撤销。其他 DAY
订单在生命周期结束时取消并释放资源。买卖结算使用 `SettlementUnitOfWork` 捕获账户、
持仓、订单、费用状态、bar 已用成交额、计数器和事件日志快照，只有全部后置条件通过才提交。

与财务状态同步，现金引擎产生带模拟时间、阶段序号和日志内连续序号的类型化只追加事件。结果
携带运行前 `CashReplaySeed` 与事件流；构造 `BacktestResult`/`PortfolioResult` 时会严格
回放并核对成交记录、交易次数、胜率、最终 NAV 和初始 NAV，篡改或漏记会拒绝结果。

**默认与边界。** 默认滑点为比例模型，普通市价单采用当前 bar 内方向不利价
（买入 high 加滑点、卖出 low 减滑点）；LIMIT 是触价、无队列模型，STOP 在触发价或跳空
开盘价上执行并应用方向滑点。默认最低佣金按父订单聚合，单 bar 成交额参与率为 80%，
所有这些实际值都会从初始化后的 Broker 写入结果假设，而不是只抄构造参数。

费用分项使用 Decimal `ROUND_HALF_UP` 到分；这是确定的默认建模假设，不是全国统一清算
规则。它比二进制 float 上的 Python `round` 更可复核，但真实账户仍应按券商协议和交割单
校准舍入阶段、最低佣金和拆单作用域。滑点目前仍折入 modeled effective price，因此结果
价格不一定是最小报价单位上的真实逐笔打印。

**已知限制。** 这是 bar 级保守撮合，不是逐笔委托、盘口队列、概率成交或市场冲击模型。
事件日志用于确定性执行审计和现金回放，不是通用复式账本，也不记录策略内部指标计算、
完整行情内容或外部副作用。原子性保证针对 Broker 管理的结算状态；用户策略对外部文件、
数据库或网络做的修改不在回滚范围内。

源码与测试证据：

- [`Broker._execute_order` — `diepi/backtest/broker/broker.py:4495`](../../diepi/backtest/broker/broker.py#L4495)：单次撮合的外层回滚边界。
- [`Broker.execute_open_orders` — `diepi/backtest/broker/broker.py:3455`](../../diepi/backtest/broker/broker.py#L3455) 与 [`Broker.execute_close_orders` — `diepi/backtest/broker/broker.py:4352`](../../diepi/backtest/broker/broker.py#L4352)：集合竞价单窗口与余量撤销。
- [`SettlementUnitOfWork` — `diepi/backtest/broker/settlement.py:170`](../../diepi/backtest/broker/settlement.py#L170)：未显式提交或发生异常时恢复完整快照。
- [`ExecutionEventJournal` — `diepi/backtest/broker/events.py:740`](../../diepi/backtest/broker/events.py#L740)：类型化、确定排序和原子批量追加。
- [`CashAuditBundle` — `diepi/backtest/broker/replay.py:2121`](../../diepi/backtest/broker/replay.py#L2121)：种子与事件流的可回放审计包。
- [`BacktestResult._validate_cash_audit` — `diepi/backtest/engine/backtest_engine.py:444`](../../diepi/backtest/engine/backtest_engine.py#L444)：结果指标与回放状态强制对账。
- [`test_fault_at_any_commit_stage_restores_the_complete_snapshot` — `tests/backtest/test_settlement_atomicity.py:521`](../../tests/backtest/test_settlement_atomicity.py#L521)：任意提交阶段故障的完整回滚回归。

## 6. 结果契约与工件验证：先回答“完整吗”，再回答“收益多少”

**用户收益。** 空数据、窗口被截断、运行失败和用户取消不会被包装成看似正常的零收益结果；
消费端可以先检查统一状态，再决定是否展示、保存、比较或排名。

**当前行为。** 单标的现金、组合现金和期货引擎的当前结果都附带不可变 `ResultContract`；
并行汇总则严格消费各子结果的契约。终态为 `SUCCESS`、
`PARTIAL`、`INVALID`、`FAILED` 或 `CANCELED`，并携带结构化原因、警告、执行假设、实际
观测区间和数据覆盖率。`OutcomeTracker` 以明确的 observation ID 集合记录预期范围与实际
完成范围；只有实际 ID 与预期 ID 完全相等的非空结果才能成为 `SUCCESS`。仅 `SUCCESS`
的契约具有 `is_rankable=True`。

**默认与边界。** 契约记录的假设包括指标参数、撮合口径、费用与流动性参数、规则簿和
会话快照 hash、复权因子证据及现金审计口径。契约有严格的 schema/semantics version，
JSON 反序列化会拒绝未知字段、重复键和不一致的派生值。

`diepi.artifacts` 提供统一的 `RunArtifact v1`；CLI 成功或失败运行会用它发布结果，GUI 的
“保存”操作也会用它归档组合或独立并行结果。Python 调用方还可直接使用
`ArtifactStore.save()`：它在同级暂存目录写入后先做完整自校验，再以不可覆盖的目录发布；
`ArtifactStore.load()` 校验 manifest、
列出的每个成员长度与 SHA-256、拒绝链接/重解析点和未列出的成员，并让对应引擎 adapter
重建和复核结果语义。加载成功返回 `LoadedRun`，其 `artifact_verified=True`；最终
`is_rankable` 还要求其中的 `RunOutcome` 自身可排名。现金单标的、现金组合、独立并行汇总
和股指期货结果均有显式 v1 adapter。

`diepi compare runs` 与 `diepi.backtest.comparison.compare_cash_runs()` 用于两个运行之间的
正式比较。它们拒绝把不同日期 scope 静默取交集，并将起始 cash replay seed、成交事件
顺序、逐日现金/NAV、费用分项、成交现金变动、终态和指标定义分开判定。指标口径只有在年化、波动、
回撤路径、成交计数与胜率单位都完整声明时才可比较。默认 CLI 只接受已验证 v1；显式加载
legacy 参与比较时，顶层固定为 `UNVERIFIED` 且返回非零，即使账本投影自身为 `EXACT`。
已验证工件仍必须两侧结果均可排名，否则顶层为 `NOT_RANKABLE`。直接传入可变的 Python
结果只产生 `UNATTESTED` 诊断；正式认证会重新打开并验证 exact `LoadedRun` 的磁盘工件。

这条工件边界不会把旧格式“猜成”新格式。`ArtifactStore.load_legacy()` 和函数式
`load_legacy_result()` 只是对旧 `ResultStorage` 目录的安全只读包装：返回的
`LoadedLegacyRun` 仅提供 `root/result/config/strategy_source`，固定
`artifact_verified=False`、`is_rankable=False`，也没有可补造的 manifest、outcome 或
provenance。旧目录内即使保存了 `SUCCESS ResultContract`，也不会因此升级为已验证工件。

**已知限制。** `ResultContract` 证明的是“引擎按声明范围完成了哪些观测，并披露了哪些
假设”，不是对用户原始数据经济含义的第三方认证。`artifact_verified` 证明加载目录满足其
manifest 与 schema，不是代码签名、恶意内容扫描或数据授权证明。CLI 会记录引擎公开的
数据契约报告；数据根存在 `diepi_dataset.json` 时还会记录 manifest 身份。对显式标的且
实际使用 direct-file 日线来源的 CLI/GUI 日线运行，还会在引擎前后锁定对应 raw/HFQ/factor 文件
的相对路径、长度和 SHA-256，期间变化即拒绝发布这份结果证据。全市场池、ETF section
fallback 或其它尚无完整来源捕获的路径不会假装拥有该证明；没有 source 但有契约报告时是
`contract_reports_only`，两者都没有才是 `not_recorded`。

`data_identity_level=content_sha256` 只表示 provenance 中列出的 source 都有内容身份，不自动
证明它们构成运行全部输入的封闭集合；调用方仍要结合显式 symbol scope、数据契约报告和
具体 source kind 判断覆盖范围。GUI 运行不会仅因完成就写盘，用户点击“保存”后才发布 v1。
任意使用系统时间、随机数或外部状态的策略不能仅凭结果契约或工件 hash 获得无条件可重复性
保证。

源码与测试证据：

- [`ResultStatus` — `diepi/backtest/result_contract.py:91`](../../diepi/backtest/result_contract.py#L91)：稳定终态集合。
- [`ResultContract` — `diepi/backtest/result_contract.py:258`](../../diepi/backtest/result_contract.py#L258)：不可变结果审计封装与状态不变量。
- [`ResultContract.is_rankable` — `diepi/backtest/result_contract.py:370`](../../diepi/backtest/result_contract.py#L370)：只有 `SUCCESS` 可排名。
- [`OutcomeTracker.finalize` — `diepi/backtest/outcome.py:241`](../../diepi/backtest/outcome.py#L241)：`SUCCESS` 必须匹配完整 observation ID 集合。
- [`test_from_dict_rejects_rankable_derived_field_tampering` — `tests/backtest/test_result_contract_unit.py:720`](../../tests/backtest/test_result_contract_unit.py#L720)：篡改排名派生字段的拒绝测试。
- [`ArtifactStore` / `LoadedRun` / `LoadedLegacyRun` — `diepi/artifacts/storage.py`](../../diepi/artifacts/storage.py)：原子发布、完整加载验证与显式 legacy 降级语义。
- [`adapter_for_kind` — `diepi/artifacts/adapters.py`](../../diepi/artifacts/adapters.py)：四类结果的严格序列化与语义重建。
- [`run_backtest` — `diepi/backtest/cli/runner.py`](../../diepi/backtest/cli/runner.py)：CLI 成功/失败工件发布与兼容视图。
- [`save_gui_run` / `load_gui_run` — `diepi/backtest/ui/worker.py`](../../diepi/backtest/ui/worker.py)：GUI v1 保存、验证加载与 legacy 降级。
- [`test_run_artifacts.py`](../../tests/backtest/test_run_artifacts.py)：round-trip、篡改、不可覆盖和 legacy 未验证回归。
- [`compare_cash_runs` — `diepi/backtest/comparison/run_parity.py`](../../diepi/backtest/comparison/run_parity.py)：run-to-run scope、账本与指标定义比较。

## 7. 组合与独立并行：把“共同资金”和“横截面对比”分开

**用户收益。** 研究者可以明确选择经济含义：用 `PortfolioEngine` 研究多个标的争用同一份
现金与冻结资源，或用 `ParallelRunner` 把同一策略独立应用到多个标的并做横截面对比，
避免把两个问题混成一条貌似合理的收益曲线。

**当前行为。** `PortfolioEngine` 在同一 Broker/Account 中处理多标的，支持盘前动态活动
股票池、共享资金、跨标的先卖后买和目标权重意图。显式 pool 可以在同一账户中混合规则簿
支持的 A 股与 ETF/LOF；行情按 symbol 路由到股票/基金目录，价格 tick、涨跌停、申报单位、
T+0/T+1 与 `stamp_duty=auto` 也逐标的解析。`ParallelRunner` 则为每个 symbol 创建
独立 `BacktestEngine`，每个子任务获得完整 `initial_cash`，然后经严格 wire schema 恢复
结果。并行排名只有在以下条件全部满足时才开放：所有请求标的成功、没有窗口截断、运行
区间/初始资金与父任务一致、每个子结果的有序观测日期集合完全相同。观测日期集合的
SHA-256 会随汇总披露。

**默认与边界。** 并行汇总的平均收益、平均回撤等是独立子账户指标的算术平均，排名也是
子结果排序；它们不是合并持仓或资金加权组合 NAV。若任一标的失败、数据范围不同或带
`WINDOW_TRUNCATED`，`ParallelResult.is_rankable` 为 false，平均指标与排行榜不再作为
可比输出。全市场股票池可按 `list_date/delist_date` 做点时成员过滤，但 `ALL_MARKET` 只从
股票主数据构造，不会自动并入 ETF；混合研究应给出显式 scope。CLI 混合账户应保持默认
`stamp_duty=auto`（或为审计清晰显式写出），因为固定数值会对所有品种一刀切；`transfer_fee_rate` 当前也仍是全账户
共享的单一数值，没有按日期或品种自动切换。

**已知限制。** `ParallelRunner` 不内建多策略调度、参数网格、多股票池、多时间窗口或
实验版本管理，这些需要调用方外部编排。并行 wire 保留观测日期和核心指标，但不保留每个
子任务的完整日 NAV/参考序列，因此聚合 Benchmark comparison 明确为 unavailable。
历史 ST 点时状态不可用；历史行业仅有当前行业映射快照时，结果会披露为不可排名，而不是
把当前行业成员冒充历史成员。

源码与测试证据：

- [`PortfolioEngine` — `diepi/backtest/engine/portfolio_engine.py:401`](../../diepi/backtest/engine/portfolio_engine.py#L401)：共享资金组合语义。
- [`test_formal_runner_mixes_stock_and_etf_in_one_cash_portfolio`](../../tests/backtest/test_cli_artifact_integration.py)：完全合成数据下，同一正式 runner 的股票/ETF 路由、共享现金、价格 tick、T+0/T+1 和自动印花税回归。
- [`ParallelResult.is_rankable` — `diepi/backtest/engine/parallel_runner.py:552`](../../diepi/backtest/engine/parallel_runner.py#L552)：完整标的覆盖和精确日期范围门禁。
- [`ParallelRunner._aggregate_results` — `diepi/backtest/engine/parallel_runner.py:1033`](../../diepi/backtest/engine/parallel_runner.py#L1033)：父任务 envelope、截断警告和有序日期集合校验。
- [`StockPool.get_pool` — `diepi/backtest/data/stock_pool.py:75`](../../diepi/backtest/data/stock_pool.py#L75)：点时股票池与历史 ST 失败边界。
- [`test_success_children_with_different_exact_day_sets_are_not_ranked` — `tests/backtest/test_parallel_runner_result_gate.py:407`](../../tests/backtest/test_parallel_runner_result_gate.py#L407)：首尾和数量相同但内部日期不同也拒绝排名。

## 8. 独立股指期货引擎：把近似模型写在结果里

**用户收益。** 对 `IC/IM/IF/IH` 日线方向策略，框架提供一个不与现金账户混用的真实合约
选择与换月路径。合约到期日和权威交易日历必须显式提供；选择 mapping 换月时，滚动表也
必须显式提供。这样可以避免用被截断的行情文件“证明”自己没有缺交易日，也避免把最后一行
行情误当到期日。

**当前行为。** 输入信号是 `trade_date + LONG/SHORT/FLAT`。默认 `strict` 要求请求窗口
每个交易日都有一条信号；稀疏输入只能显式选择 `event` 或 `ffill`，窗口开始前最后一条
有效信号用于播种目标状态。合约可以按 T-1 成交量选择，也可以使用显式 mapping；到期表
必须来自独立映射或行情中的明确 `expiry_date`。引擎在每日开盘按目标方向交易，在收盘进行
持仓估值、换月和保证金检查，并给结果附上与现金引擎相同的 `ResultContract`。

**默认与边界。** 产品乘数和保证金率来自内置静态产品表；手数固定为构造参数，手续费按
每边成交名义金额比例计算，滑点按指数点数计算。交易日历必须独立提供并覆盖完整请求窗口；
带 `is_open` 的日历还要证明闭市日与区间完整性。滚动 mapping 的变更在映射日收盘生效，
缺价格、未知合约、到期后映射或日历/行情覆盖冲突都会失败，而不自动换一只“次优”合约。

**已知限制。** 结果明确声明 `engine_scope=approximate_index_futures_research`：它是日线近似
研究引擎，不支持日内开平仓、平今费率、交易所结算价逐日盯市、动态历史保证金率或盘中
强平。保证金只在日收盘检查，持仓按 close 而非交易所 settlement mark，账户采用累计
NAV 而非真实每日结算。期货事件日志仅记录换月与收盘保证金等诊断事件，不是完整订单/
成交/NAV 回放。期货结果不能与现金引擎结果合并成一个统一保证金账户。

源码与测试证据：

- [`PRODUCT_SPECS` — `diepi/futures/constants.py:4`](../../diepi/futures/constants.py#L4)：当前四个 CFFEX 产品的静态规格。
- [`_load_trading_calendar` — `diepi/futures/engine.py:30`](../../diepi/futures/engine.py#L30)：独立日历和覆盖证明。
- [`FuturesEngine.run` — `diepi/futures/engine.py:304`](../../diepi/futures/engine.py#L304)：信号策略及其显式稀疏模式。
- [`ContractSelector` — `diepi/futures/contract.py:180`](../../diepi/futures/contract.py#L180)：真实合约、到期日与滚动选择。
- [`ENGINE_SCOPE` / `DEFAULT_ASSUMPTIONS` — `diepi/futures/result.py:22`](../../diepi/futures/result.py#L22)：近似范围、收盘估值和非每日结算声明。
- [`test_truncated_calendar_cannot_self_certify_requested_tail` — `tests/futures/test_engine_synthetic.py:376`](../../tests/futures/test_engine_synthetic.py#L376)：截断日历不能自证覆盖。
- [`test_mapping_roll_executes_both_legs_at_same_close_with_two_sided_costs` — `tests/futures/test_roll_close_contract.py:101`](../../tests/futures/test_roll_close_contract.py#L101)：收盘换月双边成本回归。

## 使用这些能力时，先看三件事

1. 先看 `result.result_contract.status` 和 `is_rankable`，不要先看收益率。
2. 再看 `warnings` 与 `assumptions`，尤其是数据兼容路径、窗口截断、规则/会话快照、
   流动性、费用、T+0 覆盖和公司行为近似。
3. 最后确认自己选择的是共享资金的 `PortfolioEngine`、独立横截面的 `ParallelRunner`，
   还是范围完全独立的 `FuturesEngine`。

更精确的字段、默认值、能力矩阵和排错入口见
[《参考与边界》](04-reference-and-boundaries.md)；从安装、准备数据到第一次运行的完整流程见
[《用户手册》](03-user-guide.md)。
