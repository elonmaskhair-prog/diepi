"""CLI-library cancellation bridge without optional GUI dependencies."""

from diepi.artifacts import ArtifactStore
from diepi.backtest.cli.runner import run_backtest
from diepi.backtest.result_contract import ResultStatus
from diepi.demo import generate_synthetic_demo


def test_formal_runner_forwards_stop_check_and_publishes_canceled_artifact(tmp_path):
    demo = generate_synthetic_demo(tmp_path / "cancel-demo")
    output = run_backtest(
        strategy_file=str(demo.strategy_file),
        start_date=demo.manifest.start_date,
        end_date=demo.manifest.end_date,
        initial_cash=1_000_000.0,
        data_root=demo.data_root,
        output_dir=tmp_path / "results",
        run_name="cooperatively-canceled-run",
        pool_symbols=[demo.manifest.symbols[0]],
        daily_open_previous_day_ratio=0.1,
        stop_check=lambda: True,
        verbose=False,
    )

    loaded = ArtifactStore.load(output["artifact_dir"])
    assert output["artifact_verified"] is True
    assert output["rankable"] is False
    assert output["result_contract"]["status"] == "CANCELED"
    assert loaded.artifact_verified is True
    assert loaded.is_rankable is False
    assert loaded.outcome.result_contract.status is ResultStatus.CANCELED
