"""Perform a grid search ove strategy parameters to find optimal parameters."""
import datetime
import itertools
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Dict, List, Tuple, Any, Optional

import pandas as pd

import futureproof
from tradingstrategy.client import Client

from tradeexecutor.analysis.trade_analyser import TradeSummary, build_trade_analysis
from tradeexecutor.backtest.backtest_routing import BacktestRoutingIgnoredModel
from tradeexecutor.backtest.backtest_runner import run_backtest_inline
from tradeexecutor.state.state import State
from tradeexecutor.state.types import USDollarAmount
from tradeexecutor.strategy.cycle import CycleDuration
from tradeexecutor.strategy.default_routing_options import TradeRouting
from tradeexecutor.strategy.routing import RoutingModel
from tradeexecutor.strategy.strategy_module import DecideTradesProtocol
from tradeexecutor.strategy.trading_strategy_universe import TradingStrategyUniverse

logger = logging.getLogger(__name__)


@dataclass
class GridParameter:
    name: str
    value: Any

    def __post_init__(self):
        pass

    def __hash__(self):
        return hash((self.name, self.value))

    def __eq__(self, other):
        return self.name == other.name and self.value == other.value

    def to_path(self) -> str:
        """"""
        value = self.value
        if type(value) in (float, int, str):
            return f"{self.name}={self.value}"
        else:
            raise NotImplementedError(f"We do not support filename conversion for value {type(value)}={value}")


@dataclass()
class GridCombination:
    """One combination line in grid search."""

    #: Alphabetically sorted list of parameters
    parameters: Tuple[GridParameter]

    def __post_init__(self):
        assert len(self.parameters) > 0

    def __hash__(self):
        return hash(self.parameters)

    def __eq__(self, other):
        return self.parameters == other.parameters

    def get_state_path(self) -> Path:
        """Get the path where the resulting state file is stored."""
        path_parts = [p.to_path() for p in self.parameters]
        return Path(os.path.join(*path_parts))

    def validate(self):
        """Check arguments can be serialised as fs path."""
        assert isinstance(self.get_state_path(), Path)

    def as_dict(self) -> dict:
        """Get as kwargs mapping."""
        return {p.name: p.value for p in self.parameters}

    def destructure(self) -> List[Any]:
        """Open parameters dict.

        This will return the arguments in the same order you pass them to :py:func:`prepare_grid_combinations`.
        """
        return [p.value for p in self.parameters]


@dataclass()
class GridSearchResult:
    """Result for one grid combination."""

    combination: GridCombination

    state: State

    summary: TradeSummary


class GridSearcWorker(Protocol):
    """Define how to create different strategy bodies."""

    def __call__(self, universe: TradingStrategyUniverse, combination: GridCombination) -> GridSearchResult:
        """Run a new decide_trades() strategy body based over the serach parameters.

        :param args:
        :param kwargs:
        :return:
        """


def prepare_grid_combinations(parameters: Dict[str, List[Any]]) -> List[GridCombination]:
    """Get iterable search matrix of all parameter combinations.

    Make sure we preverse the original order of the grid search parameters.
    """

    args_lists: List[list] = []
    for name, values in parameters.items():
        args = [GridParameter(name, v) for v in values]
        args_lists.append(args)

    combinations = itertools.product(*args_lists)

    # Maintain the orignal parameter order over itertools.product()
    order = tuple(parameters.keys())
    def sort_by_order(combination: List[GridParameter]):
        temp = {p.name: p for p in combination}
        return tuple([temp[o] for o in order])

    combinations = [GridCombination(sort_by_order(c)) for c in combinations]
    for c in combinations:
        c.validate()
    return combinations


def run_grid_combination(
        grid_search_worker: GridSearcWorker,
        universe: TradingStrategyUniverse,
        combination: GridCombination,
        result_path: Path,
):
    state_file = result_path.joinpath(combination.get_state_path()).joinpath("state.json")
    if state_file.exists():
        with open(state_file, "rt") as inp:
            data = inp.read()
            state = State.from_json(data)
    else:
        pass

    result = grid_search_worker(universe, combination)
    return result


