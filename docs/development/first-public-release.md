# dieΠ 0.1.1 补丁发布清单

当前 diePi 公共源码历史和 `0.1.0` release 于 2026-08-18 建立并发布：
<https://github.com/elonmaskhair-prog/diepi>。本文现记录 `release/0.1.1` 补丁候选的发布门禁；
分支或本文的存在不代表已经合并 `main`、创建 release 或上传 PyPI。所有未勾选项都是实际
未完成项。

## 当前发布阻断项

以下事项需要仓库或账号的实际状态，不能在本地源码中伪造：

- [x] 准确的仓库、问题跟踪、变更记录和安全政策 URL 已写入包元数据。
- [ ] 启用私密漏洞报告渠道，由维护者账号完成一次不含敏感信息的收发验证，并更新
  `SECURITY.md` 的准确入口。
- [ ] 为 `pypi` GitHub Environment 配置审批规则，并在 PyPI 为准确的 owner、repository、
  workflow 文件 `.github/workflows/release.yml` 和 environment `pypi` 配置 Trusted Publisher。
- [x] `diepi` PyPI 项目已存在且 `0.1.0` 已公开；`0.1.1` 不得复用旧版工件。
- [ ] 留存并由项目所有者确认真实示例行情的可再分发证据包（最低材料见下文）。
- [ ] 从公开仓库的 fresh clone 构建候选，对同一批工件运行完整测试、精确目录树和安装门禁。

发布工作流默认只构建、校验、生成 SHA-256 清单并上传 GitHub Actions 工件。只有 tag 运行、
PyPI Trusted Publisher 与 `pypi` environment 已配置，而且仓库变量
`DIEPI_PYPI_TRUSTED_PUBLISHING` 被所有者明确设为 `enabled` 时，PyPI job 才会运行。该变量在
上传包未获单独授权前必须保持未设置。

## 不可跨越的边界

- 不把本机会话、凭据、策略、结果、未审查数据或构建缓存带入 commit/工件。
- 不绕过候选分支、PR、CI 和精确 tag 门禁直接向 `main`、GitHub release 或包索引发布。
- 不把“测试通过”“构建成功”解释成发布授权。
- commit/push、tag/release 和 PyPI 上传仍是可分别确认的外部动作。
- 原始审查材料、行情、策略、回测结果、凭据、本机路径和临时构建产物默认不公开。

## 0.1.1 候选的来源

候选必须以公开仓库 `main` 的 fresh clone 为基线。当前正式工作树满足这一基线要求；
无 `.git` 的开发快照只能作为已审查变更来源，不能直接作为发布来源：

1. 冻结待审候选，记录源码、文档、依赖、许可证和构建工件的摘要。
2. 运行数据无关测试、发布门禁、文档链接和隐私扫描。
3. 在 fresh clone 上只应用已审查变更，检查 `git diff` 与精确公共白名单。
4. 在新 clone 再次检查敏感文本、测试、sdist、wheel 和隔离安装冒烟。
5. 将 diff 和唯一的已测工件交给项目所有者作最终检查。
6. commit/push、tag/release 和 PyPI 上传分别等待明确授权。

## 发布源目录

```text
diepi/
├─ README.md
├─ LICENSE
├─ CONTRIBUTING.md
├─ SECURITY.md
├─ CHANGELOG.md
├─ THIRD_PARTY_NOTICES.md
├─ .gitattributes
├─ .gitignore
├─ pyproject.toml
├─ MANIFEST.in
├─ requirements.txt
├─ setup.py
├─ diepi/
│  ├─ __init__.py
│  ├─ __main__.py
│  ├─ cli.py
│  ├─ integration.py
│  ├─ runtime.py
│  ├─ artifacts/
│  ├─ backtest/
│  ├─ commands/
│  ├─ demo/
│  └─ futures/
├─ tests/
│  ├─ backtest/
│  └─ futures/
├─ examples/
├─ docs/
│  ├─ product/
│  └─ development/          # 只放批准公开的维护者材料
├─ tools/
└─ .github/                 # 已纳入白名单的公开 CI 工作流
```

以下内容不进入发布 commit 或工件：

