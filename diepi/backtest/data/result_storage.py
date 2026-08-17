"""
回测结果存储模块

保存/加载/管理回测记录
"""

import os
import json
import hashlib
import math
import shutil
import stat
import tempfile
from datetime import datetime
from typing import List, Dict, Tuple, Optional
import pandas as pd

from ..engine.portfolio_engine import PortfolioResult
from ..config import RESULTS_DIR as CONFIG_RESULTS_DIR
from ..comparison import ComparisonBundle
from ..broker.target_execution import TargetExecutionBundle
from ..broker.replay import (
    CashAuditBundle,
    CashReplaySeed,
    cash_replay_trade_records,
)
from ..broker.events import ExecutionEventJournal
from ..result_contract import ResultContract, ResultStatus


def _unique_json_object(pairs):
    """Reject duplicate keys while retaining ordinary JSON value semantics."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


class ResultStorage:
    """
    回测结果存储管理器

    保存目录结构:
    diepi_results/
    └── 20251211_153000/
        ├── meta.json           # 元信息
        ├── config.json         # 回测配置
        ├── strategy.py         # 策略代码
        ├── 回测报告.html        # 可视化报告
        ├── 每日净值.csv
        ├── 交易记录.csv
        └── 持仓记录.csv
    """

    RESULTS_DIR = CONFIG_RESULTS_DIR
    VERSION = "1.2"
    LEGACY_UNCLASSIFIED = "LEGACY_UNCLASSIFIED"
    CASH_AUDIT_ARTIFACT_SCHEMA = "diepi.cash_audit_artifacts"
    CASH_AUDIT_ARTIFACT_SCHEMA_VERSION = 1
    CASH_REPLAY_SEED_FILE = "cash_replay_seed.json"
    EXECUTION_EVENT_JOURNAL_FILE = "execution_event_journal.json"
    STAGING_PREFIX = ".diepi-staging-"

    @staticmethod
    def _exact_object_keys(value, expected, label):
        if type(value) is not dict:
            raise TypeError(f"{label} must be exactly object")
        expected = set(expected)
        actual = set(value)
        if actual != expected:
            missing = sorted(expected - actual)
            unknown = sorted(actual - expected)
            raise ValueError(
                f"{label} keys mismatch: missing={missing}, unknown={unknown}"
            )
        return value

    @classmethod
    def _artifact_descriptor(cls, path: str, payload: bytes) -> dict:
        return {
            "byte_length": len(payload),
            "path": path,
            "sha256": hashlib.sha256(payload).hexdigest(),
        }

    @classmethod
    def _validate_artifact_descriptor(
        cls,
        descriptor,
        *,
        expected_path: str,
        label: str,
    ) -> dict:
        payload = cls._exact_object_keys(
            descriptor, ("byte_length", "path", "sha256"), label
        )
        if payload["path"] != expected_path:
            raise ValueError(
                f"{label} path must be exactly {expected_path!r}"
            )
        byte_length = payload["byte_length"]
        if type(byte_length) is not int or byte_length < 0:
            raise TypeError(
                f"{label} byte_length must be a non-negative int"
            )
        digest = payload["sha256"]
        if (
            type(digest) is not str
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise ValueError(
                f"{label} sha256 must be canonical lowercase hex"
            )
        return payload

    @classmethod
    def _validate_cash_audit_descriptor(cls, meta: dict):
        """Validate the small manifest without reading replay artifacts."""

        if type(meta) is not dict:
            raise TypeError("meta.json root must be exactly object")
        descriptor = meta.get("cash_audit")
        if descriptor is None:
            return None
        payload = cls._exact_object_keys(
            descriptor,
            ("journal", "schema", "schema_version", "seed"),
            "cash_audit descriptor",
        )
        if payload["schema"] != cls.CASH_AUDIT_ARTIFACT_SCHEMA:
            raise ValueError("cash_audit artifact schema mismatch")
        if (
            type(payload["schema_version"]) is not int
            or payload["schema_version"]
            != cls.CASH_AUDIT_ARTIFACT_SCHEMA_VERSION
        ):
            raise ValueError("cash_audit artifact schema_version mismatch")
        cls._validate_artifact_descriptor(
            payload["seed"],
            expected_path=cls.CASH_REPLAY_SEED_FILE,
            label="cash replay seed",
        )
        cls._validate_artifact_descriptor(
            payload["journal"],
            expected_path=cls.EXECUTION_EVENT_JOURNAL_FILE,
            label="execution event journal",
        )
        return payload

    @classmethod
    def _validate_current_result(cls, result: PortfolioResult) -> None:
        if type(result) is not PortfolioResult:
            raise TypeError("result must be exactly PortfolioResult")
        result._validate_target_execution()
        result._validate_cash_audit()

    @classmethod
    def _cleanup_staging_directory(cls, staging_path: str, root: str) -> None:
        """Remove only the staging entry owned by the current save."""

        root = os.path.abspath(root)
        staging_path = os.path.abspath(staging_path)
        if (
            os.path.dirname(staging_path) != root
            or not os.path.basename(staging_path).startswith(
                cls.STAGING_PREFIX
            )
        ):
            raise RuntimeError("refusing to clean an unowned staging path")
        try:
            mode = os.lstat(staging_path).st_mode
        except FileNotFoundError:
            return
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            os.unlink(staging_path)
            return
        shutil.rmtree(staging_path)

    @classmethod
    def _save_cash_audit_artifacts(
        cls, folder_path: str, result: PortfolioResult
    ):
        bundle = getattr(result, "cash_audit", None)
        if bundle is None:
            return None
        if type(bundle) is not CashAuditBundle:
            raise TypeError(
                "result.cash_audit must be exactly CashAuditBundle or None"
            )
        # Revalidate every result-to-replay binding at the archive boundary.
        cls._validate_current_result(result)
        seed_payload = bundle.seed.to_json().encode("utf-8")
        journal_payload = bundle.journal_json.encode("utf-8")
        artifacts = (
            (cls.CASH_REPLAY_SEED_FILE, seed_payload),
            (cls.EXECUTION_EVENT_JOURNAL_FILE, journal_payload),
        )
        for relative_path, payload in artifacts:
            with open(
                os.path.join(folder_path, relative_path), "wb"
            ) as artifact_file:
                artifact_file.write(payload)
        return {
            "journal": cls._artifact_descriptor(
                cls.EXECUTION_EVENT_JOURNAL_FILE, journal_payload
            ),
            "schema": cls.CASH_AUDIT_ARTIFACT_SCHEMA,
            "schema_version": cls.CASH_AUDIT_ARTIFACT_SCHEMA_VERSION,
            "seed": cls._artifact_descriptor(
                cls.CASH_REPLAY_SEED_FILE, seed_payload
            ),
        }

    @classmethod
    def _load_cash_audit_artifact(
        cls,
        folder_path: str,
        descriptor,
        *,
        expected_path: str,
        label: str,
    ) -> str:
        payload = cls._validate_artifact_descriptor(
            descriptor,
            expected_path=expected_path,
            label=label,
        )
        byte_length = payload["byte_length"]
        digest = payload["sha256"]

        root = os.path.realpath(folder_path)
        candidate_path = os.path.abspath(os.path.join(root, expected_path))
        try:
            before = os.lstat(candidate_path)
        except FileNotFoundError:
            raise ValueError(f"{label} artifact is missing") from None
        if stat.S_ISLNK(before.st_mode):
            raise ValueError(f"{label} artifact must not be a symlink")
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"{label} artifact must be a regular file")

        artifact_path = os.path.realpath(candidate_path)
        try:
            within_root = os.path.normcase(
                os.path.commonpath((root, artifact_path))
            ) == os.path.normcase(root)
        except ValueError:
            within_root = False
        if (
            not within_root
            or os.path.normcase(os.path.dirname(artifact_path))
            != os.path.normcase(root)
            or os.path.normcase(artifact_path)
            != os.path.normcase(candidate_path)
        ):
            raise ValueError(f"{label} path escapes the result folder")
        flags = os.O_RDONLY
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            file_descriptor = os.open(candidate_path, flags)
        except OSError as exc:
            raise ValueError(
                f"{label} artifact cannot be opened safely"
            ) from exc
        try:
            opened = os.fstat(file_descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError(f"{label} artifact must be a regular file")
            if (before.st_dev, before.st_ino) != (
                opened.st_dev,
                opened.st_ino,
            ):
                raise ValueError(f"{label} artifact changed while opening")
            with os.fdopen(file_descriptor, "rb") as artifact_file:
                file_descriptor = None
                raw = artifact_file.read()
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
        if len(raw) != byte_length:
            raise ValueError(f"{label} byte_length mismatch")
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError(f"{label} sha256 mismatch")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError(f"{label} must be strict UTF-8") from None

    @classmethod
    def _deserialize_cash_audit(cls, meta: dict, folder_path: str):
        payload = cls._validate_cash_audit_descriptor(meta)
        if payload is None:
            # Legacy archives and deliberately hand-built legacy results.
            return None
        seed_json = cls._load_cash_audit_artifact(
            folder_path,
            payload["seed"],
            expected_path=cls.CASH_REPLAY_SEED_FILE,
            label="cash replay seed",
        )
        journal_json = cls._load_cash_audit_artifact(
            folder_path,
            payload["journal"],
            expected_path=cls.EXECUTION_EVENT_JOURNAL_FILE,
            label="execution event journal",
        )
        seed = CashReplaySeed.from_json(seed_json)
        # Parse independently before joining so a damaged journal is identified
        # at its own artifact boundary rather than as a generic bundle error.
        ExecutionEventJournal.from_json(journal_json)
        return CashAuditBundle(seed=seed, journal_json=journal_json)

    @classmethod
    def _serialize_result_contract(cls, result: PortfolioResult):
        contract = getattr(result, "result_contract", None)
        if contract is None:
            return None
        if type(contract) is not ResultContract:
            raise TypeError(
                "result.result_contract must be exactly ResultContract or None"
            )
        return contract.to_dict()

    @classmethod
    def _deserialize_result_contract(cls, meta: dict):
        if type(meta) is not dict:
            raise TypeError("meta.json root must be exactly object")
        payload = meta.get("result_contract")
        if payload is None:
            return None
        return ResultContract.from_dict(payload)

    @classmethod
    def _serialize_comparisons(cls, result: PortfolioResult):
        comparisons = getattr(result, "comparisons", None)
        if comparisons is None:
            return None
        if type(comparisons) is not ComparisonBundle:
            raise TypeError(
                "result.comparisons must be exactly ComparisonBundle or None"
            )
        return comparisons.to_dict()

    @classmethod
    def _deserialize_comparisons(cls, meta: dict):
        if type(meta) is not dict:
            raise TypeError("meta.json root must be exactly object")
        payload = meta.get("comparisons")
        if payload is None:
            return None
        return ComparisonBundle.from_dict(payload)

    @classmethod
    def _serialize_target_execution(cls, result: PortfolioResult):
        bundle = getattr(result, "target_execution", None)
        if bundle is None:
            return None
        if type(bundle) is not TargetExecutionBundle:
            raise TypeError(
                "result.target_execution must be exactly "
                "TargetExecutionBundle or None"
            )
        contract = getattr(result, "result_contract", None)
        if (
            type(contract) is ResultContract
            and contract.status is ResultStatus.SUCCESS
            and not bundle.complete
        ):
            raise ValueError(
                "SUCCESS result cannot store incomplete target execution evidence"
            )
        return bundle.to_dict()

    @classmethod
    def _deserialize_target_execution(cls, meta: dict):
        if type(meta) is not dict:
            raise TypeError("meta.json root must be exactly object")
        payload = meta.get("target_execution")
        if payload is None:
            return None
        return TargetExecutionBundle.from_dict(payload)

    @classmethod
    def _validate_stored_reference_total_return_excess(
        cls,
        meta: dict,
        result: PortfolioResult,
    ) -> None:
        # Missing is the legacy-compatible representation.  In current
        # artifacts the value is redundant evidence and must agree exactly
        # with the result contract, actual NAV scope, and comparison leg.
        if "reference_total_return_excess" not in meta:
            return
        stored = meta["reference_total_return_excess"]
        expected = result.reference_total_return_excess
        if stored is None:
            if expected is not None:
                raise ValueError(
                    "stored reference_total_return_excess is null but the "
                    "comparison is numerically eligible"
                )
            return
        if type(stored) not in (int, float):
            raise TypeError(
                "reference_total_return_excess must be a number or null"
            )
        number = float(stored)
        if not math.isfinite(number):
            raise ValueError(
                "reference_total_return_excess must be finite"
            )
        if expected is None or number != expected:
            raise ValueError(
                "stored reference_total_return_excess does not agree with "
                "the result contract and exact comparison scope"
            )

    @classmethod
    def _load_meta(cls, meta_path: str) -> dict:
        return cls._load_json_object(meta_path, "meta.json")

    @classmethod
    def _load_json_object(cls, path: str, label: str) -> dict:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(
                f,
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
        if type(payload) is not dict:
            raise TypeError(f"{label} root must be exactly object")
        return payload

    @classmethod
    def save(cls, result: PortfolioResult, config: dict, code: str) -> str:
        """
        保存回测结果

        Args:
            result: PortfolioResult 对象
            config: 回测配置字典
            code: 策略代码字符串

        Returns:
            保存的文件夹路径
        """
        # 创建时间戳文件夹
        cls._validate_current_result(result)
        root = os.path.realpath(cls.RESULTS_DIR)
        os.makedirs(root, exist_ok=True)
        staging_path = tempfile.mkdtemp(
            prefix=cls.STAGING_PREFIX,
            dir=root,
        )
        staging_token = os.path.basename(staging_path)[
            len(cls.STAGING_PREFIX):
        ]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        final_path = os.path.join(root, f"{timestamp}_{staging_token}")

        try:
            cash_audit_descriptor = cls._save_cash_audit_artifacts(
                staging_path, result
            )
            # 1. 保存 meta.json
            meta = {
                "save_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "version": cls.VERSION,
                "mode": "portfolio",
                "start_date": result.start_date,
                "end_date": result.end_date,
                "initial_cash": result.initial_cash,
                "final_value": result.final_value,
                "total_return": result.total_return,
                "annual_return": result.annual_return,
                "max_drawdown": result.max_drawdown,
                "max_drawdown_close_nav": (
                    result.max_drawdown_close_nav
                ),
                "max_drawdown_intraday_low_nav": (
                    result.max_drawdown_intraday_low_nav
                ),
                "max_drawdown_intraday_high_to_low": (
                    result.max_drawdown_intraday_high_to_low
                ),
                "sharpe_ratio": result.sharpe_ratio,
                "trade_count": result.trade_count,
                "win_rate": result.win_rate,
                "benchmark_code": result.benchmark_code,
                "benchmark_return": result.benchmark_return,
                "excess_return": result.excess_return,
                "result_contract": cls._serialize_result_contract(result),
                "comparisons": cls._serialize_comparisons(result),
                "target_execution": cls._serialize_target_execution(result),
                "cash_audit": cash_audit_descriptor,
                "reference_total_return_excess": (
                    result.reference_total_return_excess
                ),
            }
            with open(os.path.join(staging_path, "meta.json"), "w", encoding="utf-8") as f:
                json.dump(
                    meta, f, ensure_ascii=False, indent=2,
                    allow_nan=False,
                )

            # 2. 保存 config.json
            with open(os.path.join(staging_path, "config.json"), "w", encoding="utf-8") as f:
                json.dump(
                    config, f, ensure_ascii=False, indent=2,
                    allow_nan=False,
                )

            # 3. 保存 strategy.py
            with open(os.path.join(staging_path, "strategy.py"), "w", encoding="utf-8") as f:
                f.write(code)

            # 4. 保存每日净值 CSV
            cls._save_daily_values_csv(staging_path, result.daily_values)

            # 5. 保存交易记录 CSV
            cls._save_trades_csv(staging_path, result.trades)

            # 6. 保存持仓记录 CSV
            cls._save_positions_csv(staging_path, result.position_history)

            # 7. 生成 HTML 报告
            from .report_generator import ReportGenerator
            ReportGenerator.generate(staging_path, result, config, code)

            if os.path.lexists(final_path):
                raise FileExistsError(
                    f"result archive target already exists: {final_path}"
                )
            os.rename(staging_path, final_path)
            staging_path = None
            return final_path

        except Exception:
            # 保存失败，清理文件夹
            if staging_path is not None:
                cls._cleanup_staging_directory(staging_path, root)
            raise

    @classmethod
    def _save_daily_values_csv(cls, folder_path: str, daily_values: pd.DataFrame):
        """保存每日净值 CSV"""
        if daily_values is None or daily_values.empty:
            return

        df = daily_values.copy()

        # 确保有日期列
        if df.index.name == 'date' or isinstance(df.index, pd.DatetimeIndex):
            df = df.reset_index()
            df.rename(columns={'index': '日期', 'date': '日期'}, inplace=True)

        # 日收益率由 MetricEngine 相对期初净值计算。这里只消费引擎
        # 的规范列，不能用 pct_change() 重算（那会把首日收益抹成 0）。
        if 'daily_return' in df.columns:
            df['日收益率'] = df['daily_return'].apply(
                lambda x: "" if pd.isna(x) else f"{float(x)*100:.2f}%"
            )

        # 重命名列
        rename_map = {
            'cash': '现金',
            'market_value': '市值',
            'total_value': '总资产',
        }
        df.rename(columns=rename_map, inplace=True)

        # 格式化日期
        if '日期' in df.columns:
            df['日期'] = pd.to_datetime(df['日期']).dt.strftime('%Y-%m-%d')

        # 选择列顺序
        metric_cols = [
            'daily_return',
            'drawdown_close_nav',
            'drawdown_intraday_low_nav',
            'drawdown_intraday_high_to_low',
        ]
        cols = [
            '日期', '现金', '市值', '总资产',
            *metric_cols,
            '日收益率',
        ]
        cols = [c for c in cols if c in df.columns]
        df = df[cols]

        df.to_csv(os.path.join(folder_path, "每日净值.csv"), index=False, encoding="utf-8-sig")

    @classmethod
    def _save_trades_csv(cls, folder_path: str, trades: List[Dict]):
        """保存交易记录 CSV (优化版)"""
        if not trades:
            # 创建空文件
            pd.DataFrame(columns=['日期', '股票代码', '股票名称', '方向', '数量', '价格', '金额', '盈亏', '累计盈亏']).to_csv(
                os.path.join(folder_path, "交易记录.csv"), index=False, encoding="utf-8-sig"
            )
            return

        df = pd.DataFrame(trades)
        
        # 1. 批量处理日期
        if 'time' in df.columns:
            # YYYYMMDD -> YYYY-MM-DD
            df['日期'] = pd.to_datetime(df['time'], format='%Y%m%d', errors='coerce').dt.strftime('%Y-%m-%d')
        else:
            df['日期'] = ''

        # 2. 批量获取股票名称
        if 'symbol' in df.columns:
            unique_symbols = df['symbol'].unique()
            name_map = cls._batch_get_stock_names(unique_symbols)
            df['股票名称'] = df['symbol'].map(name_map).fillna('')
        else:
            df['股票名称'] = ''

        # 3. 映射方向
        if 'direction' in df.columns:
            df['方向'] = df['direction'].map({'BUY': '买入', 'SELL': '卖出'}).fillna(df['direction'])
        
        # 4. 计算盈亏
        if 'profit' not in df.columns:
            df['profit'] = 0.0
        
        # 累计盈亏
        df['累计盈亏'] = df['profit'].cumsum()

        # 格式化数值列
        df['价格'] = df['price'].apply(lambda x: f"{x:.2f}")
        df['金额'] = df['amount'].apply(lambda x: f"{x:.2f}")
        
        # 盈亏仅在非零时显示（或者是卖出时）
        # 为了保持 Pandas 效率，这里直接保留数值，保存时再格式化可能更快，但为了一致性，使用 apply
        df['盈亏'] = df['profit'].apply(lambda x: f"{x:.2f}" if x != 0 else "")
        df['累计盈亏'] = df['累计盈亏'].apply(lambda x: f"{x:.2f}")

        # 重命名和排序
        df.rename(columns={'symbol': '股票代码', 'shares': '数量'}, inplace=True)
        
        cols = ['日期', '股票代码', '股票名称', '方向', '数量', '价格', '金额', '盈亏', '累计盈亏']
        # 确保列存在
        for c in cols:
            if c not in df.columns:
                df[c] = ''
                
        df = df[cols]
        df.to_csv(os.path.join(folder_path, "交易记录.csv"), index=False, encoding="utf-8-sig")

    @classmethod
    def _save_positions_csv(cls, folder_path: str, positions: List[Dict]):
        """保存持仓记录 CSV (优化版)"""
        if not positions:
            pd.DataFrame(columns=['日期', '股票代码', '股票名称', '持仓', '成本', '现价', '市值', '盈亏', '盈亏%']).to_csv(
                os.path.join(folder_path, "持仓记录.csv"), index=False, encoding="utf-8-sig"
            )
            return

        df = pd.DataFrame(positions)
        
        # 1. 批量处理日期
        if 'date' in df.columns:
             df['日期'] = pd.to_datetime(df['date'], format='%Y%m%d', errors='coerce').dt.strftime('%Y-%m-%d')
        else:
            df['日期'] = ''

        # 2. 批量获取股票名称
        if 'symbol' in df.columns:
            unique_symbols = df['symbol'].unique()
            name_map = cls._batch_get_stock_names(unique_symbols)
            df['股票名称'] = df['symbol'].map(name_map).fillna('')
        else:
            df['股票名称'] = ''
            
        # 3. 计算盈亏比率
        # profit_rate 有可能是0，需要检查
        if 'profit_rate' not in df.columns:
            df['profit_rate'] = 0.0
            
        # 如果 profit_rate 为0 但有成本，尝试计算
        mask = (df['profit_rate'] == 0) & (df['cost'] > 0)
        if mask.any():
            df.loc[mask, 'profit_rate'] = (df.loc[mask, 'price'] - df.loc[mask, 'cost']) / df.loc[mask, 'cost']

        # 格式化数值
        df['成本'] = df['cost'].apply(lambda x: f"{x:.2f}")
        df['现价'] = df['price'].apply(lambda x: f"{x:.2f}")
        
        if 'market_value' in df.columns:
            df['市值'] = df['market_value'].apply(lambda x: f"{x:.2f}")
        else:
            df['市值'] = '0.00'
            
        if 'profit' in df.columns:
            df['盈亏'] = df['profit'].apply(lambda x: f"{x:.2f}")
        else:
            df['盈亏'] = '0.00'
            
        df['盈亏%'] = df['profit_rate'].apply(lambda x: f"{x*100:.2f}%")

        # 重命名和选择
        df.rename(columns={'symbol': '股票代码', 'shares': '持仓'}, inplace=True)
        
        cols = ['日期', '股票代码', '股票名称', '持仓', '成本', '现价', '市值', '盈亏', '盈亏%']
        for c in cols:
            if c not in df.columns:
                df[c] = ''
                
        df = df[cols]
        df.to_csv(os.path.join(folder_path, "持仓记录.csv"), index=False, encoding="utf-8-sig")

    @classmethod
    def _batch_get_stock_names(cls, symbols: List[str]) -> Dict[str, str]:
        """批量获取股票名称"""
        name_map = {}
        try:
            from ..data import DataProvider
            provider = DataProvider() # 假设 DataProvider 初始化开销不大，或者内部有缓存
            # 如果 DataProvider 支持 batch get 最好，否则只能在这里循环
            # 但至少这里循环次数 = 股票数 << 记录数
            for s in symbols:
                info = provider.get_stock_info(s)
                if info and 'name' in info:
                    name_map[s] = info['name']
        except Exception:
            pass
        return name_map

    @classmethod
    def _get_stock_name(cls, symbol: str) -> str:
        """获取股票名称"""
        try:
            from ..data import DataProvider
            provider = DataProvider()
            info = provider.get_stock_info(symbol)
            if info and 'name' in info:
                return info['name']
        except:
            pass
        return ''

    @classmethod
    def load(cls, folder_path: str) -> Tuple[PortfolioResult, dict, str]:
        """
        加载回测记录

        Args:
            folder_path: 记录文件夹路径

        Returns:
            (PortfolioResult, config, code) 元组
        """
        # 1. 读取 meta.json
        meta = cls._load_meta(os.path.join(folder_path, "meta.json"))
        result_contract = cls._deserialize_result_contract(meta)
        comparisons = cls._deserialize_comparisons(meta)
        target_execution = cls._deserialize_target_execution(meta)
        cash_audit = cls._deserialize_cash_audit(meta, folder_path)

        # 2. 读取 config.json
        config = cls._load_json_object(
            os.path.join(folder_path, "config.json"), "config.json"
        )

        # 3. 读取 strategy.py
        with open(os.path.join(folder_path, "strategy.py"), "r", encoding="utf-8") as f:
            code = f.read()

        # 4. 读取每日净值
        daily_values = cls._load_daily_values_csv(folder_path)

        # 5. 读取交易记录
        trades = (
            cash_replay_trade_records(cash_audit)
            if cash_audit is not None
            else cls._load_trades_csv(folder_path)
        )

        # 6. 读取持仓记录
        positions = cls._load_positions_csv(folder_path)

        # 7. 重建 PortfolioResult
        result = PortfolioResult(
            start_date=meta.get('start_date', ''),
            end_date=meta.get('end_date', ''),
            initial_cash=meta.get('initial_cash', 0),
            final_value=meta.get('final_value', 0),
            total_return=meta.get('total_return', 0),
            annual_return=meta.get('annual_return', 0),
            max_drawdown=meta.get('max_drawdown', 0),
            max_drawdown_close_nav=meta.get(
                'max_drawdown_close_nav', meta.get('max_drawdown', 0)
            ),
            max_drawdown_intraday_low_nav=meta.get(
                'max_drawdown_intraday_low_nav', 0
            ),
            max_drawdown_intraday_high_to_low=meta.get(
                'max_drawdown_intraday_high_to_low'
            ),
            trade_count=meta.get('trade_count', 0),
            win_rate=meta.get('win_rate'),
            sharpe_ratio=meta.get('sharpe_ratio', 0),
            benchmark_code=meta.get('benchmark_code', ''),
            benchmark_return=meta.get('benchmark_return', 0),
            excess_return=meta.get('excess_return', 0),
            daily_values=daily_values,
            trades=trades,
            position_history=positions,
            result_contract=result_contract,
            comparisons=comparisons,
            target_execution=target_execution,
            cash_audit=cash_audit,
        )
        cls._validate_stored_reference_total_return_excess(meta, result)

        return result, config, code

    @classmethod
    def _load_daily_values_csv(cls, folder_path: str) -> pd.DataFrame:
        """加载每日净值 CSV"""
        csv_path = os.path.join(folder_path, "每日净值.csv")
        if not os.path.exists(csv_path):
            return pd.DataFrame()

        df = pd.read_csv(csv_path, encoding="utf-8-sig")

        # 反向重命名
        rename_map = {
            '日期': 'date',
            '现金': 'cash',
            '市值': 'market_value',
            '总资产': 'total_value',
        }
        df.rename(columns=rename_map, inplace=True)

        # 设置日期索引
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'])
            df.set_index('date', inplace=True)

        # 中文列仅供人工阅读；规范的原始比例列已随 CSV 一起保存。
        if '日收益率' in df.columns:
            df.drop(columns=['日收益率'], inplace=True)

        return df

    @classmethod
    def _load_trades_csv(cls, folder_path: str) -> List[Dict]:
        """加载交易记录 CSV"""
        csv_path = os.path.join(folder_path, "交易记录.csv")
        if not os.path.exists(csv_path):
            return []

        df = pd.read_csv(csv_path, encoding="utf-8-sig")
        if df.empty:
            return []

        if df.empty:
            return []

        # 向量化处理
        # 1. 映射字段名
        df.rename(columns={
            '日期': 'time',
            '股票代码': 'symbol',
            '方向': 'direction',
            '数量': 'shares',
            '价格': 'price',
            '金额': 'amount',
            '盈亏': 'profit'
        }, inplace=True)

        # 2. 清洗日期 (2024-01-01 -> 20240101)
        if 'time' in df.columns:
            df['time'] = df['time'].astype(str).str.replace('-', '')

        # 3. 映射方向
        if 'direction' in df.columns:
            df['direction'] = df['direction'].map({'买入': 'BUY', '卖出': 'SELL'}).fillna('BUY')

        # 4. 数值转换 (移除逗号并转float)
        numeric_cols = ['shares', 'price', 'amount', 'profit']
        for col in numeric_cols:
            if col in df.columns:
                df[col] = df[col].astype(str).str.replace(',', '', regex=False)
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
        
        # 转换 shares 为 int
        if 'shares' in df.columns:
             df['shares'] = df['shares'].astype(int)

        # 5. 转为 Dict List
        # 仅保留需要的列
        cols = ['time', 'symbol', 'direction', 'shares', 'price', 'amount', 'profit']
        cols = [c for c in cols if c in df.columns]
        
        return df[cols].to_dict('records')

    @classmethod
    def _load_positions_csv(cls, folder_path: str) -> List[Dict]:
        """加载持仓记录 CSV"""
        csv_path = os.path.join(folder_path, "持仓记录.csv")
        if not os.path.exists(csv_path):
            return []

        df = pd.read_csv(csv_path, encoding="utf-8-sig")
        if df.empty:
            return []

        if df.empty:
            return []
            
        # 向量化处理
        # 1. 映射字段名
        df.rename(columns={
            '日期': 'date',
            '股票代码': 'symbol',
            '持仓': 'shares',
            '成本': 'cost',
            '现价': 'price',
            '市值': 'market_value',
            '盈亏': 'profit',
            '盈亏%': 'profit_rate'
        }, inplace=True)

        # 2. 清洗日期
        if 'date' in df.columns:
            df['date'] = df['date'].astype(str).str.replace('-', '')

        # 3. 处理百分比 (盈亏%)
        if 'profit_rate' in df.columns:
            df['profit_rate'] = df['profit_rate'].astype(str).str.replace('%', '', regex=False)
            df['profit_rate'] = pd.to_numeric(df['profit_rate'], errors='coerce').fillna(0) / 100

        # 4. 数值转换
        numeric_cols = ['shares', 'cost', 'price', 'market_value', 'profit']
        for col in numeric_cols:
             if col in df.columns:
                df[col] = df[col].astype(str).str.replace(',', '', regex=False)
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

        # 转换 shares 为 int
        if 'shares' in df.columns:
             df['shares'] = df['shares'].astype(int)

        # 5. 转为 Dict List
        cols = ['date', 'symbol', 'shares', 'cost', 'price', 'market_value', 'profit', 'profit_rate']
        cols = [c for c in cols if c in df.columns]

        return df[cols].to_dict('records')

    @classmethod
    def list_records(cls) -> List[Dict]:
        """
        列出所有保存的记录

        Returns:
            记录列表，每个记录包含 {path, save_time, total_return, start_date, end_date, ...}
        """
        records = []

        if not os.path.exists(cls.RESULTS_DIR):
            return records

        for name in os.listdir(cls.RESULTS_DIR):
            folder_path = os.path.join(cls.RESULTS_DIR, name)
            if not os.path.isdir(folder_path):
                continue

            meta_path = os.path.join(folder_path, "meta.json")
            if not os.path.exists(meta_path):
                continue

            try:
                meta = cls._load_meta(meta_path)
                result_contract = cls._deserialize_result_contract(meta)
                cls._deserialize_comparisons(meta)
                cls._deserialize_target_execution(meta)
                cls._validate_cash_audit_descriptor(meta)

                records.append({
                    'path': folder_path,
                    'folder_name': name,
                    'save_time': meta.get('save_time', ''),
                    'start_date': meta.get('start_date', ''),
                    'end_date': meta.get('end_date', ''),
                    'total_return': meta.get('total_return', 0),
                    'annual_return': meta.get('annual_return', 0),
                    'max_drawdown': meta.get('max_drawdown', 0),
                    'sharpe_ratio': meta.get('sharpe_ratio', 0),
                    'trade_count': meta.get('trade_count', 0),
                    'initial_cash': meta.get('initial_cash', 0),
                    'final_value': meta.get('final_value', 0),
                    'result_status': (
                        result_contract.status.value
                        if result_contract is not None
                        else cls.LEGACY_UNCLASSIFIED
                    ),
                    'rankable': (
                        result_contract.is_rankable
                        if result_contract is not None
                        else False
                    ),
                })
            except Exception:
                continue

        # 按保存时间倒序排序
        records.sort(key=lambda x: x['save_time'], reverse=True)

        return records

    @classmethod
    def delete(cls, folder_path: str) -> bool:
        """
        删除记录

        Args:
            folder_path: 记录文件夹路径

        Returns:
            是否删除成功
        """
        try:
            root = os.path.realpath(os.path.abspath(cls.RESULTS_DIR))
            candidate = os.path.abspath(os.fsdecode(os.fspath(folder_path)))
            entry = os.lstat(candidate)

            is_junction = getattr(os.path, 'isjunction', None)
            if (
                stat.S_ISLNK(entry.st_mode)
                or not stat.S_ISDIR(entry.st_mode)
                or (is_junction is not None and is_junction(candidate))
            ):
                return False

            target = os.path.realpath(candidate)
            if (
                os.path.normcase(os.path.realpath(os.path.dirname(candidate)))
                != os.path.normcase(root)
                or os.path.normcase(os.path.dirname(target))
                != os.path.normcase(root)
            ):
                return False

            # A direct child is deletable only when it is a published result,
            # not an arbitrary directory that happens to share RESULTS_DIR.
            meta_path = os.path.join(target, 'meta.json')
            meta_entry = os.lstat(meta_path)
            if stat.S_ISLNK(meta_entry.st_mode) or not stat.S_ISREG(
                meta_entry.st_mode
            ):
                return False

            # Recheck the target identity immediately before the destructive
            # operation. shutil.rmtree also refuses a top-level symlink.
            verified = os.lstat(target)
            if (
                stat.S_ISLNK(verified.st_mode)
                or not stat.S_ISDIR(verified.st_mode)
                or (entry.st_dev, entry.st_ino)
                != (verified.st_dev, verified.st_ino)
            ):
                return False

            shutil.rmtree(target)
            return True
        except Exception:
            return False

    @classmethod
    def ensure_results_dir(cls):
        """确保结果目录存在"""
        os.makedirs(cls.RESULTS_DIR, exist_ok=True)
