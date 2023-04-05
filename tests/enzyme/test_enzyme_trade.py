"""Execute trades using Enzyme vault."""
import datetime
import secrets
from decimal import Decimal

import pytest
from eth_account import Account
from hexbytes import HexBytes

from eth_defi.enzyme.integration_manager import IntegrationManagerActionId
from eth_defi.enzyme.vault import Vault
from eth_defi.hotwallet import HotWallet
from eth_defi.trace import assert_transaction_success_with_explanation
from eth_defi.uniswap_v2.deployment import UniswapV2Deployment
from eth_typing import HexAddress

from tradeexecutor.state.blockhain_transaction import BlockchainTransactionType
from tradingstrategy.pair import PandasPairUniverse
from web3 import Web3
from web3.contract import Contract

from eth_defi.event_reader.reorganisation_monitor import create_reorganisation_monitor

from tradeexecutor.ethereum.enzyme.tx import EnzymeTransactionBuilder
from tradeexecutor.ethereum.enzyme.vault import EnzymeVaultSyncModel

from tradeexecutor.state.identifier import AssetIdentifier, TradingPairIdentifier
from tradeexecutor.state.state import State
from tradeexecutor.testing.ethereumtrader_uniswap_v2 import UniswapV2TestTrader



@pytest.fixture
def hot_wallet(web3, deployer, user_1, usdc: Contract) -> HotWallet:
    """Create hot wallet for the signing tests.

    Top is up with some gas money and 500 USDC.
    """
    private_key = HexBytes(secrets.token_bytes(32))
    account = Account.from_key(private_key)
    wallet = HotWallet(account)
    wallet.sync_nonce(web3)
    tx_hash = web3.eth.send_transaction({"to": wallet.address, "from": user_1, "value": 15 * 10**18})
    assert_transaction_success_with_explanation(web3, tx_hash)
    tx_hash = usdc.functions.transfer(wallet.address, 500 * 10**6).transact({"from": deployer})
    assert_transaction_success_with_explanation(web3, tx_hash)

    return wallet


def test_enzyme_execute_open_position(
    web3: Web3,
    deployer: HexAddress,
    vault: Vault,
    usdc: Contract,
    weth: Contract,
    usdc_asset: AssetIdentifier,
    weth_asset: AssetIdentifier,
    user_1: HexAddress,
    uniswap_v2: UniswapV2Deployment,
    weth_usdc_trading_pair: TradingPairIdentifier,
    pair_universe: PandasPairUniverse,
    hot_wallet: HotWallet,
):
    """Open a simple spot buy position using Enzyme."""

    reorg_mon = create_reorganisation_monitor(web3)

    tx_hash = vault.vault.functions.addAssetManagers([hot_wallet.address]).transact({"from": user_1})
    assert_transaction_success_with_explanation(web3, tx_hash)

    sync_model = EnzymeVaultSyncModel(
        web3,
        vault.address,
        reorg_mon,
    )

    state = State()
    sync_model.sync_initial(state)

    # Make two deposits from separate parties
    usdc.functions.transfer(user_1, 500 * 10**6).transact({"from": deployer})
    usdc.functions.approve(vault.comptroller.address, 500 * 10**6).transact({"from": user_1})
    vault.comptroller.functions.buyShares(500 * 10**6, 1).transact({"from": user_1})

    # Strategy has its reserve balances updated
    sync_model.sync_treasury(datetime.datetime.utcnow(), state)
    assert state.portfolio.get_total_equity() == pytest.approx(500)

    tx_builder = EnzymeTransactionBuilder(hot_wallet, vault)

    # Check we have balance
    assert usdc.functions.balanceOf(tx_builder.get_erc_20_balance_address()).call() == 500 * 10**6

    # Now make a trade
    trader = UniswapV2TestTrader(
        web3,
        uniswap_v2,
        hot_wallet=tx_builder.hot_wallet,
        state=state,
        pair_universe=pair_universe,
        tx_builder=tx_builder,
    )

    position, trade = trader.buy(
        weth_usdc_trading_pair,
        Decimal(500),
        execute=False,
        slippage_tolerance=0.999,
    )

    # How much ETH we expect in the trade
    eth_amount = Decimal(0.310787861255819868)
    assert trade.fee_tier == 0.0030
    assert trade.planned_quantity == pytest.approx(eth_amount)
    assert trade.lp_fees_estimated == None  # TODO: UniswapV2TestTrader does not support yet?

    deltas = trade.calculate_asset_deltas()
    assert deltas[0].asset == usdc_asset.address
    assert deltas[0].raw_amount == pytest.approx(-500 * 10**6)
    assert deltas[1].asset == weth_asset.address
    assert deltas[1].raw_amount == pytest.approx(eth_amount * Decimal(1 - trade.slippage_tolerance) * 10**18)

    trader.execute_trades_simple([trade], broadcast=False)

    # Check that the blockchain transactions where constructed for Enzyme's vault
    txs = trade.blockchain_transactions
    assert len(txs) == 2  # approve + swap tokens

    approve_tx = txs[0]
    assert approve_tx.type == BlockchainTransactionType.enzyme_vault
    assert approve_tx.broadcasted_at is None
    assert approve_tx.nonce == 0
    # The EOA hot wallet transaction needs to be send to comptroller contract
    assert approve_tx.contract_address == vault.comptroller.address
    # IntegrationManager.callOnExtension() API
    assert approve_tx.args[0] == vault.deployment.contracts.integration_manager.address
    assert approve_tx.args[1] == IntegrationManagerActionId.CallOnIntegration.value
    assert len(approve_tx.args[2]) > 0  # Solidity ABI encode packed

    # This is the payload of the tx the vault performs
    assert approve_tx.details["contract"] == usdc.address
    assert approve_tx.details["function"] == "approve"
    assert approve_tx.details["args"][0] == uniswap_v2.router.address
    assert len(approve_tx.asset_deltas) == 0

    swap_tx = txs[1]
    assert swap_tx.type == BlockchainTransactionType.enzyme_vault
    assert swap_tx.broadcasted_at is None
    assert swap_tx.nonce == 1
    assert swap_tx.contract_address == vault.comptroller.address
    assert swap_tx.args[0] == vault.deployment.contracts.integration_manager.address
    assert swap_tx.args[1] == IntegrationManagerActionId.CallOnIntegration.value
    assert swap_tx.details["contract"] == uniswap_v2.router.address
    assert swap_tx.details["function"] == "swapExactTokensForTokens"

    # Spend USDC, receive WETH
    assert len(swap_tx.asset_deltas) == 2
    assert swap_tx.asset_deltas[0].asset == usdc_asset.address
    assert swap_tx.asset_deltas[0].int_amount < 0
    assert swap_tx.asset_deltas[1].asset == weth_asset.address
    assert swap_tx.asset_deltas[1].int_amount == pytest.approx(eth_amount * Decimal(1 - trade.slippage_tolerance) * 10**18)

    # Broadcast both transactions
    trader.broadcast_trades([trade], stop_on_execution_failure=True)

    assert weth.functions.balanceOf(vault.vault.address).call() > 0

    assert trade.is_success()
    assert trade.executed_quantity == Decimal('0.310787860635789571')
    assert trade.executed_price == pytest.approx(1608.81444332199)
    assert trade.executed_reserve == pytest.approx(Decimal('499.999999'))