```text
.git/
.release-gate/
build/
dist/
*.egg-info/
.pytest_cache/
.pytest_tmp*/
diepi_results/
parity_runs_v2/
docs/audit/                 # 始终不进入；批准的脱敏改写另存 docs/development/
本地行情、私有策略、真实交易记录和临时日志
```

## 当前命名约定

- 展示品牌：`dieΠ`，中文读作“带派”。
- ASCII 发行名与命令：`diepi`。
- 公开 Python API：`diepi.backtest`、`diepi.futures`、`diepi.artifacts`；编排 adapter 使用
  `diepi.integration` 的版本化 capability 合同；不发布同名冲突风险较高的旧顶层兼容包。

## 发布工具、依赖锁与工件哈希

- CI 和发布工作流首先从 `tools/bootstrap_pip_requirements.txt` 安装固定为 26.2.1 的
  平台无关 pip wheel，并校验该 wheel 的 SHA-256；这一步必须发生在安装其他第三方包之前。
- `tools/release_tool_constraints.txt` 精确约束 pip、setuptools、build、Twine 和 pip-audit。
  这些工具当前均提供覆盖项目 Python 下限的跨平台纯 Python wheel。
- pandas、NumPy、PyArrow、PySide6 等运行时 wheel 与操作系统和 Python 版本有关，不能用一份
  只含少量哈希的文件冒充跨平台完整 lock。项目保留公开元数据中的兼容区间，在 CI 矩阵中
  解析实际 wheel、执行 `pip check`，并审计完整已解析环境。
- `tools/build_release.py` 要求当前发布工具与精确约束一致，执行 `twine check`，并为 wheel 与
  sdist 写出 `SHA256SUMS`。tag 工作流还对 wheel/sdist 生成 GitHub artifact attestation。
- 更新工具版本时，必须在一次独立审查中同时更新约束、pip bootstrap 哈希、漏洞审计结果和
  Action 的官方完整 commit SHA；不能只改显示版本注释。

## 真实示例行情的证据包（不进入公共仓库）

真实切片体积小并不会自动取得再分发权。`0.1.1` 发布前至少留存以下材料，保存于非公开审计
位置：

1. 每一种源数据的提供方全名、官方页面或 API 端点、取得日期，以及取得当日适用的许可/
   服务条款快照（可验证的 PDF、网页存档或截图）。
2. 取得数据时使用的账号类型或公开访问条件、具体证券代码、日期区间、频率和字段；保留可
   重放的抽取命令或脚本版本，但不得记录 token。
3. 原始输入文件的 SHA-256、切片/字段映射/复权处理说明，以及公开的 22 个 Parquet 文件与
   manifest 的 SHA-256 对照，从源输入到发布字节应可追溯。
4. 条款中允许再分发该切片的原文位置，或提供方的书面许可。若条款仅允许个人研究、禁止
   再发布或含义不明，则在得到许可前必须用合成数据替换，不能只依赖“公开可获得”的判断。
5. 项目所有者签署的审阅日期和结论，明确对应本次候选的文件哈希；数据变化后结论失效。

## 最终授权记录

下列各项在实际完成前必须保持未勾选：

- [x] 当前源码的内容与目录终审通过（以 `tools/public_git_allowlist.txt` 的精确文件集为准）
- [x] 当前源码构建出的 Python 公共命名空间与隔离安装验证通过
- [x] GitHub 仓库和 PyPI `diepi` 项目已建立；`0.1.0` 已于 2026-08-18 公开。
- [ ] 许可证、真实示例行情再分发证据与第三方依赖审查通过（源码权属确认见
  `docs/development/source-ownership.md`；依赖披露见 `THIRD_PARTY_NOTICES.md`）
- [x] 原始 Git 历史和私有材料确定不在公共候选中
- [x] 当前源码的全量数据无关测试、依赖审计与发布门禁通过
- [ ] 全新无 `.git` 候选目录复核通过
- [ ] 公共仓库的准确 URL 已写入元数据；私密漏洞报告入口仍需维护者账号收发验证
- [ ] PyPI Trusted Publisher、`pypi` environment 审批和发布变量门禁已验证
- [x] 项目所有者明确授权提交并推送 `0.1.1` 变更
- [x] 项目所有者明确授权创建 `v0.1.1` release 或上传包

推送和发布仍是彼此独立的外部动作；本地复核完成不会自动勾选或授权任何一项。
