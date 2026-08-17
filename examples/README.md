# 示例策略

这五个策略用于展示框架 API 和研究流程。它们不是投资建议、经过验证的有效策略或未来
收益承诺。示例参数和标的仅供说明，未必适合其他时段、数据集、品种分类或成本模型。

第一次接触项目时，先运行 `diepi demo diepi_demo`。demo 使用程序生成的 synthetic 行情，
不是本目录任何策略的真实业绩样本。下面五个策略面向用户自行准备的数据。

| 文件 | 用途 | 主要执行方式 |
| --- | --- | --- |
| `ma_cross_strategy.py` | MA5 严格上穿 MA20 买入、下穿卖出的最小示例 | T-1 日线信号、T 开盘订单 |
| `etf_simple_test.py` | 带固定示例日期的 ETF 订单/API 小型诊断 | 日线开盘订单 |
| `etf_2b_reversal.py` | 可配置回看期和持有期的 ETF 2B 反转示例 | 组合式日线流程 |
| `etf_static_benchmark.py` | 通过环境变量配置的 ETF 静态配置基准 | 组合目标配置 |
| `chanlun_divergence_strategy.py` | 使用 `PortfolioStrategy` 的量价背离示例 | 日线选股与分钟监控 |

## 数据前提

示例不会下载或修复数据。请把 `DATA_ROOT` 指向满足产品文档数据布局和字段契约的本地
仓库。2010–2026 的独立 A 股交易日历由 diePi 内置；本地 `trade_cal.parquet` 只是完整
override。所选标的和完整研究区间必须具备价格模式所需的数据轨。默认 `dual` 需要
raw/HFQ/因子；显式 `raw` 只需 raw 日线，但不执行因子公司行为覆盖。

建议先按实际标的和日期只读校验：

```bash
diepi data validate --data-root /path/to/market-data --symbols 000001.SZ --start 20240101 --end 20241231 --price-mode dual
```

通过只说明当前 scope 满足结构与执行契约，不认证数据授权、真实性或经济正确性。

源码和 sdist 随附的四证券真实格式切片位于 `examples/market_data_v1/data`（wheel 不包含）。
从项目根目录可以直接完成校验、复制策略、股票+ETF 共享现金回测和 GUI 查看：

```bash
diepi data validate --data-root examples/market_data_v1/data --symbols 600000.SH,000001.SZ,510300.SH,159915.SZ --start 20260101 --end 20260630 --price-mode dual
diepi examples copy ma-cross ./ma_cross_strategy.py
diepi run ./ma_cross_strategy.py --data-root examples/market_data_v1/data --results-root ./diepi_results --symbols 600000.SH,510300.SH --start 20260101 --end 20260630 --price-mode dual --stamp-duty auto --daily-open-previous-day-ratio 0.1 --name public-ma-mixed
diepi gui --data-root examples/market_data_v1/data --results-root ./diepi_results
```

进入 GUI“历史记录”，双击 `public-ma-mixed`，再双击成交行即可查看个股交易和经指纹核验的
K 线。完整 5/15/30 分钟路径和 signals CSV 解耦示例见[项目首页](../README.md)。

如果本地源库很大，可先用 `diepi data extract` 抽取新的私有工作区；抽取不会复制策略
signals，且默认不可再分发。已有的“目标权重 + 当日收盘退出 + daily scope”冻结组合输入
应走 `diepi run --combo-bundle ...`，而不是改写本目录的教学策略来模拟其时间语义。

解释指标前，请检查数据契约警告和最终结果状态。进程正常完成不代表结果一定可排名。

## 日线集合竞价容量

日线 OPEN 或 CLOSE 订单必须显式配置集合竞价流动性假设。引擎不会推测当日竞价容量。
例如，开盘订单研究可以使用前一交易日成交额比例：

```bash
diepi examples copy ma-cross ./ma_cross_strategy.py
diepi run ./ma_cross_strategy.py --data-root /path/to/market-data --symbols 000001.SZ --start 20240101 --end 20241231 --price-mode raw --daily-open-previous-day-ratio 0.1
```

也可以使用对应的固定金额帽参数。策略提交 CLOSE 订单时，需要单独配置收盘容量。容量
帽只是研究假设，并不证明该规模的订单在真实市场一定能够成交。

该教学例比较相邻两个已完成时点的 MA5/MA20 关系，是真正的 crossing，不是“快线一直
在上就每天重试”的 regime。至少需要 21 个已完成日线观测；交叉订单若被规则或容量拒绝，
不会仅因均线仍保持同一侧而自动重试。

## 将示例用于研究之前

1. 阅读策略，识别所有回看窗口、执行窗口和退出条件。
2. 核验研究日期内的品种规则、费用、过户费率、价格轨和数据覆盖。
3. 按预先记录的研究计划替换示例标的和参数，不要根据已经看到的结果反复调参。
4. 比较运行前检查 `result_contract` 的状态、假设、警告和覆盖证据；CLI 新结果应再通过
   `ArtifactStore.load()` 验证为 `artifact_verified=True`。
5. 只使用合成数据或获得许可的本地数据，不要提交数据或结果产物。

这些示例的正式首批范围是日线现金研究。分钟回调、独立并行和股指期货近似研究属于
高级或实验路径，应先阅读精确边界，再选择对应数据与接口。

安装与运行见[用户手册](../docs/product/03-user-guide.md)，支持范围和已知限制见
[参考与边界](../docs/product/04-reference-and-boundaries.md)。
