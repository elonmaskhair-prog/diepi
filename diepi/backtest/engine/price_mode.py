import math


# Daily adjustment-factor feeds can contain a few parts-per-million of drift
# even when no exchange corporate action occurred.  Applying integer-share
# reinvestment to that noise leaks one share at a time through floor rounding.
# Changes below 0.001% are economically below the executable tick for
# ordinary cash equities and are treated as source noise.
ADJUSTMENT_FACTOR_MATERIALITY = 1e-5


class PriceModeMixin:
    def _get_adj_ratio(self, symbol: str) -> float:
        data = getattr(self, "_data", None)
        getter = getattr(data, "get_adj_ratio", None)
        if not callable(getter):
            raise RuntimeError(
                "distinct price spaces require a strict adjustment-factor provider"
            )
        ratio = getter(symbol, getattr(self, "current_date", None))
        if isinstance(ratio, bool):
            raise ValueError("adjustment ratio must be finite and positive")
        try:
            numeric = float(ratio)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(
                "adjustment ratio must be finite and positive"
            ) from None
        if not math.isfinite(numeric) or numeric <= 0.0:
            raise ValueError("adjustment ratio must be finite and positive")
        return numeric

    def set_same_source_symbols(self, symbols, skip_adjust=None) -> None:
        """登记 legacy compatibility-only 的"两腿同源"标的。

        正式 ``DataProvider.get_aligned_pair`` 路径要求策略腿与撮合腿严格成对，
        任一腿缺失都会 fail-fast，不会用此方法静默镜像另一腿。这里仅保留给
        未实现严格成对接口的旧 provider/测试适配器，用于明确选择单轨兼容语义。

        - symbols: 全部同源标的 → 短路挂单价换算（两腿同一价格空间）。
          历史陷阱：daily_raw+adj_factor 但无 daily/ 时，回退让策略看 raw 价，
          而 hfq→raw 换算仍生效，限价被错除、0 成交无警告。
        - skip_adjust: 其中需要禁用除权调股的子集（= 共享的是策略腿/hfq 类
          连续序列的标的——序列自身连续，调股会重复调整）。
          raw 单轨 + 有因子的标的不在此列：raw 撮合+因子调股=真实账户行为，
          必须保留（拆分日股数翻倍、价格减半、净值连续）。"""
        self._same_source_symbols = frozenset(symbols) if symbols else frozenset()
        self._same_source_skip_adjust = (
            frozenset(skip_adjust) if skip_adjust else frozenset())

    def add_same_source_symbol(self, symbol: str, skip_adjust: bool = False) -> None:
        """增量登记单个同源标的（懒加载路径用：预加载时不在池内、
        盘前由策略动态返回的标的，镜像 fallback 后必须同步登记，
        否则限价换算/除权调股仍按双轨口径走——审查确认的懒加载绕过洞）"""
        self._same_source_symbols = (
            getattr(self, '_same_source_symbols', frozenset()) | {symbol})
        if skip_adjust:
            self._same_source_skip_adjust = (
                getattr(self, '_same_source_skip_adjust', frozenset()) | {symbol})

    def _is_same_source(self, symbol: str) -> bool:
        return symbol in getattr(self, '_same_source_symbols', ())

    def _skip_corporate_adjust(self, symbol: str) -> bool:
        return symbol in getattr(self, '_same_source_skip_adjust', ())

    def _convert_price_for_execution(self, symbol: str, price: float):
        if price is None:
            return None
        if (
            getattr(self, "_strategy_price_mode", None)
            == self._execution_price_mode
        ):
            return price
        if self._is_same_source(symbol):
            # 单轨同源：挂单价与撮合价在同一价格空间，禁止换算
            return price
        ratio = self._get_adj_ratio(symbol)
        if self._strategy_price_mode == "hfq" and self._execution_price_mode == "raw":
            return price / ratio
        if self._strategy_price_mode == "raw" and self._execution_price_mode == "hfq":
            return price * ratio
        raise ValueError(
            "unsupported distinct strategy/execution price-space pair"
        )

    def _adjust_positions_for_corporate_actions(self) -> None:
        if self._execution_price_mode != "raw":
            return
        # A single price-space run deliberately has no factor-based total-
        # return overlay.  In particular, ``raw/raw`` is the documented
        # minimal-data mode: it models the supplied unadjusted price series as
        # is and must not turn an optional adj_factor file into a hidden hard
        # dependency after the first position is opened.  The formal default
        # remains ``hfq/raw`` (dual), where the distinct spaces make audited
        # factor conversion and corporate-action adjustment mandatory.
        if (
            getattr(self, "_strategy_price_mode", None)
            == self._execution_price_mode
        ):
            return
        if not getattr(self, "current_date", None):
            return
        prev_date = self.get_prev_trade_day(self.current_date, 1)
        if not prev_date:
            return
        broker = getattr(self, "_broker", None)
        data = getattr(self, "_data", None)
        if broker is None:
            return
        # 跨夜订单与持仓必须在同一公司行为价格空间内处理。尤其是卖单：
        # Position.apply_split_ratio 会调整 position.frozen_shares，但 Order
        # 仍保留旧股数，随后成交会留下无法释放的冻结。最小且可审计的安全策略
        # 是在调股前撤销该标的全部未完成单；未持仓的限价买单也要覆盖，否则
        # 会拿除权前价格直接匹配除权后 bar。
        open_order_symbols = {
            order.symbol for order in broker.account.get_open_orders()
        }
        symbols = set(broker.account.positions) | open_order_symbols
        if not symbols:
            return
        if data is None or not callable(getattr(data, "get_adj_ratio", None)):
            raise RuntimeError(
                "raw corporate-action adjustment requires strict factor data"
            )
        for symbol in sorted(symbols):
            if self._skip_corporate_adjust(symbol):
                # hfq 类连续序列镜像作撮合腿：序列自身连续，禁调股
                # （raw 单轨不在此列——raw+因子调股是真实账户行为，保留）
                continue
            today_ratio = data.get_adj_ratio(symbol, self.current_date)
            prev_ratio = data.get_adj_ratio(symbol, prev_date)
            for value, label in (
                (today_ratio, "today adjustment ratio"),
                (prev_ratio, "previous adjustment ratio"),
            ):
                if isinstance(value, bool):
                    raise ValueError(f"{label} must be finite and positive")
                try:
                    numeric = float(value)
                except (TypeError, ValueError, OverflowError):
                    raise ValueError(
                        f"{label} must be finite and positive"
                    ) from None
                if not math.isfinite(numeric) or numeric <= 0.0:
                    raise ValueError(f"{label} must be finite and positive")
                if label.startswith("today"):
                    today_ratio = numeric
                else:
                    prev_ratio = numeric
            ratio = today_ratio / prev_ratio
            if abs(ratio - 1.0) < ADJUSTMENT_FACTOR_MATERIALITY:
                continue
            # A vendor adjustment factor is a total-return identity, not an
            # authoritative declaration of split/dividend terms.  Route it to
            # the explicit reinvestment model so ordinary cash-dividend ratios
            # cannot masquerade as strict split entitlements.  Direct callers
            # of Broker.apply_corporate_action keep the strict split policy.
            broker.apply_adjustment_factor_total_return(
                symbol,
                ratio,
                effective_date=self.current_date,
                sim_time=getattr(self, 'current_time', None),
                phase='day_start',
            )
