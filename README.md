# dieΠ

## 写在项目前：为什么做 dieΠ

> 这是一篇作者序，记录项目的来由和我对回测的理解，不是使用 dieΠ 的前置材料，也不构成
> 任何投资建议。当前能力、默认值和限制以 `docs/product/02-core-features.md`、
> `docs/product/03-user-guide.md`、`docs/product/04-reference-and-boundaries.md` 以及每次运行
> 保存的 `result_contract` 为准。

2020 年那会儿，我刚研究生毕业，入职前闲得没事，看到我爹每天在看一些主播讲炒股。
什么首板、二板、烂板、龙回头，讲得激情澎湃，动不动就是多少个涨停的赚。我自然是不信
的。我从 2014 年开始炒 A 股，随后经历人生第一次股灾，之后炒过鞋、潮牌、加密货币，
后来还在元宇宙买过地，也算有一定的交易经验。我认为，“炒”这个字精确概括了这类行为。

当时我就问我爸：

1. 人家如果真的赚到钱，还直播干什么？
2. 互为对手盘，人家难道不怕自己的策略失效？
3. 人家其实是想让你跟进去，替他抬轿子。

我爸的回答是：他在博采众长，主播说的东西他自己会验证，还会结合缠论、推背图、江恩等
中西古今理论来辩证思考……

他的回答的确让我一时无法反驳。除了缠论看过一点，其他几个我都不懂。而且我突然想起，
他曾经教过我用黄金分割加江恩波段线画图。我当时给同学炫耀，分析微软股价，画出了一个
上升通道；后来一个月的最高价和最低价真的落在我画的区间里，还碰到了上下轨道线。一次
偶然的成功，看起来像是完美预测了后来一个月的趋势。我学过高数、统计学，也挂过线代，
会写一点 Python、R、SAS，却无法用自己掌握的知识解释这件事，索性选择了相信。

随后，我按照他的思路，在当时的一个在线量化平台上写了个“抓庄家”的算法，逻辑很简单：

1. 某个小盘股涨停，一定是有大资金介入，不可能是散户主导买出来的。
2. 大资金买入，一定是要盈利的。
3. 在前两个假设下，如果该小盘股出现过独立于行业或概念的缩量连跌、平台盘整、拉升试盘，
   涨幅较低且换手率连续两天增加——我当时把它理解成利好消息偷跑——那么我就买入。等到
   它涨停点火，再择机卖出；如果一直不涨停，就持有到止盈或止损发生。

回测买过一堆股票，胜率大约四成，整体小亏。但从选股结果里，的确抓到过后来翻很多倍的
股票。

我根据结果反过头调参数：换手率是 1.5、1.6 还是别的倍数，拉升试盘是 3% 到 8% 中的
哪个值。用过拟合的方式“确定”参数后，我就去实盘了。周四买了两只股，其中一只周五微跌
3%，下周一开始连续五个涨停，我全吃到了；另一只没有这么猛，但最终也赚了约 4%。这给了
我极大的信心——这是我的第一次量化。之后下一笔交易，我信心爆棚，加钱追涨停板，结果
巨亏。

到了 2026 年，我爹还是在玩“抓庄家”的那一套。我也从早期大模型时代开始设计这套框架，
伴随不同模型和自己的策略研究反复迭代到现在。那次先幸运、再过拟合、最后亏损的经历，
一直提醒我：一条漂亮曲线并不能自动证明策略可靠。

### 为什么开源

第一个原因，是我看到现在仍有很多人相信直播荐股，也不断看到因此受骗的新闻。我希望更多
普通人能借助工具和 AI，把一个说法放进数据和规则里验证，在市场中少一点盲从。

第二个原因，是我一直被开源和分享精神打动。不只是做项目、写代码，烹饪、健身、摄影、
旅游……我的生活很多方面都受益于前人的经验和建议。我也希望把自己反复踩坑后整理出的
工具公开出来，给别人提供一个可以检查、质疑和继续改进的起点。

### dieΠ 现在能做什么

对于有量化背景的朋友，dieΠ 当前主要有这些特点：

1. **本地优先。** 框架内置的数据读取、回测和结果保存都在本机完成，不主动上传数据或
   策略；用户策略和第三方依赖仍可能自行联网。独立并行是把同一策略、同一区间和同一参数
   运行在多个标的、多个独立账户上，不是共享资金组合或参数网格；实际速度也受数据 I/O、
   进程开销和机器资源共同影响。
2. **按品种和日期处理 A 股规则。** 当前覆盖交易单位、T+1、不同板块的历史涨跌停和显式
   的新股豁免日历。新股豁免依赖 `list_date` 等元数据；注册制前首日特殊价格带等历史细节
   仍有明确近似，不能理解成完整复刻全部交易所历史规则。
