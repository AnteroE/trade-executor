"""Uniswap v2 live pricing.

Directly asks Uniswap v2 asset price from Uniswap pair contract
and JSON-RPC API.
"""
import logging
import datetime
from decimal import Decimal, ROUND_DOWN
from typing import Optional

from web3 import Web3

from tradeexecutor.ethereum.uniswap_v3_execution import UniswapV3ExecutionModel
from tradeexecutor.ethereum.uniswap_v3_routing import UniswapV3SimpleRoutingModel, route_tokens, get_uniswap_for_pair
from tradeexecutor.state.identifier import TradingPairIdentifier
from tradeexecutor.strategy.execution_model import ExecutionModel

from tradeexecutor.state.types import USDollarAmount
from tradeexecutor.strategy.pricing_model import PricingModel
from tradeexecutor.strategy.trading_strategy_universe import TradingStrategyUniverse, translate_trading_pair
from tradingstrategy.pair import PandasPairUniverse

from eth_defi.uniswap_v3.price import UniswapV3PriceHelper
from eth_defi.uniswap_v3.deployment import UniswapV3Deployment

logger = logging.getLogger(__name__)


class UniswapV3LivePricing(PricingModel):
    """Always pull the latest dollar price for an asset from Uniswap v2 deployment.

    Supports

    - Two-way BUSD -> Cake

    - Three-way trades BUSD -> BNB -> Cake

    ... within a single, uniswap v3 like exchange.

    .. note ::

        If a trade quantity/currency amount is not given uses
        a "default small value" that is 0.1. Depending on the token,
        this value might be too much/too little, as Uniswap
        fixed point math starts to break for very small amounts.
        For example, for USDC trade 10 cents is already quite low.

    More information

    - `About ask and bid <https://www.investopedia.com/terms/b/bid-and-ask.asp>`_:
    """

    def __init__(self,
                 web3: Web3,
                 pair_universe: PandasPairUniverse,
                 routing_model: UniswapV3SimpleRoutingModel,
                 very_small_amount=Decimal("0.10")):

        assert isinstance(web3, Web3)
        assert isinstance(routing_model, UniswapV3SimpleRoutingModel)
        assert isinstance(pair_universe, PandasPairUniverse)

        self.web3 = web3
        self.pair_universe = pair_universe
        self.very_small_amount = very_small_amount
        self.routing_model = routing_model

        assert isinstance(self.very_small_amount, Decimal)

    def get_uniswap(self, target_pair: TradingPairIdentifier) -> UniswapV3Deployment:
        """Helper function to speed up Uniswap v3 deployment resolution."""
        if target_pair not in self.uniswap_cache:
            self.uniswap_cache[target_pair] = get_uniswap_for_pair(
                self.web3, 
                self.routing_model.address_map, 
                target_pair
            )
        return self.uniswap_cache[target_pair]
    
    def get_price_helper(self, target_pair: TradingPairIdentifier) -> UniswapV3PriceHelper:
        uniswap_v3 = self.get_uniswap(target_pair)
        return UniswapV3PriceHelper(uniswap_v3)
        
    def get_pair_for_id(self, internal_id: int) -> Optional[TradingPairIdentifier]:
        """Look up a trading pair.

        Useful if a strategy is only dealing with pair integer ids.

        :return:
            None if the price data is not available
        """
        pair = self.pair_universe.get_pair_by_id(internal_id)
        if not pair:
            return None
        return translate_trading_pair(pair)

    def check_supported_quote_token(self, pair: TradingPairIdentifier):
        assert pair.quote.address == self.routing_model.reserve_token_address, f"Quote token {self.routing_model.reserve_token_address} not supported for pair {pair}, pair tokens are {pair.base.address} - {pair.quote.address}"

    def get_sell_price(self,
                       ts: datetime.datetime,
                       pair: TradingPairIdentifier,
                       quantity: Optional[Decimal],
                       ) -> USDollarAmount:
        """Get live price on Uniswap."""

        if quantity is None:
            quantity = Decimal(self.very_small_amount)

        assert isinstance(quantity, Decimal)

        target_pair, intermediate_pair = self.routing_model.route_pair(self.pair_universe, pair)

        base_addr, quote_addr, intermediate_addr = route_tokens(target_pair, intermediate_pair)

        # In three token trades, be careful to use the correct reserve token
        quantity_raw = target_pair.base.convert_to_raw_amount(quantity)

        # See estimate_sell_received_amount_raw in eth_defi.uniswap_v2.fees
        path = (
            [base_addr, intermediate_addr, quote_addr] 
            if intermediate_addr 
            else [base_addr, quote_addr]
        )
        fees = (
            [intermediate_pair.fee, target_pair.fee]
            if intermediate_addr
            else [target_pair.fee]
        )
        
        price_helper = self.get_price_helper(target_pair)
        received_raw = price_helper.get_amount_out(
            amount_in=quantity_raw,
            path=path,
            fees=fees
        ) 
            
        if intermediate_pair is not None:
            received = intermediate_pair.quote.convert_to_decimal(received_raw)
        else:
            received = target_pair.quote.convert_to_decimal(received_raw)

        return float(received / quantity)

    def get_buy_price(self,
                       ts: datetime.datetime,
                       pair: TradingPairIdentifier,
                       reserve: Optional[Decimal],
                       ) -> USDollarAmount:
        """Get live price on Uniswap.

        :param reserve:
            The buy size in quote token e.g. in dollars

        :return: Price for one reserve unit e.g. a dollar
        """

        if reserve is None:
            reserve = Decimal(self.very_small_amount)
        else:
            assert isinstance(reserve, Decimal), f"Reserve must be decimal, got {reserve.__class__}: {reserve}"

        target_pair, intermediate_pair = self.routing_model.route_pair(self.pair_universe, pair)

        base_addr, quote_addr, intermediate_addr = route_tokens(target_pair, intermediate_pair)

        # In three token trades, be careful to use the correct reserve token
        if intermediate_pair is not None:
            reserve_raw = intermediate_pair.quote.convert_to_raw_amount(reserve)
            self.check_supported_quote_token(intermediate_pair)
        else:
            reserve_raw = target_pair.quote.convert_to_raw_amount(reserve)
            self.check_supported_quote_token(pair)

        # See eth_defi.uniswap_v2.fees.estimate_buy_received_amount_raw
        path = (
            [quote_addr, intermediate_addr, base_addr]
            if intermediate_addr
            else base_addr 
        )
        fees = (
            [intermediate_pair.fee, target_pair.fee]
            if intermediate_addr
            else [target_pair.fee]
        )
        
        price_helper = self.get_price_helper(target_pair)
        token_raw_received = price_helper.get_amount_out(
            amount_in=reserve_raw,
            path=path,
            fees=fees
        )
        
        token_received = target_pair.base.convert_to_decimal(token_raw_received)
        return float(reserve / token_received)

    def get_mid_price(self,
                      ts: datetime.datetime,
                      pair: TradingPairIdentifier) -> USDollarAmount:
        """Get the mid price by the candle."""

        # TODO: Use native Uniswap router functions to get the mid price
        # Here we are using a hack
        bp = self.get_buy_price(ts, pair, self.very_small_amount)
        sp = self.get_sell_price(ts, pair, self.very_small_amount)
        return (bp + sp) / 2

    def quantize_base_quantity(self, pair: TradingPairIdentifier, quantity: Decimal, rounding=ROUND_DOWN) -> Decimal:
        """Convert any base token quantity to the native token units by its ERC-20 decimals."""
        assert isinstance(pair, TradingPairIdentifier)
        decimals = pair.base.decimals
        return Decimal(quantity).quantize((Decimal(10) ** Decimal(-decimals)), rounding=ROUND_DOWN)


def uniswap_v3_live_pricing_factory(
        execution_model: ExecutionModel,
        universe: TradingStrategyUniverse,
        routing_model: UniswapV3SimpleRoutingModel,
        ) -> UniswapV3LivePricing:

    assert isinstance(universe, TradingStrategyUniverse)
    assert isinstance(execution_model, (UniswapV3ExecutionModel)), f"Execution model not compatible with this execution model. Received {execution_model}"
    assert isinstance(routing_model, UniswapV3SimpleRoutingModel), f"This pricing method only works with Uniswap routing model, we received {routing_model}"
    web3 = execution_model.web3
    return UniswapV3LivePricing(
        web3,
        universe.universe.pairs,
        routing_model
    )
