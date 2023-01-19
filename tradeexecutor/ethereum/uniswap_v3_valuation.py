"""Value model based on Uniswap v3 market price.

Value positions based on their "dump" price on Uniswap,
assuming we get the worst possible single trade execution.
"""
import datetime
from typing import Tuple

from tradeexecutor.ethereum.uniswap_v3_live_pricing import UniswapV3LivePricing
from tradeexecutor.state.position import TradingPosition
from tradeexecutor.state.types import USDollarAmount
from tradeexecutor.strategy.valuation import ValuationModel


class UniswapV3PoolRevaluator(ValuationModel):
    """Re-value assets based on their on-chain price.

    Does directly JSON-RPC call to get the latest price in the Uniswap pools.

    Only uses direct route - mostly useful for testing, may not give a realistic price in real
    world with multiple order routing options.

    .. warning ::

        This valuation metohd always uses the latest price. It
        cannot be used for backtesting.
    """

    def __init__(self, pricing_model: UniswapV3LivePricing):
        assert isinstance(pricing_model, UniswapV3LivePricing), f"Require UniswapV3LivePricing instance. Received {pricing_model}"
        
        self.pricing_model = pricing_model

    def __call__(self,
                 ts: datetime.datetime,
                 position: TradingPosition) -> Tuple[datetime.datetime, USDollarAmount]:
        assert isinstance(ts, datetime.datetime)
        pair = position.pair

        assert position.is_long(), "Short not supported"

        quantity = position.get_quantity()
        # Cannot do pricing for zero quantity
        if quantity == 0:
            return ts, 0.0

        price = self.pricing_model.get_sell_price(ts, pair, quantity)

        return ts, price


def uniswap_v3_sell_valuation_factory(pricing_model):
    return UniswapV3PoolRevaluator(pricing_model)