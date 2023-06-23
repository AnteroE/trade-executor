"""Reinitialise a strategy state.

"""
import os
import shutil
from pathlib import Path
from typing import Optional

from eth_defi.hotwallet import HotWallet

from .app import app
from ..bootstrap import prepare_executor_id, create_web3_config, create_sync_model, create_state_store
from ..log import setup_logging
from ...ethereum.enzyme.vault import EnzymeVaultSyncModel
from ...strategy.execution_model import AssetManagementMode
from . import shared_options


@app.command()
def reinit(
    id: str = shared_options.id,
    name: str = shared_options.name,

    strategy_file: Path = shared_options.strategy_file,
    state_file: Optional[Path] = shared_options.state_file,
    private_key: Optional[str] = shared_options.private_key,
    log_level: str = shared_options.log_level,

    asset_management_mode: AssetManagementMode = shared_options.asset_management_mode,
    vault_address: Optional[str] = shared_options.vault_address,
    vault_deployment_block_number: Optional[int] = shared_options.vault_deployment_block_number,

    json_rpc_binance: Optional[str] = shared_options.json_rpc_binance,
    json_rpc_polygon: Optional[str] = shared_options.json_rpc_polygon,
    json_rpc_avalanche: Optional[str] = shared_options.json_rpc_avalanche,
    json_rpc_ethereum: Optional[str] = shared_options.json_rpc_ethereum,
    json_rpc_arbitrum: Optional[str] = shared_options.json_rpc_arbitrum,
    json_rpc_anvil: Optional[str] = shared_options.json_rpc_anvil,

):
    """Reinitialise a strategy state.

    Deletes all the state history of a state and starts tracking the strategy again.

    This command will fix any accounting divergences between a vault and a strategy state.
    The strategy must not have any open positions to be reinitialised, because those open
    positions cannot carry over with the current event based tracking logic.
    """

    global logger

    id = prepare_executor_id(id, strategy_file)

    logger = setup_logging(log_level)

    web3config = create_web3_config(
        gas_price_method=None,
        json_rpc_binance=json_rpc_binance,
        json_rpc_polygon=json_rpc_polygon,
        json_rpc_avalanche=json_rpc_avalanche,
        json_rpc_ethereum=json_rpc_ethereum,
        json_rpc_arbitrum=json_rpc_arbitrum,
        json_rpc_anvil=json_rpc_anvil,
    )

    assert web3config, "No RPC endpoints given. A working JSON-RPC connection is needed for check-wallet"

    # Check that we are connected to the chain strategy assumes
    web3config.choose_single_chain()

    if private_key is not None:
        hot_wallet = HotWallet.from_private_key(private_key)
    else:
        hot_wallet = None

    web3 = web3config.get_default()

    sync_model = create_sync_model(
        asset_management_mode,
        web3,
        hot_wallet,
        vault_address,
    )

    logger.info("RPC details")

    # Check the chain is online
    logger.info(f"  Chain id is {web3.eth.chain_id:,}")
    logger.info(f"  Latest block is {web3.eth.block_number:,}")

    # Check balances
    logger.info("Balance details")
    logger.info("  Hot wallet is %s", hot_wallet.address)

    vault_address =  sync_model.get_vault_address()
    start_block = None
    if vault_address:
        logger.info("  Vault is %s", vault_address)
        if vault_deployment_block_number:
            start_block = vault_deployment_block_number
            logger.info("  Vault deployment block number is %d", start_block)

    if not state_file:
        state_file = f"state/{id}.json"

    state_file = Path(state_file)
    store = create_state_store(state_file)
    assert not store.is_pristine(), f"State does not exists yet: {state_file}"

    # Make a backup
    # https://stackoverflow.com/a/47528275/315168
    for i in range(1, 20):
        try:
            backup_file = state_file.with_suffix(".reinit-backup-{i}.json")
            state_file.rename(backup_file)
        except Exception as e:
            raise RuntimeError(f"Could not create backup {backup_file}") from e

    logger.info("Old state backed up as %s", backup_file)

    state = store.create(name)

    logger.info("Syncing initial strategy chain state.")
    logger.info("For Enzyme vaults this may take a long time as the sync will go through all the blocks in the chain.")
    logger.info("To speed up process use --vault_deployment_block_number hint as a command line argument.")
    logger.info(f"Vault deployment block number hint is {start_block or 0:,}.")

    assert isinstance(sync_model, EnzymeVaultSyncModel), f"reinit currently only supports EnzymeVaultSyncModel, got {sync_model}"

    sync_model.sync_reinit(state, start_block=start_block)

    store.sync(state)

    web3config.close()

    reserve_position = state.portfolio.get_default_reserve_position()

    logger.info("Reserve balance is %s", reserve_position)

    assert reserve_position.quantity > 0, f"Reinitialisation did not see any deposits in vault: {sync_model.vault}, reserve position is {reserve_position}"

    logger.info("All done: State deployment info is %s", state.sync.deployment)