def perform_grid_search(
        grid_search_worker: GridSearcWorker,
        universe: TradingStrategyUniverse,
        combinations: List[GridCombination],
        result_path: Path,
        max_workers=16,
) -> Dict[GridCombination, GridSearchResult]:
    """Search different strategy parameters over a grid.

    - Run using parallel processing via threads.
      `Numoy should release GIL for threads <https://stackoverflow.com/a/40630594/315168>`__.

    - Save the resulting state files to a directory structure
      for invidual run analysis

    - If a result exists, do not perform the backtest again.
      However we still load the summary

    - Trading Strategy Universe is shared across threads to save memory.

    :param combinations:
        Prepared grid combinations.

        See :py:func:`prepare_grid_combinations`

    :param result_path:
        A folder where resulting state files will be stored.

    :return
    """

    assert isinstance(result_path, Path), f"Expected Path, got {type(result_path)}"
    assert result_path.exists() and result_path.is_dir(), f"Not a dir: {result_path}"

    start = datetime.datetime.utcnow()

    logger.info("Performing a grid search over %s combinations, storing results in %s, with %d threads",
                len(combinations),
                result_path,
                max_workers,
                )

    task_args = [(grid_search_worker, universe, c, result_path) for c in combinations]

    if max_workers > 1:

        logger.info("Doing a multiprocess grid search")
        # Do a parallel scan for the maximum speed
        #
        # Set up a futureproof task manager
        #
        # For futureproof usage see
        # https://github.com/yeraydiazdiaz/futureproof
        executor = futureproof.ThreadPoolExecutor(max_workers=max_workers)
        tm = futureproof.TaskManager(executor, error_policy=futureproof.ErrorPolicyEnum.RAISE)

        # Run the checks parallel using the thread pool
        tm.map(run_grid_combination, task_args)

        # Extract results from the parallel task queue
        results = [task.result for task in tm.as_completed()]

    else:
        logger.info("Doing a single thread grid search")
        # Do single thread - good for debuggers like pdb/ipdb
        #
        iter = itertools.starmap(run_grid_combination, task_args)

        # Force workers to finish
        results = list(iter)

    duration = datetime.datetime.utcnow() - start
    logger.info("Grid search finished in %s", duration)

    data = {}
    for idx, task in enumerate(task_args):
        combination = task[2]
        result = results[idx]
        data[combination] = result

    return data



def run_grid_search_backtest(
        combination: GridCombination,
        decide_trades: DecideTradesProtocol,
        universe: TradingStrategyUniverse,
        cycle_duration: Optional[CycleDuration] = None,
        start_at: Optional[datetime.datetime] = None,
        end_at: Optional[datetime.datetime] = None,
        initial_deposit: USDollarAmount = 5000.0,
        trade_routing: Optional[TradeRouting] = None,
        data_delay_tolerance: Optional[pd.Timedelta] = None,
        name: str = "backtest",
        routing_model: Optional[RoutingModel] = None,
) -> GridSearchResult:
    assert isinstance(universe, TradingStrategyUniverse)

    universe_range = universe.universe.candles.get_timestamp_range()
    if not start_at:
        start_at = universe_range[0]

    if not end_at:
        end_at = universe_range[1]

    if not cycle_duration:
        cycle_duration = CycleDuration.from_timebucket(universe.universe.candles.time_bucket)
    else:
        assert isinstance(cycle_duration, CycleDuration)

    if not routing_model:
        routing_model = BacktestRoutingIgnoredModel(universe.get_reserve_asset().address)

    # Run the test
    state, universe, debug_dump = run_backtest_inline(
        name="No stop loss",
        start_at=start_at.to_pydatetime(),
        end_at=end_at.to_pydatetime(),
        client=None,
        cycle_duration=cycle_duration,
        decide_trades=decide_trades,
        create_trading_universe=None,
        universe=universe,
        initial_deposit=initial_deposit,
        reserve_currency=None,
        trade_routing=TradeRouting.user_supplied_routing_model,
        routing_model=routing_model,
        allow_missing_fees=True,
        data_delay_tolerance=data_delay_tolerance,
    )

    analysis = build_trade_analysis(state.portfolio)
    summary = analysis.calculate_summary_statistics()

    return GridSearchResult(
        combination=combination,
        state=state,
        summary=summary,
    )
