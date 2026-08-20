# dieΠ 本地行情数据格式 v1

> 适用版本：`diepi 0.1.1` Alpha；格式版本：`diepi-local-market-data-v1`；校对日期：2026-08-20。
> 本文是股票与 ETF/LOF 现金引擎的本地行情输入规范。期货、signals CSV、combo
> bundle 和结果工件各有独立契约，不属于本格式。

> 文档导航：[项目首页](../../README.md) · [目录](README.md) ·
> [用户手册](03-user-guide.md) · [参考与边界](04-reference-and-boundaries.md)

## 1. 先记住这六条

1. `--data-root` 指向**包含 `parquet/` 的目录**，不是 `parquet/` 本身。
2. 行情的稳定公开后端是 Parquet；Excel/通用 CSV 不能直接作为行情输入。
3. **日线每标的、每价格轨一个普通 Parquet 文件**；一个文件可以放该标的全部历史。
4. **分钟线每标的一个目录，v1 按年份分片**；每个年份文件仍只能有一个标的。
5. 不支持把所有股票日线合成一个全市场文件，也不支持把单只日线自由拆成年度目录。
6. 默认 `dual` 需要后复权、原始价和复权因子三件套；最小入门可显式选 `raw`。

本文的“必须”是 v1 规范，“建议”是为了跨 pandas/PyArrow 版本保持一致的
规范写法。底层读取器为旧数据保留的宽容行为不会自动升格为 v1 承诺。
v1 是目录、表和语义契约，不需要在每个 Parquet 中自行增加 `format_version` 列。

## 2. 根目录和唯一推荐布局

```text
DATA_ROOT/
└─ parquet/
   ├─ metadata/
   │  ├─ common/trade_cal.parquet             # 可选：完整本地日历 override
   │  ├─ common/industry/mapping.parquet      # 可选：行业股票池
   │  ├─ stock/basic.parquet                  # 全市场/历史股票池需要
   │  └─ etf/basic.parquet                    # 可选：ETF 身份和上市元数据
   └─ timeseries/
      ├─ daily/{symbol}.parquet                   # 股票后复权日线
      ├─ daily_raw/{symbol}.parquet               # 股票原始日线
      ├─ adj_factor/{symbol}.parquet              # 股票复权因子
      ├─ minute/{symbol}/{year}.parquet           # 股票后复权 1 分钟
      ├─ minute_raw/{symbol}/{year}.parquet       # 股票原始 1 分钟
      ├─ etf_daily/{symbol}.parquet               # ETF/LOF 后复权日线
      ├─ etf_daily_raw/{symbol}.parquet           # ETF/LOF 原始日线
      ├─ etf_adj_factor/{symbol}.parquet          # ETF/LOF 复权因子
      ├─ etf_minute/{symbol}/{year}.parquet       # ETF/LOF 后复权 1 分钟
      └─ etf_minute_raw/{symbol}/{year}.parquet   # ETF/LOF 原始 1 分钟
```

### 2.1 symbol 和文件名

v1 使用六位代码、英文句点和大写交易所后缀：

```text
600000.SH
000001.SZ
430047.BJ
510300.SH
159915.SZ
```

文件名和文件内的 `ts_code` 均建议使用这个完整形式。读取器还尝试
`600000_SH.parquet` 这类历史文件名，但下划线文件名不是 v1 推荐输出。

股票和 ETF/LOF 根据 symbol 规则路由到不同目录。不要为了让一个代码被当成 ETF
而仅把它移到 `etf_*` 目录；代码身份、目录和实际品种必须一致。

### 2.2 文件粒度

| 数据 | v1 粒度 | 可以包含的时间 | 不支持的代替布局 |
| --- | --- | --- | --- |
| 日线 | 每标的、每价格轨一个文件 | 该标的全部历史，或一个明确切片 | `all_stocks.parquet`；`daily/{symbol}/{year}.parquet` |
| 分钟 | 每标的一个目录，每年一个文件 | 该 symbol 在该自然年的 1 分钟 bar | 全市场一文件；同一文件混合多 symbol |
| 复权因子 | 每标的一个文件 | 全部历史，或“基准锚点 + 切片日期” | 丢掉基准锚点的同期硬截取 |
| 元数据 | 每市场/类型一张表 | 多个 symbol | 用时序文件名代替主数据身份 |

