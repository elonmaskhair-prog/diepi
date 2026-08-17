# diePi 本地行情数据格式 v1：真实数据切片

这里是一份可以被 CLI 和 GUI 直接读取的教学数据根目录：`data/`。项目所有者
从其有权公开的本地行情库中，按固定范围只读切片并规范化了以下内容：

- 沪市股票：`600000.SH`
- 深市股票：`000001.SZ`
- 沪市宽基 ETF：`510300.SH`
- 深市宽基 ETF：`159915.SZ`
- `2026-01-01` 至 `2026-06-30` 范围内的 116 个实际开市日
- 每个标的的 raw/HFQ 日线、raw/HFQ 一分钟线和复权因子
- 股票与 ETF 的最小 basic 元数据
- 可验证逻辑内容的 `diepi_dataset.json`

## 发布与使用边界

这些 Parquet 是上述证券的真实历史行情切片，不是合成走势。项目所有者已
确认拥有随本项目公开发布该切片的权利，本目录内容随仓库按 Apache-2.0
许可证发布。

示例仅用于说明数据格式、验证安装和运行教学策略，不保证数据适合任何特定
研究目的，也不构成投资建议、收益承诺或行情供应商真实性认证。用户进行正式
研究时，应自行核对数据来源、许可、复权口径、完整性和经济含义。

开市日期使用 diePi 内置的 `cn-a-share-2010-2026-v1` 日历。切片没有放置
本地 `trade_cal.parquet`，因此同时演示了内置日历的默认行为。

## 数据根目录

命令行和 GUI 中的 `data-root` 应选择：

```text
examples/market_data_v1/data
```

不是它下面的 `parquet/` 目录。

## 行数与时间范围

每个标的包含：

| 数据 | 每轨行数 | 实际行情范围 |
|---|---:|---|
| 日线 raw/HFQ | 116 | `20260105` 至 `20260630` |
| 一分钟 raw/HFQ | 27,956 | `2026-01-05 09:30` 至 `2026-06-30 15:00` |
| 复权因子 | 117 | 原始基准锚点 1 行 + 范围内 116 行 |

因子文件的第一行保留了源序列原始 HFQ 基准锚点，不属于 2026 年上半年的
行情评估行。股票锚点为 `20100104`；`510300.SH` 为 `20120528`；
`159915.SZ` 为 `20111209`。不能把锚点替换为 1，也不能只截取同期因子。

## 校验日线

在项目根目录执行：

```powershell
python -m diepi data validate `
  --data-root examples/market_data_v1/data `
  --symbols "600000.SH,000001.SZ,510300.SH,159915.SZ" `
  --start 20260101 `
  --end 20260630 `
  --price-mode dual
```

也可以把最后一项改为 `raw`，验证只使用原始价格轨的最小模式。当前
`data validate` 的首版命令只验证日线；一分钟双轨由框架读取时使用同一份
严格数据契约进行校验。

## 运行 MA 示例并在 GUI 查看

仍在项目根目录执行。下面的组合同时包含一只沪市股票和一只沪市宽基 ETF，并让默认费用
规则按品种和交易日自适应：

```powershell
diepi examples copy ma-cross ./ma_cross_strategy.py
diepi run ./ma_cross_strategy.py --data-root examples/market_data_v1/data --results-root ./diepi_results --symbols 600000.SH,510300.SH --start 20260101 --end 20260630 --price-mode dual --stamp-duty auto --daily-open-previous-day-ratio 0.1 --name public-ma-mixed
diepi gui --data-root examples/market_data_v1/data --results-root ./diepi_results
```

GUI 中进入“历史记录”，双击 `public-ma-mixed` 查看净值和回撤；双击成交行可进入该标的的
交易记录和 K 线。源码/sdist GUI 的“载入公开样例”按钮会载入同一数据根、股票+ETF、日期
和执行假设。wheel 不包含本目录行情，因此该按钮在纯 wheel 环境中会禁用并说明原因。

## 用同一切片运行 signals CSV

目标权重 CSV 是策略信号与回测执行解耦的推荐入口之一：

```powershell
python -c "from pathlib import Path; Path('signals_mixed.csv').write_text('date,symbol,target_weight\n20260106,600000.SH,0.5\n20260106,510300.SH,0.4\n20260302,600000.SH,0.2\n20260302,510300.SH,0.7\n20260601,600000.SH,0\n20260601,510300.SH,0\n', encoding='utf-8')"
diepi run --signals ./signals_mixed.csv --signals-format target --data-root examples/market_data_v1/data --results-root ./diepi_results --start 20260101 --end 20260630 --price-mode dual --stamp-duty auto --daily-open-previous-day-ratio 0.1 --name public-signals-mixed
diepi gui --data-root examples/market_data_v1/data --results-root ./diepi_results
```

GUI 历史会同时读取 CLI 生成的代码策略结果和 signals 结果。完整 signals 时间语义、动作型
格式和 combo bundle v1 输入契约见[用户手册](../../docs/product/03-user-guide.md)。

## 一分钟时间语义

每个交易日恰好 241 条 observation：

```text
09:30                         独立开盘集合竞价 observation
09:31 ... 11:30              120 根已完成 bar
13:01 ... 15:00              120 根已完成 bar
```

`trade_time` 是无时区的 `timestamp[ns]`。09:30 行让分钟引擎严格执行开盘
集合竞价，其余 240 行对应连续交易分钟。

源分钟文件没有 `pre_close`。构建器按“相同标的、相同价格轨、相同交易日”
从对应日线确定性补入该字段；不会从另一价格轨推测，也不会跨日填充。

分钟成交额 `amount` 的单位为元；日线成交额 `amount` 的单位为千元。受源端
小数舍入影响，分钟成交额之和与日线成交额可能存在约 `1e-8` 量级的相对
差异。OHLC 应按相应价格精度核对。

本切片没有落盘可选字段 `vol`。框架 v1 尚未替不同来源统一日线/分钟
`vol` 的来源单位；需要该字段时，应由用户的数据适配器明确单位。

## 只读复现切片

`generate.py` 是确定性的本地只读切片构建器，不生成价格、不访问网络、
不修改源文件，也不会把源目录写入输出文件。源数据根目录必须包含
`parquet/`；目标目录必须尚不存在：

```powershell
python examples/market_data_v1/generate.py `
  --source-data-root <SOURCE_DATA_ROOT> `
  --output .tmp-market-data-v1
```

构建器只读取四个标的、固定日期范围和 v1 所需字段，并执行以下规范化：

- ETF 源文件的点号名或下划线兼容名均可读取，输出统一为点号名；
- `trade_date` 统一为 `YYYYMMDD` 字符串；
- `trade_time` 统一为无时区 `timestamp[ns]`；
- 日线和分钟只保留 `ts_code`、时间键、OHLC、`pre_close`、`amount`；
- 分钟 `pre_close` 只从同轨日线补充；
- 因子保留源文件第一行基准锚点及范围内逐日数据；
- basic 元数据只保留公开格式要求的六个字段。

两个目录的 `diepi_dataset.json` 中 `manifest_sha256` 相同，即表示全部
Parquet 的逻辑表内容相同；Parquet writer 或压缩元数据差异不会影响逻辑
身份。
