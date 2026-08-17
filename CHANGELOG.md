# 变更记录

本文件记录 dieΠ 的用户可见变化。项目仍在准备首次公开；**未发布**下的内容描述的是
候选状态，不代表已经发布的版本或兼容性保证。

## 未发布

### 新增

- 本地 A 股与 ETF 现金回测的首次公开候选，以及独立的股指期货日线近似研究引擎。
- 新的命令闭环：`diepi doctor`、`diepi data validate`、`diepi data extract`、
  `diepi demo`、`diepi examples list/copy`、`diepi run`、`diepi compare runs` 与
  `diepi gui`；原 `diepi strategy.py ...` 保留为 run 兼容简写。
- 内置、版本化的 2010–2026 A 股交易日历；本地日历保留为严格完整 override，数据抽取
  工作区不再复制交易日历。
- wheel 内可列出和复制的 MA5/MA20 严格交叉示例，以及 raw-minimal 到已验证 Artifact 的
  端到端回归。
- `RuntimePaths` 显式路径解析，以及 `CacheManager` / `DataProvider` 的实例级
  `data_root` 注入；显式参数优先于环境变量，不需要修改进程全局状态。
- 确定性 generated synthetic demo：包含 raw/HFQ/复权因子、交易日历、证券元数据、
  dataset manifest、严格验证报告和默认回测；所有值均非真实行情。
- 按标的和日期范围的只读数据校验，以及版本化 dataset manifest 和逻辑内容摘要。
- 原子、本地、默认不可再分发的数据范围抽取器；保留前一交易日、双价格轨和复权锚点，
  不复制私有策略信号。
- 正式 combo bundle 回放：盘前目标权重与开盘后提交的当日收盘退出均带完整输入留痕，
  CLI 与 GUI 使用同一因果语义。
- `compare runs` 与 Python parity API：不取日期交集，核对 cash seed、成交事件顺序、费用/
  cash delta 与终态，要求完整指标口径，并分开报告经济投影和输入工件信任；raw/legacy
  顶层不会成功，正式认证会重新验证磁盘 RunArtifact。
- ETF 证券元数据按品种路由验证，并加强行情价格带与规则冲突的 fail-fast 检查。
- 正式支持的 PySide6 GUI Python/wheel 入口；不包含 standalone 桌面安装器。
- CLI 与 GUI 共用结果根和 `RunArtifact v1` 历史入口；GUI 增加 raw/dual/hfq 与日线容量配置、
  MA5/MA20 示例载入、原生净值回撤、成交到个股日期下钻、个股成交表、订单事件和并行 child
  下钻。历史 K 线只有在当前本地行情与工件记录的运行期文件指纹一致时才开放。
- 命令行工作流、示例、产品文档和显式源码工件检查。
- 结果状态、假设、警告和审计证据，用于显式标记不完整或不可比较的研究运行。
- `RunArtifact v1`：四类引擎结果的版本化 adapter、原子且不可覆盖的保存、封闭集合与
  hash/语义验证加载、结构化 provenance，以及显式 Python API。
- CLI 成功结果自动发布已验证 v1，失败结果尽力发布不可排名的诊断工件；规范成员位于
  `inputs/`、`tables/`、`evidence/` 和根部 manifest/config/provenance/result，同时保留
  manifest 覆盖的 summary/equity/orders 等兼容视图。
- GUI 保存按钮为组合结果和独立并行汇总发布 v1；历史页验证 v1，并把旧格式明确显示为
  legacy 未验证。
- `ArtifactStore.load_legacy()` / `load_legacy_result()` 安全只读迁移入口；旧
  `ResultStorage` 固定 `artifact_verified=false`、`is_rankable=false`，不因内嵌
  `SUCCESS` 契约升级信任等级。

### 变更

- 首批正式产品范围明确为用户自备本地数据的 A 股与 ETF/LOF 日线现金研究；分钟、独立
  并行和底层编排归为高级路径，股指期货日线近似研究归为实验范围。
- Python 最低版本与打包元数据统一为 3.10；GUI 依赖统一为 PySide6 与 pyqtgraph。
- PyPI 长描述不再依赖尚未公布公共仓库地址的相对链接；正式地址建立前使用纯文字路径。
- CLI 与 GUI 新保存结果统一使用 `RunArtifact v1`；既有 `ResultStorage` 目录仍只可通过
  legacy 入口读取，绝不因迁移或内嵌成功契约升级信任。
- CLI `--name` 收紧为可跨平台使用的 ASCII 标识，且继续拒绝覆盖既有工件或索引项。
- `raw/raw` 单价格空间明确不读取复权因子、也不应用因子公司行为覆盖；正式默认仍为
  `hfq/raw` dual，实际模式与公司行为模型写入结果 assumptions。
- PyArrow 最低版本提高到 `23.0.1`，排除截至候选审阅日已知受影响的旧版；CI 同时审计
  当前全依赖解析和最低直接依赖版本。
- 首次公共树采用精确文件白名单、离线 Markdown 链接/行锚点检查、无 Git 候选复核和
  固定提交版本的基础 GitHub Actions。

### 公开说明

- 项目仍为 Alpha 软件。
- 尚未公布发布日期、公共仓库地址或受支持版本周期。
- 只有最终代码、文档、依赖、许可证、隐私和工件审阅全部通过后，才会从最终快照建立
  首次公共历史。