日线查询会在读取单标的文件后按日期筛选，因此“一只证券一个全历史文件”是
推荐布局。它不是通用 Parquet dataset 扫描器；大型分钟数据应按年拆分，不要用
一个跨年超大文件模仿日线。

ETF 日线还有 `parquet/section/etf_daily/{date}.parquet` 和 `etf_daily_raw` 的旧截面
回退。它是专用兼容路径，**不属于本地行情数据格式 v1**。

### 2.3 文件系统边界

直接行情、因子、分钟分片、manifest、元数据和本地交易日历都必须是单链接的普通规则文件：

- 不能用同名 Parquet dataset 目录代替 `.parquet` 文件；
- 不能是 hard link（`st_nlink != 1`）、symlink、junction 或 Windows reparse point；
- 分钟 symbol 目录本身也不能是链接/重解析点；
- Parquet 压缩算法、row group 大小和 writer 不限定，但必须能被当前
  pandas/PyArrow 正常读取。

`0.1.1` 的读取上限是公开输入契约的一部分：

- `diepi_dataset.json` 最多 4 MiB，最多声明 16,384 个成员；
- manifest 成员路径最多 1,024 个 UTF-8 字节，每个路径段最多 255 个 UTF-8 字节；
- manifest 路径必须跨平台唯一：忽略大小写后不得冲突，并拒绝 Windows 盘符/ADS 冒号、
  保留设备名以及以空格或句点结尾的路径段；
- manifest 声明的每个 Parquet 成员最多 512 MiB；
- metadata Parquet 最多 256 MiB；本地 `trade_cal.parquet` 最多 16 MiB。

这些上限在打开文件和解析内容前 fail closed；超过上限时应拆分输入或缩小数据范围，不能
通过链接、目录替代或修改 manifest 绕过。没有 manifest 时，直接行情的布局和必需来源仍按
本文其他章节校验，但“每个 manifest 成员 512 MiB”不应被误读成所有未声明文件的通用承诺。

实现位置：[`plain_files.py`](../../diepi/backtest/data/plain_files.py)、
[`dataset_manifest.py`](../../diepi/backtest/data/dataset_manifest.py) 以及
[`CacheConfig` 与 `ParquetReader`](../../diepi/backtest/data/cache_manager.py)。

### 2.4 可选 dataset manifest

`DATA_ROOT/diepi_dataset.json` 不是 Parquet，也不是使用自有数据的必需文件。它可以声明
dataset 身份、symbols、范围和每个成员的逻辑表值 hash，使 Parquet writer、压缩或
row group 的字节差异不会改变逻辑数据身份。

有 manifest 时，`data validate` 会先验证其成员；没有时仍可校验用户自备数据，但报告会
记录 unmanifested 身份。manifest 只证明它声明的逻辑内容没有变，不证明数据真实、
可再分发或经济语义正确。正式 CLI 会在执行前核对 manifest 中每个成员的逻辑身份；运行
前后还会对实际价格轨、复权因子、证券 basic/行业 metadata 和本标的已知辅助时序做稳定
字节指纹。任一运行可达输入变化都会使本次运行 fail closed。大而宽的共享 manifest 会增加
执行前校验成本，生产者应为一次研究范围发布 scope-specific manifest，而不要在其中混入
大量无关证券或年份分区。实现见
[`dataset_manifest.py`](../../diepi/backtest/data/dataset_manifest.py)。

## 3. `raw` / `hfq` / `dual`

| `--price-mode` | 策略看到 | 撮合使用 | 需要的文件 | 适用性 |
| --- | --- | --- | --- | --- |
| `raw` | 原始价 | 原始价 | 对应 `*_raw` 行情 | 最小入门；不应将跨除权期间的曲线解读为复权总回报 |
| `hfq` | 后复权价 | 后复权价 | 对应非 raw 行情 | 单轨兼容/研究模式；撮合不在原始价空间 |
| `dual` | 后复权价 | 原始价 | HFQ + raw + `adj_factor` | 默认且正式研究推荐 |

