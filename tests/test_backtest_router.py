"""Backtesting router tests.
"""
import datetime
import logging
from decimal import Decimal

import pytest

from tradeexecutor.state.trade import TradeStatus
from tradeexecutor.strategy.strategy_module import ReserveCurrency
from tradeexecutor.testing.backtest_trader import BacktestTrader
from tradingstrategy.chain import ChainId
from tradingstrategy.timebucket import TimeBucket

from tradeexecutor.backtest.backtest_execution import BacktestExecutionModel
from tradeexecutor.backtest.backtest_pricing import BacktestPricingModel
from tradeexecutor.backtest.backtest_routing import BacktestRoutingModel
from tradeexecutor.backtest.backtest_sync import BacktestSyncer
from tradeexecutor.backtest.backtest_valuation import BacktestValuationModel
from tradeexecutor.backtest.simulated_wallet import SimulatedWallet
from tradeexecutor.cli.log import setup_pytest_logging
from tradeexecutor.ethereum.default_routes import get_pancake_default_routing_parameters
from tradeexecutor.state.state import State
from tradeexecutor.strategy.execution_model import ExecutionContext
from tradeexecutor.strategy.trading_strategy_universe import load_all_data, TradingStrategyUniverse, \
    translate_trading_pair
from tradeexecutor.utils.timer import timed_task


@pytest.fixture(scope="module")
def logger(request):
    """Setup test logger."""
    return setup_pytest_logging(request, mute_requests=False)



@pytest.fixture(scope="module")
def execution_context(request) -> ExecutionContext:
    """Setup backtest execution context."""
    return ExecutionContext(live_trading=False, timed_task_context_manager=timed_task)



@pytest.fixture(scope="module")
def universe(request, persistent_test_client, execution_context) -> TradingStrategyUniverse:
    """Backtesting data universe.

    This contains only data for WBNB-BUSD pair on PancakeSwap v2 since 2021-01-01.
    """

    client = persistent_test_client

    # Time bucket for our candles
    candle_time_bucket = TimeBucket.d1

    # Which chain we are trading
    chain_id = ChainId.bsc

    # Which exchange we are trading on.
    exchange_slug = "pancakeswap-v2"

    # Which trading pair we are trading
    trading_pair = ("WBNB", "BUSD")

    # Load all datas we can get for our candle time bucket
    dataset = load_all_data(client, candle_time_bucket, execution_context)

    # Filter down to the single pair we are interested in
    universe = TradingStrategyUniverse.create_single_pair_universe(
        dataset,
        chain_id,
        exchange_slug,
        trading_pair[0],
        trading_pair[1],
    )

    return universe


@pytest.fixture(scope="module")
def routing_model() -> BacktestRoutingModel:
    routing_parameters = get_pancake_default_routing_parameters(ReserveCurrency.busd)
    routing_model = BacktestRoutingModel(**routing_parameters)
    return routing_model


@pytest.fixture(scope="module")
def pricing_model(routing_model, universe) -> BacktestPricingModel:
    return BacktestPricingModel(universe, routing_model)


@pytest.fixture(scope="module")
def valuation_model(pricing_model) -> BacktestValuationModel:
    return BacktestValuationModel(pricing_model)


@pytest.fixture()
def wallet(universe) -> SimulatedWallet:
    return SimulatedWallet()


@pytest.fixture()
def deposit_syncer(wallet) -> BacktestSyncer:
    """Start with 10,000 USD."""
    return BacktestSyncer(wallet, Decimal(10_000))


@pytest.fixture()
def state(universe: TradingStrategyUniverse, deposit_syncer: BacktestSyncer) -> State:
    """Start with 10,000 USD cash in the portfolio."""
    state = State()
    events = deposit_syncer(state.portfolio, datetime.datetime(1970, 1, 1), universe.reserve_assets)
    assert len(events) == 1
    token, usd_exchange_rate = state.portfolio.get_default_reserve_currency()
    assert token.token_symbol == "BUSD"
    assert usd_exchange_rate == 1
    assert state.portfolio.get_current_cash() == 10_000
    return state


def test_get_historical_price(
        logger: logging.Logger,
        state: State,
        universe: TradingStrategyUniverse,
        pricing_model: BacktestPricingModel,
    ):
    """Retrieve historical buy and sell price."""

    ts = datetime.datetime(2021, 6, 1)
    execution_model = BacktestExecutionModel(max_slippage=0.01)
    trader = BacktestTrader(ts, state, universe, execution_model, pricing_model)
    wbnb_busd = translate_trading_pair(universe.universe.pairs.get_single())

    # Check the candle price range that we have data to get the price
    price_range = universe.universe.candles.get_candles_by_pair(wbnb_busd.internal_id)
    assert price_range.iloc[0]["timestamp"] < ts

    # Check the candle price range that we have data to get the price
    liquidity_range = universe.universe.liquidity.get_samples_by_pair(wbnb_busd.internal_id)
    assert liquidity_range.iloc[0]["timestamp"] < ts

    # Get the price for buying WBNB for 1000 USD at 2021-1-1
    buy_price = trader.get_buy_price(wbnb_busd, Decimal(1_000))
    assert buy_price == pytest.approx(361.04028)

    # Get the price for sellinb 1 WBNB
    sell_price = trader.get_sell_price(wbnb_busd, Decimal(1))
    assert sell_price == pytest.approx(361.04028)


def test_create_and_execute_backtest_trade(
        logger: logging.Logger,
        state: State,
        universe: TradingStrategyUniverse,
    ):
    """Manually walk through creation and execution of a single backtest trade."""

    ts = datetime.datetime(2021, 1, 1)
    execution_model = BacktestExecutionModel(max_slippage=0.01)
    trader = BacktestTrader(ts, state, universe, execution_model)
    wbnb_busd = universe.universe.pairs.get_single()

    # Create trade for 10 WBNB buy
    position, trade = trader.create(wbnb_busd, Decimal(10))

    assert trade.is_buy()
    assert trade.get_status() == TradeStatus.started