3. **把研究价格和成交价格分开。** 默认双价格轨让策略观察后复权价格、撮合使用不复权
   价格，并显式处理滑点、资金冻结和单 bar 流动性帽。这是基于 OHLC/bar 的执行模型，
   不是逐笔委托或 Level-2 排队模拟。
4. **策略生成与执行回放可以分开。** 可以在策略生命周期中直接生成订单，也可以把预先
   计算的目标仓位或交易清单交给框架执行。项目提供适合脚本和自动化调用的 CLI，以及
   正式支持的本地 GUI；它随 Python 包/wheel 和可选依赖交付，不是独立桌面安装器。CLI
   本身不是专门的 AI 协议。
5. **现金与期货范围明确分开。** 现金引擎支持股票及 ETF/LOF 的日线和分钟回测；IF、IC、
   IH、IM 使用独立的日线近似期货引擎。两者目前不共享现金和保证金账户，因此“对冲”只能
   作为独立策略腿研究，不能描述成已经支持股票与期货的一体化组合对冲回测。

对于没有量化背景的朋友，可以先把量化理解为“数据 + 策略 + 回测”。数据是按日或分钟整理
的行情和必要元数据；策略描述在什么条件下产生交易意图；回测按框架明确披露的 bar 级撮合
与交易规则重放历史。它能统一处理一部分常见约束，但并不等同于交易所级真实成交还原。

普通用户可以使用 dieΠ，但需要自行准备有权使用的真实行情。首批公开版本最适合已经有
本地 Parquet 数据的研究者；还没有数据时，可以先用程序生成的 synthetic demo 检查安装、
数据契约、策略生命周期和结果工件。synthetic demo 的所有价格与成交量都是虚构值，不能
用来评价策略，更不能当作真实行情转发。

如果你需要长周期、高可信度的专业数据，我推荐tushare平台！

我开源的是研究工具，不是收益承诺。框架把一些常见的前视、成交容量和结果完整性问题显式
化，但数据来源、清洗质量、策略过拟合和研究结论仍由使用者负责。比较结果前，请先检查
`result_contract`。

我的碎碎念到这里结束。接下来的产品说明书，请叫上你的硅基朋友：先看
`docs/product/02-core-features.md`，需要动手时看 `docs/product/03-user-guide.md`，查精确
默认值和限制时看 `docs/product/04-reference-and-boundaries.md`。

## 使用前先确认范围

当前源码版本是 `0.1.1` Alpha。项目品牌写作 dieΠ；Python 发行名和命令行入口写作
`diepi`。公开 API 位于 `diepi.backtest`、`diepi.futures`、`diepi.artifacts`；Agent/MCP 等
编排 adapter 使用版本化 `diepi.integration` capability 契约。

当前正式主路径是沪深北 A 股与 ETF/LOF 的日线现金研究，以及自备本地数据的诊断、严格
校验、CLI/Python 回测和 GUI 结果检查。分钟现金回测、独立并行和底层 Python 编排属于高级
路径；`IC/IM/IF/IH` 是与现金账户分开的实验性日线近似期货引擎。

当前不提供 standalone `.exe`、`.dmg` 或 Linux 桌面安装器，也不负责自动下载行情、通用
Excel/数据库导入、tick/Level-2 队列还原、实盘交易、股票与期货共享保证金账户、参数网格
或多策略调度。

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

安装后先做一次只读体检：

```bash
diepi doctor
```

尚未配置行情时，`doctor` 会报告 `NOT_CONFIGURED`/警告，不阻止 synthetic demo；显式传入
不存在或不合格的数据根仍会失败。

## 5 / 15 / 30 分钟首次体验

5 分钟 demo 可以从新的工作目录执行；15 / 30 分钟的真实切片示例必须从源码 checkout 或
解压后的 sdist 项目根目录执行。若系统找不到 `diepi`，可把命令开头替换成
`python -m diepi`。

### 5 分钟：验证安装，并在 GUI 打开结果

```bash
diepi demo diepi_demo
diepi gui --data-root diepi_demo/market-data --results-root diepi_demo/results
```

第一条命令创建 deterministic synthetic 数据、严格校验并运行一次日线回测；第二条命令
打开同一数据根与结果根。进入“历史记录”，双击 `synthetic_demo`，即可查看净值、回撤、
交易和个股明细。demo 中所有价格、成交量、交易日和证券名称均由程序生成，只能验证安装、
数据契约、策略生命周期和结果流程，收益没有研究或投资含义。

### 15 分钟：用真实切片跑股票 + ETF 的 MA 示例

真实切片就在仓库的 `examples/market_data_v1/data/parquet/`，包括深沪各一只股票、两个宽基
ETF 的 2026 年上半年日线、分钟线和复权因子。GitHub 不会把 Parquet 渲染成表格；可以浏览
文件树、下载后读取，或者直接运行下面的校验命令：