`raw` 和 `hfq` 单轨时，同一份表同时担任 strategy 与 execution 轨，因此必须满足
execution 的完整字段契约。`dual` 中的 HFQ 表只担任 strategy 轨，但 v1 生产者
仍建议为 HFQ 表写入完整字段，便于同一数据直接切换到 `hfq` 单轨。

## 4. 所有 Parquet 共同规则

### 4.1 列名和物理类型

- 列名是大小写敏感的 ASCII snake_case；请使用本文精确列名，不要写 `Date`、
  `datetime`、`成交额` 或自定义同义词。
- 列名必须唯一，不得出现重复列。
- v1 规范写法如下；部分历史数值/日期类型可能被读取器兼容，但不应作为新数据的依据。

| 逻辑值 | v1 Parquet 类型 | 示例 |
| --- | --- | --- |
| 交易日 | UTF-8 string | `20260105` |
| symbol / 文本 | UTF-8 string | `600000.SH` |
| 分钟时间 | `timestamp[ns]`，无时区 | `2026-01-05 09:31:00` |
| 价格、成交额、复权因子 | `float64` | `12.34` |
| `is_open` | `int8` | `0` / `1` |

价格、成交额和复权因子不得用带逗号的文本，也不得包含 `NaN`、`+Inf`、
`-Inf` 或布尔值。日期和时间不得带时区；分钟时间的秒、微秒和纳秒必须为零。

### 4.2 行顺序和额外列

- 主时间键在文件中必须唯一、严格递增。
- 不要指望校验器自动排序、去重、取双轨交集、向前填充或修补数据。
- 运行时读取器可能容忍不参与契约的附加列，但它们不因此成为 v1 字段。
- `diepi data extract` 是隐私边界，遇到其公开列集之外的字段会 fail closed，
  避免把策略信号、研究注释或 DataFrame attributes 悄悄带入切片。

这里要区分“v1 规范”与“当前校验器已证明的范围”：分钟读取器会按时间整理读入的
行，`data validate` 也不会仅凭年度文件名独立证明文件内没有混入其他年份。因此，
一次校验通过不能替代生成端对物理行顺序和文件年份边界的检查。仓库自带样例另有
机器测试，直接核对这两项；自建转换器也应加入同等断言。

契约实现：[`contract.py`](../../diepi/backtest/data/contract.py)；切片边界：
[`extraction_service.py`](../../diepi/backtest/data/extraction_service.py)。

## 5. 日线文件

### 5.1 每个文件必须有什么

| 文件作用 | 精确必需列 | v1 可选列 |
| --- | --- | --- |
| raw execution（`raw` 或 `dual`） | `trade_date,open,high,low,close,pre_close,amount` | `ts_code,symbol,vol,change,pct_chg` |
| HFQ strategy（`dual`） | `trade_date,open,high,low,close` | `pre_close,amount,ts_code,symbol,vol,change,pct_chg` |
| HFQ strategy + execution（`hfq`） | `trade_date,open,high,low,close,pre_close,amount` | `ts_code,symbol,vol,change,pct_chg` |

**推荐的 v1 生产端规则：**raw 和 HFQ 日线都写入七个核心列：

```text
trade_date,open,high,low,close,pre_close,amount
```

这样同一批数据可在 `raw` / `hfq` / `dual` 之间切换，而不需要重写 Parquet。

### 5.2 日线字段字典

