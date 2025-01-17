"""Metadata describes strategy for website rendering.

Metadata is not stored as the part of the state, but configured
on the executor start up.
"""
import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, TypedDict

from dataclasses_json import dataclass_json
from tradingstrategy.chain import ChainId

from tradeexecutor.state.state import State
from tradeexecutor.state.types import ZeroExAddress
from tradeexecutor.strategy.execution_model import AssetManagementMode


class EnzymeSmartContracts(TypedDict):
    """Various smart contract addresses associated with Enzyme.

    - Vault specific contracts

    - Enzyme chain specific contracts

    See :py:class:`eth_defi.enzyme.deployment.EnzymeContracts` for the full list of protocol specific contracts.
    """

    #: Vault address
    vault: ZeroExAddress

    #: Comptroller proxy contract for vault
    comptroller: ZeroExAddress

    #: Generic adapter doing asset management transactions
    generic_adapter: ZeroExAddress

    #: Enzyme contract
    gas_relay_paymaster_lib: ZeroExAddress

    #: Enzyme contract
    gas_relay_paymaster_factory: ZeroExAddress

    #: Enzyme contract
    integration_manager: ZeroExAddress

    #: Calculate values for various positions
    #:
    #: See https://github.com/enzymefinance/protocol/blob/v4/contracts/persistent/off-chain/fund-value-calculator/IFundValueCalculator.sol
    fund_value_calculator: ZeroExAddress

    #: VaultUSDCPaymentForwarder.sol
    #:
    payment_forwarder: ZeroExAddress


@dataclass_json
@dataclass
class OnChainData:
    """Smart contract information for a strategy.

    Needed for frontend deposit/redemptions/etc.
    """

    #: On which this strategy runs on
    chain_id: ChainId = field(default=ChainId.unknown)

    #: Is this s hot wallet strategy or vaulted strategy
    #:
    asset_management_mode: AssetManagementMode = field(default=AssetManagementMode.dummy)

    #: Smart contracts configured for this strategy.
    #:
    #: Depend on the vault backend.
    #:
    smart_contracts: EnzymeSmartContracts = field(default_factory=dict)


@dataclass_json
@dataclass
class Metadata:
    """Strategy metadata."""

    #: Strategy name
    name: str

    #: 1 sentence
    short_description: Optional[str]

    #: Multiple paragraphs.
    long_description: Optional[str]

    #: For <img src>
    icon_url: Optional[str]

    #: When the instance was started last time, UTC
    started_at: datetime.datetime

    #: Is the executor main loop running or crashed.
    #:
    #: Use /status endpoint to get the full exception info.
    #:
    #: Not really a part of metadata, but added here to make frontend
    #: queries faster. See also :py:class:`tradeexecutor.state.executor_state.ExecutorState`.
    executor_running: bool

    #: List of smart contracts and related web3 interaction information for this strategy.
    #:
    on_chain_data: OnChainData = field(default_factory=OnChainData)

    #: The previous backtest run results for this strategy.
    #:
    #: Used in the web frontend to display the backtested values.
    #:
    backtested_state: Optional[State] = None

    #: Backtest notebook .ipynb file
    #:
    #:
    backtest_notebook: Optional[Path] = None

    #: Backtest notebook .html file
    #:
    #:
    backtest_html: Optional[Path] = None

    #: How many days live data is collected until key metrics are switched from backtest to live trading based
    #:
    key_metrics_backtest_cut_off: datetime.timedelta = datetime.timedelta(days=90)

    @staticmethod
    def create_dummy() -> "Metadata":
        return Metadata(
            name="Dummy",
            short_description="Dummy metadata",
            long_description=None,
            icon_url=None,
            started_at=datetime.datetime.utcnow(),
            executor_running=True,
        )

    def has_backtest_data(self) -> bool:
        """Does this strategy have backtest data available on the file system?"""
        return (self.backtest_notebook and self.backtest_notebook.exists()) and (self.backtest_html and self.backtest_html.exists())