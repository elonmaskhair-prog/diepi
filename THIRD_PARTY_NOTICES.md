# Third-party dependency notice

diePi 的源码采用 Apache-2.0。项目所有者已在
`docs/development/source-ownership.md` 留下本轮权属确认；当前源码发行不包含用户策略或
复制的上游源码。源码和 sdist 中的 `examples/market_data_v1/data` 是项目所有者单独确认
可公开分发的四证券、半年真实行情格式切片，运行时 wheel 不携带它。Apache-2.0 不会因此
自动覆盖完整本地行情库、上游数据或用户自行抽取的数据；该边界不是对第三方数据条款的
一般性授权。

发行包内置的 2010–2026 A 股交易日历是项目依据沪深交易所公开休市公告重新整理的日期
事实表，只包含自然日与开/休市状态；它不包含第三方程序代码、行情价格、成交量或供应商
Parquet。公告索引与 2026 年原始公告入口记录在 `diepi/backtest/data/calendar.py`。

安装 diePi 时，包管理器会分别解析下列第三方依赖。它们不是被复制进 diePi wheel 的
源码，仍由各自作者按各自许可证授权：

| 用途 | 包 | 许可证（上游声明） |
| --- | --- | --- |
| 核心 | NumPy | BSD-3-Clause / modified BSD |
| 核心 | pandas | BSD-3-Clause |
| 核心 | PyArrow / Apache Arrow | Apache-2.0 |
| 可选 GUI | PySide6、Shiboken6、Qt for Python wheels | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only；另有商业发行 |
| 可选 GUI | pyqtgraph | MIT |

开发与发布检查还使用 pytest、setuptools、build、coverage、Ruff 和 pip-audit；这些工具
不属于运行时 wheel payload。完整的传递依赖及其许可证应以实际锁定/安装版本携带的
metadata 和 license 文件为准。

首次候选把 PyArrow 下界设为 `23.0.1`；该版本包含 Apache Arrow 2026-02 公布的 IPC
reader 安全修复。依赖漏洞信息会变化，因此发布门禁仍需在每次候选冻结时重新审计，而不
把本文件的审查日期当作长期保证。

本项目当前只发布 Python 源码/wheel，不捆绑 standalone GUI 安装器。若以后冻结或重分发
Qt/PySide6 二进制、Qt 插件或其他第三方组件，必须在发布前重新审查所选 Qt 模块、许可证
文本、notice、可替换/重新链接要求和源代码提供义务。本文件是依赖披露，不是法律意见。

上游许可入口（审查日期：2026-08-12）：

- NumPy：`https://numpy.org/about/`
- pandas：`https://github.com/pandas-dev/pandas/blob/main/LICENSE`
- Apache Arrow：`https://arrow.apache.org/` 与其发行 LICENSE
- Qt / Qt for Python：`https://doc.qt.io/qt-6/licensing.html`
- pyqtgraph：`https://github.com/pyqtgraph/pyqtgraph/blob/master/LICENSE.txt`