| 字段 | 是否核心 | v1 类型 | 单位/语义 | 校验规则 |
| --- | --- | --- | --- | --- |
| `trade_date` | 是 | string | `YYYYMMDD` 交易日 | 有效日期、唯一、严格递增；日级时间为 00:00 |
| `open` | 是 | float64 | 元/股或元/基金份额 | 有限且 `> 0` |
| `high` | 是 | float64 | 同上 | 有限且 `> 0`；不小于 open/low/close |
| `low` | 是 | float64 | 同上 | 有限且 `> 0`；不大于 open/high/close |
| `close` | 是 | float64 | 同上 | 有限且 `> 0` |
| `pre_close` | execution 必需 | float64 | 同一价格轨的上一收盘/价格带参考价 | 有限且 `> 0`（仅显式免检日例外） |
| `amount` | execution 必需 | float64 | **千元** | 有限且 `>= 0`；对齐后乘 1000 转成元 |
| `ts_code` | 可选 | string | 完整 symbol | 若存在，每行必须与请求 symbol 完全一致 |
| `symbol` | 可选 | string | symbol 身份列 | 若存在，每行必须与请求 symbol 完全一致 |
| `vol` | 可选 | numeric | v1 不赋予现金撮合语义 | 当前现金流动性帽不读取它；若保留应在数据说明中声明源单位 |
| `change` | 可选 | float64 | 价差，通常为元/份 | 诊断列；当前不参与核心撮合契约 |
| `pct_chg` | 可选 | float64 | 涨跌百分点 | 诊断列；当前不参与核心撮合契约 |

日线 `amount` 的**源单位必须是千元**。如果你的上游给的是元，转换到 v1 时必须
除以 1000；如果上游单位不明，停止转换，不要猜。

v1 不为浮点价格强制一个统一小数位数。raw OHLC 应保留该品种/日期的合法源精度，
HFQ 由因子映射后可以有更多小数位；转换器不应用统一 `round(2)` 或 `round(3)`
修改价格恒等式。下单、费用和品种价格精度是引擎规则，不是 Parquet dtype。

### 5.3 日线行与范围

- 每行是一个已完成的日 bar；不要为停牌日伪造开高低收。
- 数据契约不会自动证明所有应有交易日都存在；上市、退市、停牌与数据缺口仍需用户复核。
- `dual` 的 HFQ 与 raw 时间键必须完全相同；不会静默取交集。
- 文件可以包含运行结束日之后的行；运行会按请求窗口和策略可见性过滤。
- 一个日线文件过大时，当前读取路径仍会先读取该 symbol 文件再筛选；这是容量规划边界。

## 6. 1 分钟文件

### 6.1 路径与分片

```text
parquet/timeseries/minute/600000.SH/2026.parquet
parquet/timeseries/minute_raw/600000.SH/2026.parquet
parquet/timeseries/etf_minute/510300.SH/2026.parquet
parquet/timeseries/etf_minute_raw/510300.SH/2026.parquet
```

`minute` 是 1 分钟基础 bar，不是任意的 5/15/30/60 分钟上游表。高于 1 分钟的组合
频率由引擎在有效会话内严格重采样。v1 文件名使用四位自然年；一个年度文件
可以只包含当年的部分日期，但不得混入其他年或其他 symbol。

### 6.2 必需列

| 文件作用 | 精确必需列 | v1 可选列 |
| --- | --- | --- |
| raw execution（`raw` 或 `dual`） | `trade_time,open,high,low,close,pre_close,amount` | `trade_date,ts_code,symbol,vol` |
| HFQ strategy（`dual`） | `trade_time,open,high,low,close,pre_close` | `amount,trade_date,ts_code,symbol,vol` |
| HFQ strategy + execution（`hfq`） | `trade_time,open,high,low,close,pre_close,amount` | `trade_date,ts_code,symbol,vol` |

v1 生产端同样建议 raw 和 HFQ 分钟表都写入七个核心列。旧分钟文件缺
`pre_close` 时，provider 可能从**同 symbol、同价格轨**日线补充并记录来源；
这是可审计的历史兼容路径，新 v1 文件不应依赖它。

### 6.3 分钟字段字典

