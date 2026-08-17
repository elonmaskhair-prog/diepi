# 参与 dieΠ

感谢你考虑参与 dieΠ。项目仍处于 Alpha 阶段，正在本地完成首次公开前审阅。与扩展功能
数量相比，正确性、可复现性和明确披露研究假设更重要。

## 开始之前

- 公共仓库建立后，请通过仓库届时公布的贡献渠道讨论改动；首次公开前，只通过当前本地
  审阅已经建立的私密渠道与维护者协调。
- 保持改动聚焦。除非无法独立审阅，不要把行为变更、打包变更和大规模重构混在一起。
- 不要提交行情数据、私有策略、凭据、本机绝对路径或生成的回测结果。
- 需要数据夹具时，优先使用确定性 synthetic 数据，并在名称、manifest 和文档中明确标记
  非真实行情；不要把真实行情“改个名字”后当测试夹具。
- 策略代码是受信任的本地 Python，不是沙箱输入。

## 开发环境

Python 支持范围和依赖分组以 `pyproject.toml` 为准。典型的可编辑安装方式如下：

```bash
python -m pip install --upgrade "setuptools>=77" build
python -m pip install -e ".[dev,gui]"
```

运行数据无关测试时，把 `DATA_ROOT` 指向单独的空目录。标有 `integration` 的测试依赖
显式准备的本地数据仓库，不能悄悄回退到贡献者的个人数据。

## 必做检查

提交改动前运行数据无关测试：

```bash
python tools/run_test_gate.py --junitxml .release-gate/local/unit.xml --min-passed 2000 -- -m "not integration" tests/backtest tests/futures
```

若改动涉及打包元数据、进入源码工件的文档或公开命令入口，还需运行：

```bash
python tools/build_release.py --output-root .release-gate/local-build
```

首次公开候选还应运行静态、覆盖率和依赖漏洞检查：

```bash
python -m ruff check diepi tests tools examples
python tools/check_markdown_links.py
python -m coverage run --source=diepi -m pytest -m "not integration" tests/backtest tests/futures
python -m coverage report --fail-under=70.0
python -m pip_audit --strict .
```

70% 是当前数据无关分支覆盖率的已测基线，用于防止大幅回退，不是正确性证明；核心契约、
工件完整性和发布门禁仍必须分别通过。

若改动涉及首次体验或命令路由，还需至少运行：

```bash
python -m pytest tests/backtest/test_onboarding_services.py tests/backtest/test_onboarding_commands.py -q
```

onboarding 测试必须使用临时目录和 generated synthetic 数据，不应依赖贡献者的真实
`DATA_ROOT`。公开 README 会作为包索引长描述渲染；在真实公共仓库 URL 尚未建立时，使用
纯文字仓库路径，不要虚构链接、邮箱或下载地址。

发布门禁会从显式白名单生成源码快照，检查 sdist 和 wheel，从解包后的 sdist 收集测试，
并隔离安装 wheel 做冒烟验证。通过门禁不能替代首次公开前的干净历史和隐私审阅。
Python 3.10 的最低直接依赖组合记录在 `tools/minimum_core_requirements.txt` 和
`tools/minimum_gui_requirements.txt`；修改依赖下界时必须同步更新并验证这两份文件。

只有具备所需数据且能说明数据来源及覆盖范围时，才运行相关集成测试。请如实报告跳过项
和警告，不要把部分执行称为完整通过。

## 改动要求

- 修复缺陷或改变可执行契约时，增加回归测试。
- 保留事件时序、订单状态、现金和结果契约证据，不能只修最终指标。
- 新增的假设和兼容边界应写入产品文档。
- 示例应小而清晰，不作收益承诺。
- CLI、GUI 和 Python API 对同一能力的成熟度表述必须一致：正式首批范围是自备数据的
  日线现金研究；高级/实验路径需要明确标注。
- 保存或迁移结果时，不得把未验证的 legacy 目录推断成现行、可排名工件。
- 用户可见行为、兼容性或打包发生变化时，更新 `CHANGELOG.md`。

提交贡献即表示你同意该贡献可以按 `LICENSE` 中的许可证分发。