https://github.com/elonmaskhair-prog/diepi/tree/main/examples/market_data_v1/data/parquet

wheel 不附带这份真实切片；源码 checkout 和 sdist 包含它。从项目根目录依次执行：

```bash
diepi data validate --data-root examples/market_data_v1/data --symbols 600000.SH,000001.SZ,510300.SH,159915.SZ --start 20260101 --end 20260630 --price-mode dual
diepi examples copy ma-cross ./ma_cross_strategy.py
diepi run ./ma_cross_strategy.py --data-root examples/market_data_v1/data --results-root ./diepi_results --symbols 600000.SH,510300.SH --start 20260101 --end 20260630 --price-mode dual --stamp-duty auto --daily-open-previous-day-ratio 0.1 --name public-ma-mixed
diepi gui --data-root examples/market_data_v1/data --results-root ./diepi_results
```

这次运行把沪市股票 `600000.SH` 与宽基 ETF `510300.SH` 放在同一现金组合中。GUI 中进入
“历史记录”，双击 `public-ma-mixed`；再双击成交行，可以查看个股成交记录和经行情指纹
核验的 K 线。

### 30 分钟：让策略信号与回测解耦

先创建一份小型目标权重信号。下面这条 Python 命令在 PowerShell、cmd、Bash 中都可执行：

```bash
python -c "from pathlib import Path; Path('signals_mixed.csv').write_text('date,symbol,target_weight\n20260106,600000.SH,0.5\n20260106,510300.SH,0.4\n20260302,600000.SH,0.2\n20260302,510300.SH,0.7\n20260601,600000.SH,0\n20260601,510300.SH,0\n', encoding='utf-8')"
diepi run --signals ./signals_mixed.csv --signals-format target --data-root examples/market_data_v1/data --results-root ./diepi_results --start 20260101 --end 20260630 --price-mode dual --stamp-duty auto --daily-open-previous-day-ratio 0.1 --name public-signals-mixed
diepi gui --data-root examples/market_data_v1/data --results-root ./diepi_results
```

GUI 历史中会同时出现代码策略 `public-ma-mixed` 和信号回放 `public-signals-mixed`。signals 的
`date=T` 表示 T 日盘前已知、T 日开盘提交；如果信号由 T 日收盘数据产生，应把执行日期写成
下一交易日，不能把收盘信息回填成当日开盘信号。

## 使用自己的本地数据

dieΠ 不下载、不上传，也不附带完整行情库。公开源码和 sdist 包含一份由维护者从公开
渠道整理并决定继续分发的四证券、半年真实行情格式切片；运行时 wheel 不包含该切片。
公开可访问、个人使用或非商业使用本身不等于第三方已授予再分发权，代码的 Apache-2.0
许可也不自动覆盖行情数据。上游条款和证据链由维护者负责持续复核；下游再分发者仍应
独立确认适用权利。

每个 Parquet 的字段名、dtype、单位、日线/分钟分片、复权因子锚点和完整示例见
`docs/product/05-local-market-data-format-v1.md`。最少的 raw 日线工作区只需所选标的一个
文件：股票放在 `daily_raw/`，ETF/LOF 放在 `etf_daily_raw/`，字段为
`trade_date,open,high,low,close,pre_close,amount`。

默认 `dual` 模式要求研究用 HFQ 轨、原始撮合轨和复权因子严格对齐。
如果研究只需要单一价格空间，应显式选择 `--price-mode hfq` 或 `--price-mode raw`；缺少
任一价格轨时，数据层不会把 `dual` 静默降级为单轨。

现金引擎内置 `cn-a-share-2010-2026-v1` 交易日历，覆盖 `20100101..20261231`；范围内不必另备
`trade_cal.parquet`。如果提供本地日历，它会作为完整 override 使用，不与内置日历拼接。

先对具体标的和日期做只读校验：

```bash
diepi data validate --data-root /path/to/market-data --symbols 000001.SZ --start 20240101 --end 20241231 --price-mode dual
```

校验不会下载、排序、取交集、填充或修复数据；通过只证明请求范围满足当前结构与执行契约，
不证明数据授权、供应商真实性或经济含义。

已有兼容数据湖时，可以只在本机抽取限定日期和标的的私有工作区：

```bash
diepi data extract --source-data-root /path/to/market-data --workspace ./my-private-sample --symbols 159915.SZ,510300.SH --start 20260112 --end 20260515 --include-metadata
```

抽取器只读源目录、拒绝覆盖目标，保留必要预备日、双价格轨和因子锚点；输出默认标记为私有
且不可再分发。安全边界、字段白名单和平台限制见用户手册与参考文档。