| 字段 | 是否核心 | v1 类型 | 单位/语义 | 校验规则 |
| --- | --- | --- | --- | --- |
| `trade_time` | 是 | timestamp[ns]，无时区 | 该 1 分钟 bar 的**结束时刻** | 唯一、严格递增；秒/子秒为零；位于该品种有效会话 |
| `trade_date` | 可选 | string | `YYYYMMDD` | 若存在，必须与同行 `trade_time` 的日期一致 |
| `open/high/low/close` | 是 | float64 | 元/股或元/基金份额 | 全部有限、`> 0`，且满足 high/low 包络 |
| `pre_close` | 是 | float64 | 同价格轨的上一收盘/参考价 | 有限、`> 0`；**同一交易日的每根分钟必须完全相同** |
| `amount` | execution 必需 | float64 | **元** | 有限且 `>= 0`；不再乘 1000 |
| `ts_code/symbol` | 可选 | string | symbol 身份 | 若存在，每行必须与请求 symbol 一致 |
| `vol` | 可选 | numeric | v1 不赋予现金撮合语义 | 源单位需在数据说明中自行声明 |

日线和分钟 `amount` 的单位故意不同：

```text
日线 amount = 千元
分钟 amount = 元
```

这个差异直接影响流动性帽，不能省略或猜测。

### 6.4 分钟时标与会话

dieΠ 把分钟时间解释为 bar 结束时刻：`09:30` 是独立开盘竞价观测，早盘连续
区间从 `09:31` 开始，下午首根连续 bar 是 `13:01`。收盘竞价的分组依交易所、
品种和生效日选择；输入时间不在任一有效会话时会失败，不会混入相邻 bar。

`dual` 的 HFQ 与 raw 分钟时间键必须完全相同。一个 symbol 目录中的所有
`*.parquet` 会被合并；因此不同年份文件之间也不得重复时间键。

会话实现：[`session_calendar.py`](../../diepi/backtest/session_calendar.py)；重采样实现：
[`minute_resampler.py`](../../diepi/backtest/engine/minute_resampler.py)。

## 7. 复权因子文件

### 7.1 必需列和类型

| 字段 | 是否必需 | v1 类型 | 规则 |
| --- | --- | --- | --- |
| `trade_date` | 是 | string | `YYYYMMDD`；唯一、严格递增 |
| `adj_factor` | 是 | float64 | 有限且严格 `> 0` |
| `ts_code` | 可选 | string | 若存在，每行必须与请求 symbol 一致 |
| `symbol` | 可选 | string | 若存在，每行必须与请求 symbol 一致 |

### 7.2 基准锚点和 AFI-1

因子文件的**第一行**是 HFQ 基准锚点：

```text
base_date   = 因子源第一行 trade_date
base_factor = 因子源第一行 adj_factor
hfq_close   = raw_close * (adj_factor / base_factor)
```

不同价格空间使用 `AFI-1` 校验上式，当前容差为 `rtol=1e-9`、
`atol=0.0050001`。因子要求是：

- 第一行锚点日不得晚于实际 bar 范围的第一日；
- 每个实际 bar 交易日必须有且只有一个精确因子；
- 不会用 `1` 代替缺失因子，也不会 forward-fill；
- 日线 dual 和分钟 dual 都使用交易日粒度因子；分钟数据的每个已观测交易日也必须被覆盖。

所以，“截取 2026 上半年”不能简单把因子文件只按日期过滤。如果原因子文件的
第一行早于 2026，切片仍必须保留该第一行，再加上切片中所有 bar 日期。

因子实现：[`contract.py` 中的 AFI-1](../../diepi/backtest/data/contract.py)。

## 8. 元数据文件

显式传入 `--symbols` 时，下列 basic/行业元数据通常不是行情撮合的硬依赖；缺失时
校验报告会保留未验证 warning。使用全市场、历史成员或行业股票池时，对应文件才成为必需。

### 8.1 `common/trade_cal.parquet`

2010-01-01 至 2026-12-31 已内置 `cn-a-share-2010-2026-v1`，该范围内不需要本地日历。
一旦本地文件存在，它就是**完整 override**，不会与内置日历拼接。

| 字段 | 要求 | 单位/格式 |
| --- | --- | --- |
| `cal_date` | 必需 | string `YYYYMMDD`；覆盖期从首日到末日的**每个自然日**都有一行 |
| `is_open` | 必需 | int8，只允许 `0` 或 `1` |
| `pretrade_date` | 可选 | nullable string `YYYYMMDD`；建议指向上一开市日 |

