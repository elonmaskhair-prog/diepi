# dieΠ 首次公开发布清单

本文记录首次公开前的本地流程，不代表已经创建公共仓库、Git 提交、远端、release 或
PyPI 发布。当前源码在上一份候选导出后又接受了发布安全修订，因此旧候选不再是最终候选；
必须重新导出、复核和授权。下文所有未勾选项都是实际未完成项，不得根据一次本地测试结果
推定完成。

## 当前发布阻断项

以下事项在仓库或账号实际存在后才能完成，不能在本地源码中伪造：

- [ ] 创建公共仓库后，把准确的仓库、文档和问题跟踪 URL 写入包元数据并重新构建。
- [ ] 启用私密漏洞报告渠道，由维护者账号完成一次不含敏感信息的收发验证，并更新
  `SECURITY.md` 的准确入口。
- [ ] 为 `pypi` GitHub Environment 配置审批规则，并在 PyPI 为准确的 owner、repository、
  workflow 文件 `.github/workflows/release.yml` 和 environment `pypi` 配置 Trusted Publisher。
- [ ] 在发布当日重新确认 `diepi` 的 PyPI 名称状态；“当前查询无项目”不等于预留成功。
- [ ] 留存并由项目所有者确认真实示例行情的可再分发证据包（最低材料见下文）。
- [ ] 重新导出无 `.git` 的候选目录，并对该目录运行完整测试、发布构建和精确目录树检查。

发布工作流默认只构建、校验、生成 SHA-256 清单并上传 GitHub Actions 工件。只有 tag 运行、
PyPI Trusted Publisher 与 `pypi` environment 已配置，而且仓库变量
`DIEPI_PYPI_TRUSTED_PUBLISHING` 被所有者明确设为 `enabled` 时，PyPI job 才会运行。该变量在
上传包未获单独授权前必须保持未设置。

## 不可跨越的边界

- 不把当前工作目录的 `.git/`、commit、branch、reflog、tag 或 remote 复制到公共项目。
- 不从当前工作目录直接执行 `git push`、GitHub 建仓、GitHub release 或包上传。
- 不把“测试通过”“构建成功”解释成发布授权。
- 只有项目所有者明确说“可以首次发布”后，才能开始建立公共历史；创建远端、推送代码和
  上传包仍应作为可分别确认的外部动作。
- 原始审查材料、行情、策略、回测结果、凭据、本机路径和临时构建产物默认不公开。

## 首次公开候选的来源

公共代码必须来自最终审核通过的文件白名单，而不是来自当前 Git 历史。建议流程如下：

1. 冻结待审候选，记录源码、文档、依赖、许可证和构建工件的摘要。
2. 运行数据无关测试、发布门禁、文档链接和隐私扫描。
3. 将批准的文件复制到一个全新的空目录；不复制 `.git/`，也不复制任何被排除项。
4. 在新目录再次检查目录树、敏感文本、测试、sdist、wheel 和安装冒烟。
5. 将该新目录交给项目所有者作最终差异检查。
6. 得到明确首次发布授权后，才在新目录执行 `git init` 并创建单一初始提交。
7. 远端创建、首次 push、tag/release 和 PyPI 上传分别等待明确授权。

## 推荐的首次公共目录

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

以下内容不进入首次公共目录：

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
- 公开 Python API：`diepi.backtest`、`diepi.futures`、`diepi.artifacts`；不发布同名冲突风险
  较高的旧顶层兼容包。

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

真实切片体积小并不会自动取得再分发权。首次发布前至少留存以下材料，保存于非公开审计
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

- [ ] 当前源码的内容与目录终审通过（以 `tools/public_git_allowlist.txt` 的精确文件集为准）
- [ ] 当前源码构建出的 Python 公共命名空间与隔离安装验证通过
- [ ] 项目名称、发行名及目标平台名称可用性确认（PyPI 的 `diepi` 在 2026-08-12
  查询时无项目记录，但这不构成预留；账号/组织下的仓库名称须在创建远端时确认）
- [ ] 许可证、真实示例行情再分发证据与第三方依赖审查通过（源码权属确认见
  `docs/development/source-ownership.md`；依赖披露见 `THIRD_PARTY_NOTICES.md`）
- [x] 原始 Git 历史和私有材料确定不在公共候选中
- [ ] 当前源码的全量数据无关测试、依赖审计与发布门禁通过
- [ ] 全新无 `.git` 候选目录复核通过
- [ ] 公共仓库的准确 URL 已写入元数据，私密漏洞报告入口已验证
- [ ] PyPI Trusted Publisher、`pypi` environment 审批和发布变量门禁已验证
- [ ] 项目所有者明确授权创建首次公共 Git 历史
- [ ] 项目所有者明确授权创建远端并首次推送
- [ ] 项目所有者明确授权创建 release 或上传包

前三个外部动作彼此独立；本地复核完成不会自动勾选或授权其中任何一项。公共仓库创建后，
还需要把真实仓库 URL 写入包元数据和文档，并启用一个实际可用的私密漏洞报告渠道。