## 三种策略输入，一套执行语义

`diepi run` 与 GUI 支持三种互斥输入：

- **策略代码**：在受信任的本地 Python 回调中计算并提交交易意图；
- **signals CSV**：读取目标权重或买卖动作，适合把策略生成与回测执行解耦；
- **冻结 combo**：同时表达盘前目标和运行前已经确定的当日收盘退出。

框架的执行边界是规范化后的订单、目标权重和定时收盘意图，不是某一种 CSV。数据库、模型
输出或个人文件格式可以先由用户代码转换成这些意图；dieΠ 不会猜测任意 Excel、数据库表
或私有信号格式。

显式组合池可以把受规则簿支持的股票与 ETF/LOF 放进同一个 `PortfolioEngine`，共同使用
一笔现金。每个标的仍分别应用数据目录、价格精度、涨跌停、申报单位和 T+0/T+1；
`--stamp-duty auto` 会按品种处理股票和场内基金印花税。

冻结 combo 可以先只读验证，再运行：

```bash
diepi combo validate ./my-combo --tag reviewed-v1 --json
diepi run --combo-bundle ./my-combo --data-root /path/to/market-data --results-root ./diepi_results --start 20260112 --end 20260515 --daily-open-previous-day-ratio 0.1 --daily-close-previous-day-ratio 0.1 --name combo-replay
```

完整 signals/combo 字段、日期语义和最小构造例见 `docs/product/03-user-guide.md`。

## 结果工件、比较与 GUI

一次 CLI 运行会原子发布一个 `RunArtifact v1`。先检查结果中的
`result_contract.status`、`rankable`、`warnings` 和 `assumptions`；需要程序化消费时，用
`diepi.artifacts.ArtifactStore.load()` 重新验证 manifest、成员 hash 与结果语义。失败运行也会
尽力留下不可排名的结构化诊断，不会伪装成成功结果。

两个 v1 现金结果可以生成正式 parity 报告：

```bash
diepi compare runs ./diepi_results/baseline ./diepi_results/candidate --report ./parity.md
```

安装 GUI 依赖后运行：

```bash
diepi gui --data-root /path/to/market-data --results-root ./diepi_results
```

CLI 与 GUI 指向同一个 `results-root` 时，CLI 结果会出现在 GUI 历史页。GUI 可查看资产净值、
回撤、成交、持仓、订单事件和个股明细；只有当前日线文件与运行时保存的 SHA-256 指纹一致
时，历史页才允许加载 K 线并叠加成交。GUI 不是安全沙箱，重要结论仍应回到结果契约与工件
验证状态。

## 先知道的研究边界

- “本地优先”描述内置读写路径；用户策略和第三方依赖仍可能自行联网。
- 现金撮合基于 OHLC/bar，不模拟委托簿排队；流动性帽不是冲击成本模型。
- raw OHLC 与价格带冲突时会失败，不靠夹价制造一个可成交结果。
- 日线集合竞价必须显式配置容量；费用和部分历史规则需要研究者复核。
- 默认费用按分使用 Decimal half-up；实盘校准应以券商协议和交割单为准。
- 双价格轨失败时不会静默取交集或用另一轨补齐。
- 只有完整 `SUCCESS` 结果具备基础排名资格；跨结果比较还要证明观察范围一致。
- 历史回测不能消除前视偏差、幸存者偏差、过拟合、数据错误和真实交易风险。

## 文档、源码与参与

项目仓库：

https://github.com/elonmaskhair-prog/diepi

PyPI 会直接渲染本 README，因此这里使用可复制路径而不是无法解析的相对 Markdown 链接：

- `docs/product/01-author-note.md`：作者序兼容入口；唯一正文就是本页开头；
- `docs/product/02-core-features.md`：因果、双轨、规则、审计与可比性；
- `docs/product/03-user-guide.md`：完整安装、数据、策略、运行和结果手册；
- `docs/product/04-reference-and-boundaries.md`：精确支持矩阵、默认值和失败边界；
- `docs/product/05-local-market-data-format-v1.md`：Parquet 目录、字段、单位与 Agent 适配契约；
- `CONTRIBUTING.md`：贡献与测试要求；
- `SECURITY.md`：安全报告方式；
- `THIRD_PARTY_NOTICES.md`：依赖许可证披露；
- `CHANGELOG.md`：用户可见变更。

安全问题不要公开披露，使用 `SECURITY.md` 中的 GitHub 私密报告入口。
本文不虚构尚不存在的邮箱或下载地址；外部渠道只写入已经建立的实际 URL。

## 免责声明

dieΠ 是回测研究工具，不构成投资建议，也不承诺任何收益。历史回测不能替代对数据质量、
来源权利、前视偏差、过拟合、容量假设和真实交易风险的独立检查。