日期可按任意顺序给出并允许完全重复的同日状态被规范化，但 v1 生产者应输出
唯一、升序的自然日表。同一日出现冲突的 `is_open` 会失败；请求范围不在 override
覆盖内也会失败。

### 8.2 `stock/basic.parquet`

对显式 symbol 它是可选的；对全市场或历史窗口股票池，最少要有：

| 字段 | v1 类型 | 规则 |
| --- | --- | --- |
| `ts_code` | string | 必需；完整大写 symbol；每证券唯一 |
| `list_date` | string | 必需；`YYYYMMDD` |
| `delist_date` | nullable string | 必需列；未退市可为 null/空，否则 `YYYYMMDD` |

历史成员按 `[list_date, delist_date)` 解释。`name,industry,list_status,exchange,market`
等可作可选说明列，但当前名称和行业快照不能自动证明历史 ST/行业成员。

### 8.3 `etf/basic.parquet`

显式 ETF/LOF symbol 的运行不依赖这个文件，但附带它可让数据身份更清楚。v1 建议：

| 字段 | v1 类型 | 规则 |
| --- | --- | --- |
| `ts_code` | string | 必需（兼容读取也可用 `symbol`）；完整大写 symbol |
| `list_date` | string | 建议；`YYYYMMDD` |
| `delist_date` | nullable string | 可选；未退市可为 null/空 |
| `name,index_code,index_name,exchange,etf_type` | string | 可选说明列 |

### 8.4 `common/industry/mapping.parquet`

使用行业股票池时需要：

| 字段 | v1 类型 | 规则 |
| --- | --- | --- |
| `ts_code` | string | 完整 symbol |
| `industry` | string | 行业名称 |

当前行业表是快照而不是有效日期化成员史，因此不能仅靠它消除历史行业幸存者偏差。

另一个重要边界：当前未显式传 scope 时的“全市场”是股票池，不会自动合并所有
ETF。股票与 ETF 混合研究应显式给出 symbols，或由已冻结的 signals/combo 决定调仓范围。

元数据实现：[`calendar.py`](../../diepi/backtest/data/calendar.py) 和
[`stock_pool.py`](../../diepi/backtest/data/stock_pool.py)。

## 9. 切片、预热与因果边界

存储范围与实际回测范围不应被当成同一件事。例如 MA5/MA20 严格交叉在 T 日盘前
需要截至 T-1 的 21 个已完成日线观测。如果数据只从运行 `--start` 开始，策略前期不会有
足够历史。

推荐把三种范围分开记录：

```text
source range   原数据范围
storage range  切片实际保留的 bar（含预热/前一日）
run range      真正统计和解读的回测窗口
```

切片时必须同时考虑：

- 策略指标需要的预热 bar；
- 价格带、涨跌停或竞价容量需要的前一交易日；
- raw/HFQ 完全相同的时间键；
- 因子源第一行锚点和每个 bar 日的因子；
- 数据授权是否允许创建、备份或分发切片。

`diepi data extract` 可以从已符合约定布局的本地数据湖抽取双轨日线私有工作区，
自动保留前一交易日和因子锚点。它不是任意 CSV/Excel/数据库转换器，当前也不抽取
分钟文件。需要 21 个预热观测时，请把抽取起始日显式往前移；不要把“自动前一日”
误解为通用指标预热。

## 10. 配套的 2026 上半年示例切片

仓库中的配套目录是（完整清单见[样例 README](../../examples/market_data_v1/README.md)）：

```text
examples/market_data_v1/
├─ README.md
└─ data/                         # 这一层是 --data-root
   ├─ diepi_dataset.json
   └─ parquet/...
```

该静态切片进入公开 Git 与 source distribution，但不进入运行时 wheel；只用 `pip`
安装 wheel 的用户不应按下述仓库相对路径查找它，可使用内置 `diepi demo` 走通流程，
或另行取得项目源码/sdist 中的格式切片。

manifest/请求范围为 `20260101..20260630`；行情 bar 仅有内置日历中的 116 个
开市日 `20260105..20260630`。因子文件分别保留原始因子源的第一行作为 AFI-1
基准锚点，而不是把半年切片的首日重新设成 1。包含的证券为：

| symbol | 证券 | 因子锚点 | 目录路由 |
| --- | --- | --- | --- |
| `600000.SH` | 浦发银行 | `20100104` | `daily* / minute* / adj_factor` |
| `000001.SZ` | 平安银行 | `20100104` | `daily* / minute* / adj_factor` |
| `510300.SH` | 华泰柏瑞沪深 300 ETF | `20120528` | `etf_daily* / etf_minute* / etf_adj_factor` |
| `159915.SZ` | 易方达创业板 ETF | `20111209` | `etf_daily* / etf_minute* / etf_adj_factor` |

**重要：这里提交的是上述证券的真实历史行情小切片，不是 synthetic 数据。维护者自述
其来源为公开渠道，并决定继续通过只读规范化切片器生成和分发。公开可取得、个人使用或
非盈利用途不等于第三方已授予再分发许可；相应来源条款和证据由维护者负责核对。样例只
用于说明格式、加载和回测流程，不构成完整行情服务、官方行情基准、投资建议或策略业绩
证明。**

每标的有两份 116 行日线（raw/HFQ）、两份 27,956 行分钟线（raw/HFQ）和一份
117 行因子（锚点 + 116 个 bar 日）；`stock/basic.parquet` 和 `etf/basic.parquet`
各有 2 行。示例不落盘单位未统一的可选 `vol`。

从仓库根目录执行日线 dual 校验：

```powershell
diepi data validate --data-root examples/market_data_v1/data --symbols 600000.SH,000001.SZ,510300.SH,159915.SZ --start 20260101 --end 20260630 --price-mode dual
```

`data validate` 支持 `daily` 和 `minute` 两个公开 profile。分钟校验必须显式增加
`--frequency minute`；它同时验证分钟双轨以及同标的、同价格轨的伴随日线。伴随日线是
v1 分钟回测的正式必需输入：引擎用它约束已完成数据窗口，并向盘前策略提供因果日线
历史；分钟表自带 `pre_close` 只是不再依赖日线补列，不能替代伴随日线。
分钟文件为每标的 `2026.parquet`；每个开市日有 241 条源 observation：独立
`09:30` 开盘竞价，加 240 根已完成分钟 bar（`09:31..11:30`、`13:01..15:00`），
每标的共 `116 × 241 = 27,956` 行。可用小范围 `--freq minute` 运行或 Python
`DataProvider.get_aligned_pair(..., frequency="minute")` 进入同一严格数据契约。

若使用 MA5/MA20 教学策略，切片的前 21 个开市观测应用作预热；不要把 1 月早期结果
当成已经拥有 MA20 历史。示例目录中的 `README.md` 记录实际文件、行数、身份和复现命令。

## 11. 支持与不支持速查

| 输入方式 | v1 状态 | 说明 |
| --- | --- | --- |
| 一只证券一个日线全历史文件 | 支持，推荐 | 运行按日期筛选 |
| 一只证券一个分钟目录，按年分片 | 支持，推荐 | 目录中合并 `*.parquet` |
| 显式股票 + ETF 混合 symbols | 支持 | 各走自己的目录和品种规则 |
| 所有股票合成一个日线文件 | 不支持 | 即使有 `ts_code` 也不会自动扫描 |
| 日线按 `symbol/year.parquet` 拆分 | 不支持 | 日线读取器不合并这种目录 |
| 一个分钟文件混多 symbol | 不支持 | 每标的独立年度分片 |
| Parquet dataset 目录伪装成 `.parquet` 文件 | 不支持 | 直读路径只接受普通文件 |
| Excel/任意行情 CSV/数据库表 | 不直读 | 先转换到 v1，或自定义 provider |
| signals CSV / combo CSV | 是独立入口 | 它们是交易意图，不是行情文件 |
| ETF 按日期截面文件 | 仅兼容 | 不是 v1 推荐布局 |
| 期货日线合约文件 | 不属于本规范 | 见[参考与边界](04-reference-and-boundaries.md) |

## 12. 用 Agent 适配你的数据

用户下载源码后可以自行添加转换器或 provider；这是可扩展能力，不意味着 dieΠ 会猜测
任意私有表结构。优先把转换器放在数据源和 v1 之间，保持策略、撮合、账户和市场规则层不变。

可以把下列任务交给本地 Agent：

```text
只读取我的源数据，不要修改或删除源文件。
将数据转换为 dieΠ 本地行情数据格式 v1：
- symbol 使用 NNNNNN.SH/SZ/BJ，股票和 ETF 分目录；
- 日线每标的每价格轨一个 Parquet，分钟每标的按年分片；
- 字段名、dtype 和单位严格按规范；
- 日线 amount 转换为千元，分钟 amount 转换为元；
- 保留 raw/HFQ 完全时间键和 adj_factor 基准锚点；
- 不要排序后隐藏重复键，不要填补缺失价格或因子；
- 无法确认复权口径、amount 单位、分钟时标或证券类型时立即停止，不要猜；
- 输出日线后运行 diepi data validate，列出每个输出文件的行数、范围和 schema。
```

如果 Agent 直接修改 reader/provider 以支持“全市场单文件”、数据库或其他布局，该 fork
已经超出官方 v1 读取边界。应为新 provider 增加对齐、单位、因果可见性、因子恒等式和
结果证据测试，并在结果 assumptions 中披露自定义路径；不要为了兼容数据而关闭契约。

## 13. 数据许可与公开边界

项目的 Apache-2.0 代码许可不会自动覆盖用户行情、数据商数据或由它们产生的切片。
“只有四只证券”、“只有半年”、去掉名称或改成 Parquet，都不会自动创造再分发权。

发布和下游再分发时应分开审核：

- 代码和文档按项目许可发布；
- 真实数据的来源条款、证据充分性和再分发风险由维护者/再分发者分别负责确认；
- 用户本地 `data extract` 输出默认是私有、不可再分发；
- 每个可公开数据目录都应单独声明生成方式、是否真实行情、许可和研究用途。

本仓库 `examples/market_data_v1` 是项目所有者决定继续公开的真实行情规范化切片；该决定
不是上游书面授权已经存在的证明。
同目录 `generate.py` 必须显式接收 `--source-data-root`，只读取约定的四个证券和固定
日期范围，投影 v1 列、补充同价格轨的分钟 `pre_close`、规范化时间类型，并拒绝覆盖
目标目录；它不会记录源数据根的绝对路径。维护者的发布决定只适用于仓库精确列出的样例文件，
不表示完整本地行情库、任意上游数据或用户自行抽取的数据自动获得 Apache-2.0 许可或
再分发权。

## 14. 交付前检查清单

1. `--data-root` 是否正好指向包含 `parquet/` 的目录？
2. symbol、文件名、文件内身份列和股票/ETF 目录是否一致？
3. 日线是否每标的每轨一文件，分钟是否每标的按年分片？
4. 列名、dtype、时间键、排序、OHLC 包络和有限性是否满足本文？
5. 日线 `amount` 是千元、分钟 `amount` 是元吗？
6. 分钟时标是 bar 结束时刻，`pre_close` 在同日内完全一致吗？
7. dual 的 raw/HFQ 键完全一致，因子锚点和每个 bar 日因子都存在吗？
8. 存储范围是否包含策略需要的预热，而运行范围没有把预热期当成正式结果？
9. 请求是否位于内置交易日历范围；若有本地 override，它是否自然日连续且完整覆盖？
10. 是否拥有对数据执行当前使用、备份、切片或公开分发的相应权利？
11. 日线是否已使用精确 symbols/date/price-mode 运行 `diepi data validate`，并阅读整份报告？

校验通过只证明该请求范围满足当前结构与执行契约，不证明数据已获授权、来源真实、
市场语义完整或策略具有未来收益。
