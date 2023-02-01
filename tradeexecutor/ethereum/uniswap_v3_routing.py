"""Route trades to different Uniswap v2 like exchanges."""

import logging
from typing import Dict, Set, List, Optional, Tuple

from eth_typing import HexAddress, ChecksumAddress
from tradingstrategy.chain import ChainId
from web3 import Web3

from eth_defi.gas import estimate_gas_fees
from eth_defi.uniswap_v3.deployment import UniswapV3Deployment, fetch_deployment
from eth_defi.uniswap_v3.swap import swap_with_slippage_protection
from web3.exceptions import ContractLogicError

from tradeexecutor.ethereum.tx import TransactionBuilder
from tradeexecutor.state.blockhain_transaction import BlockchainTransaction
from tradeexecutor.state.identifier import TradingPairIdentifier, AssetIdentifier
from tradeexecutor.state.trade import TradeExecution
from tradeexecutor.strategy.routing import RoutingModel, CannotRouteTrade
from tradeexecutor.strategy.trading_strategy_universe import TradingStrategyUniverse, translate_trading_pair, \
    translate_token
from tradingstrategy.pair import PandasPairUniverse

from tradeexecutor.strategy.universe_model import StrategyExecutionUniverse
from tradeexecutor.ethereum.routing_state import (
    RoutingStateBase, 
    route_tokens, # forwarded import
    OutOfBalance, # forwarded import
)

logger = logging.getLogger(__name__)


class UniswapV3RoutingState(RoutingStateBase):
    def __init__(self,
                 pair_universe: PandasPairUniverse,
                 tx_builder: Optional[TransactionBuilder]=None,
                 swap_gas_limit=2_000_000):
        super().__init__(pair_universe, tx_builder, swap_gas_limit)
    
    def get_uniswap_for_pair(self, factory_router_map: dict, target_pair: TradingPairIdentifier) -> UniswapV3Deployment:
        """Get a router for a trading pair."""
        return get_uniswap_for_pair(self.web3, factory_router_map, target_pair)
    
    def trade_on_router_two_way(self,
            uniswap: UniswapV3Deployment,
            target_pair: TradingPairIdentifier,
            reserve_asset: AssetIdentifier,
            reserve_amount: int,
            max_slippage: float,
            check_balances: False):
        """Prepare the actual swap. Same for Uniswap V2 and V3.

        :param check_balances:
            Check on-chain balances that the account has enough tokens
            and raise exception if not.
        """

        hot_wallet = self.tx_builder.hot_wallet
        
        base_token, quote_token = self.get_base_and_quote(target_pair, reserve_asset)

        if check_balances:
            self.check_has_enough_tokens(quote_token, reserve_amount)

        bound_swap_func = swap_with_slippage_protection(
            uniswap,
            recipient_address=hot_wallet.address,
            base_token=base_token,
            quote_token=quote_token,
            amount_in=reserve_amount,
            max_slippage=max_slippage * 100,  # In BPS
            pool_fees=target_pair.fee # TODO check in right format
        )
        
        return self.get_signed_tx(bound_swap_func, self.swap_gas_limit)

    def trade_on_router_three_way(self,
            uniswap: UniswapV3Deployment,
            target_pair: TradingPairIdentifier,
            intermediary_pair: TradingPairIdentifier,
            reserve_asset: AssetIdentifier,
            reserve_amount: int,
            max_slippage: float,
            check_balances: False):
        """Prepare the actual swap for three way trade.

        :param check_balances:
            Check on-chain balances that the account has enough tokens
            and raise exception if not.
        """

        hot_wallet = self.tx_builder.hot_wallet

        self.validate_pairs(target_pair, intermediary_pair)
        
        self.validate_exchange(target_pair, intermediary_pair)

        base_token, quote_token, intermediary_token = self.get_base_quote_intermediary(target_pair, intermediary_pair, reserve_asset)

        if check_balances:
            self.check_has_enough_tokens(quote_token, reserve_amount)

        pool_fees = [intermediary_pair.fee, target_pair.fee]
        
        bound_swap_func = swap_with_slippage_protection(
            uniswap,
            recipient_address=hot_wallet.address,
            base_token=base_token,
            quote_token=quote_token,
            pool_fees=pool_fees,
            amount_in=reserve_amount,
            max_slippage=max_slippage * 100,  # In BPS,
            intermediate_token=intermediary_token,
        )
        
        return self.get_signed_tx(bound_swap_func, self.swap_gas_limit)

class UniswapV3SimpleRoutingModel(RoutingModel):
    """A simple router that does not optimise the trade execution cost. Designed for uniswap-v2 forks.

    - Able to trade on multiple exchanges

    - Able to three-way trades through predefined intermediary hops,
      either on the exchange itself or some outside exchange
    """

    def __init__(self,
                 address_map: Dict[str, HexAddress],
                 allowed_intermediary_pairs: Dict[str, str],
                 reserve_token_address: str,
                 chain_id: Optional[ChainId] = None,
                 ):
        """
        
        
        :param address_map:
            Defines router smart contracts to be used with each DEX.
            Address map is a dict of factory, router, position_manager,
            and quoter addresses

        :param allowed_intermediary_pairs:

            Quote token address -> pair smart contract address mapping.

            Because we hold our reserves only in one currecy e.g. BUSD
            and we want to trade e.g. Cake/BNB pairs, we need to whitelist
            BNB as an allowed intermediary token.
            This makes it possible to do BUSD -> BNB -> Cake trade.
            This set is the list of pair smart contract addresses that
            are allowed to be used as a hop.

        :param chain_id:
            Store the chain id for which these routes were generated for.

        :param reserve_token_address:
            Token address of our reserve currency.
            Relevent for buy/sell routing.
            Lowercase.
        """

        assert type(address_map) == dict
        assert type(allowed_intermediary_pairs) == dict
        assert type(reserve_token_address) == str

        assert reserve_token_address.lower() == reserve_token_address

        # Convert all key addresses to lowercase to
        # avoid mix up with Ethereum address checksums
        self.address_map = {k.lower(): v for k, v in address_map.items()}
        self.allowed_intermediary_pairs = {k.lower(): v.lower() for k, v in allowed_intermediary_pairs.items()}
        self.reserve_token_address = reserve_token_address
        self.chain_id = chain_id

    def get_reserve_asset(self, pair_universe: PandasPairUniverse) -> AssetIdentifier:
        """Translate our reserve token address tok an asset description."""
        assert pair_universe is not None, "Pair universe missing"
        reserve_token = pair_universe.get_token(self.reserve_token_address)
        assert reserve_token, f"Pair universe does not contain our reserve asset {self.reserve_token_address}"
        return translate_token(reserve_token)

    def make_direct_trade(self,
                          routing_state: UniswapV3RoutingState,
                          target_pair: TradingPairIdentifier,
                          reserve_asset: AssetIdentifier,
                          reserve_amount: int,
                          max_slippage: float,
                          check_balances=False,
                          ) -> List[BlockchainTransaction]:
        """Prepare a trade where target pair has out reserve asset as a quote token.

        :return:
            List of approval transactions (if any needed)
        """
        uniswap = routing_state.get_uniswap_for_pair(self.address_map, target_pair)
        token_address = reserve_asset.address
        txs = routing_state.ensure_token_approved(token_address, uniswap.swap_router.address)
        txs += routing_state.trade_on_router_two_way(
            uniswap,
            target_pair,
            reserve_asset,
            reserve_amount,
            max_slippage,
            check_balances,
            )
        return txs

    def make_multihop_trade(self,
                          routing_state: UniswapV3RoutingState,
                          target_pair: TradingPairIdentifier,
                          intermediary_pair: TradingPairIdentifier,
                          reserve_asset: AssetIdentifier,
                          reserve_amount: int,
                          max_slippage: float,
                          check_balances=False,
                          ) -> List[BlockchainTransaction]:
        """Prepare a trade where target pair has out reserve asset as a quote token.

        :return:
            List of approval transactions (if any needed)
        """
        uniswap = routing_state.get_uniswap_for_pair(self.address_map, target_pair)
        token_address = reserve_asset.address
        txs = routing_state.ensure_token_approved(token_address, uniswap.router.address)
        txs += routing_state.trade_on_router_three_way(
            uniswap,
            target_pair,
            intermediary_pair,
            reserve_asset,
            reserve_amount,
            max_slippage,
            check_balances,
            )
        return txs

    def trade(self,
              routing_state: UniswapV3RoutingState,
              target_pair: TradingPairIdentifier,
              reserve_asset: AssetIdentifier,
              reserve_asset_amount: int,  # Raw amount of the reserve asset
              max_slippage: float=0.01,
              check_balances=False,
              intermediary_pair: Optional[TradingPairIdentifier] = None,
              ) -> List[BlockchainTransaction]:
        """

        :param routing_state:
        :param target_pair:
        :param reserve_asset:
        :param reserve_asset_amount:
        :param max_slippage:
            Max slippage per trade. 0.01 is 1%.
        :param check_balances:
            Check on-chain balances that the account has enough tokens
            and raise exception if not.
        :param intermediary_pair:
            If the trade needs to be routed through a intermediary pool, e.g.
            BUSD -> BNB -> Cake.
        :return:
            List of prepared transactions to make this trade.
            These transactions, like approve() may relate to the earlier
            transactions in the `routing_state`.
        """

        assert type(reserve_asset_amount) == int
        assert max_slippage is not None, "Max slippage must be given"
        assert type(max_slippage) == float
        assert reserve_asset_amount > 0, f"For sells, switch reserve_asset to different token. Got target_pair: {target_pair}, reserve_asset: {reserve_asset}, amount: {reserve_asset_amount}"

        # Our reserves match directly the asset on trading pair
        # -> we can do one leg trade
        if not intermediary_pair:
            if target_pair.quote == reserve_asset or target_pair.base == reserve_asset:
                return self.make_direct_trade(
                    routing_state,
                    target_pair,
                    reserve_asset,
                    reserve_asset_amount,
                    max_slippage=max_slippage,
                    check_balances=check_balances,
                )
            raise RuntimeError(f"Do not how to trade reserve {reserve_asset} with {target_pair}")
        else:

            assert intermediary_pair.pool_address.lower() in self.allowed_intermediary_pairs.values(), f"Does not how to trade a pair. Got intermediary pair {intermediary_pair} that is not allowed, allowed intermediary pairs are {self.allowed_intermediary_pairs}"

            return self.make_multihop_trade(
                routing_state,
                target_pair,
                intermediary_pair,
                reserve_asset,
                reserve_asset_amount,
                max_slippage=max_slippage,
                check_balances=check_balances,
            )

    def route_pair(self, pair_universe: PandasPairUniverse, trading_pair: TradingPairIdentifier) -> Tuple[TradingPairIdentifier, Optional[TradingPairIdentifier]]:
        """Return Uniswap routing information (path components) for a trading pair.

        For three-way pairs, figure out the intermedia step.

        :return:
            (router address, target pair, intermediate pair) tuple
        """

        assert isinstance(trading_pair, TradingPairIdentifier)

        reserve_asset = self.get_reserve_asset(pair_universe)

        # We can directly do a two-way trade
        if trading_pair.quote == reserve_asset:
            return trading_pair, None

        # Only issue for legacy code
        assert pair_universe, "PairUniverse must be given so that we know how to route three way trades"

        # Try to find a mid-hop pool for the trade
        intermediate_pair_contract_address = self.allowed_intermediary_pairs.get(trading_pair.quote.address.lower())

        if not intermediate_pair_contract_address:
            raise CannotRouteTrade(f"Does not know how to trade pair {trading_pair} - supported intermediate tokens are {list(self.allowed_intermediary_pairs.keys())}")

        dex_pair = pair_universe.get_pair_by_smart_contract(intermediate_pair_contract_address)
        assert dex_pair is not None, f"Pair universe did not contain pair for a pair contract address {intermediate_pair_contract_address}, quote token is {trading_pair.quote}"

        if intermediate_pair := translate_trading_pair(dex_pair):
            return trading_pair, intermediate_pair
        else:
            raise CannotRouteTrade(f"Universe does not have a trading pair with smart contract address {intermediate_pair_contract_address}")

    def route_trade(self, pair_universe: PandasPairUniverse, trade: TradeExecution) -> Tuple[TradingPairIdentifier, Optional[TradingPairIdentifier]]:
        """Figure out how to map an abstract trade to smart contracts.

        Decide if we can do a direct trade in the pair pool.
        or if we need to hop through another pool to buy the token we want to buy.

        :return:
            target pair, intermediary pair tuple
        """
        return self.route_pair(pair_universe, trade.pair)

    def execute_trades_internal(self,
                       pair_universe: PandasPairUniverse,
                       routing_state: UniswapV3RoutingState,
                       trades: List[TradeExecution],
                       check_balances=False):
        """Split for testability.

        :param check_balances:
            Check that the wallet has enough reserves to perform the trades
            before executing them. Because we are selling before buying.
            sometimes we do no know this until the sell tx has been completed.

        :param max_slippage:
            The max slipppage tolerated before the trade fails.
            0.01 is 1%.
        """

        # Watch out for executing trade twice

        txs: List[BlockchainTransaction] = []

        reserve_asset = self.get_reserve_asset(pair_universe)

        for t in trades:
            assert len(t.blockchain_transactions) == 0, f"Trade {t} had already blockchain transactions associated with it"

            target_pair, intermediary_pair = self.route_trade(pair_universe, t)

            if intermediary_pair is None:
                # Two way trade
                # Decide betwen buying and selling
                trade_txs = (
                    self.trade(
                        routing_state,
                        target_pair=target_pair,
                        reserve_asset=reserve_asset,
                        reserve_asset_amount=t.get_raw_planned_reserve(),
                        check_balances=check_balances,
                    )
                    if t.is_buy()
                    else self.trade(
                        routing_state,
                        target_pair=target_pair,
                        reserve_asset=target_pair.base,
                        reserve_asset_amount=-t.get_raw_planned_quantity(),
                        check_balances=check_balances,
                    )
                )
            elif t.is_buy():
                trade_txs = self.trade(
                    routing_state,
                    target_pair=target_pair,
                    reserve_asset=reserve_asset,
                    reserve_asset_amount=t.get_raw_planned_reserve(),
                    check_balances=check_balances,
                    intermediary_pair=intermediary_pair,
                )
            else:
                trade_txs = self.trade(
                    routing_state,
                    target_pair=target_pair,
                    reserve_asset=target_pair.base,
                    reserve_asset_amount=-t.get_raw_planned_quantity(),
                    check_balances=check_balances,
                    intermediary_pair=intermediary_pair,
                )

            t.set_blockchain_transactions(trade_txs)
            txs += trade_txs

        # Now all trades have transactions associated with them.
        # We can start to execute transactions.

    def setup_trades(self,
                     routing_state: UniswapV3RoutingState,
                     trades: List[TradeExecution],
                     check_balances=False):
        """Strategy and live execution connection.

        Turns abstract strategy trades to real blockchain transactions.

        - Modifies TradeExecution objects in place and associates a blockchain transaction for each

        - Signs tranactions from the hot wallet and broadcasts them to the network

        :param check_balances:
            Check that the wallet has enough reserves to perform the trades
            before executing them. Because we are selling before buying.
            sometimes we do no know this until the sell tx has been completed.


        :param max_slippage:
            Max slippaeg tolerated per trade. 0.01 is 1%.

        """
        return self.execute_trades_internal(routing_state.pair_universe, routing_state, trades, check_balances)

    def create_routing_state(self,
                             universe: StrategyExecutionUniverse,
                             execution_details: dict) -> UniswapV3RoutingState:
        """Create a new routing state for this cycle.

        - Connect routing to web3 and hot wallet

        - Read on-chain data on what gas fee we are going to use

        - Setup transaction builder based on this information
        """

        assert isinstance(universe, TradingStrategyUniverse)
        assert universe is not None, "Universe is required"
        assert universe.universe.pairs is not None, "Pairs are required"

        web3 = execution_details["web3"]
        hot_wallet = execution_details["hot_wallet"]

        fees = estimate_gas_fees(web3)

        logger.info("Gas fee estimations for chain %d: %s", web3.eth.chain_id, fees)

        logger.info("Estimated gas fees for chain %d: %s", web3.eth.chain_id, fees)
        tx_builder = TransactionBuilder(web3, hot_wallet, fees)
        routing_state = UniswapV3RoutingState(universe.universe.pairs, tx_builder)
        return routing_state

    def perform_preflight_checks_and_logging(self,
        routing_state: UniswapV3RoutingState,
        pair_universe: PandasPairUniverse):
        """"Checks the integrity of the routing.

        - Called from check-wallet to see our routing and balances are good
        """

        logger.info("Routing details")
        for factory, router in self.address_map.items():
            logger.info("  Factory %s uses router %s", factory, router[0])

        reserve = self.get_reserve_asset(pair_universe)
        logger.info("  Routed reserve asset is %s", reserve)
        

def get_uniswap_for_pair(web3: Web3, address_map: dict, target_pair: TradingPairIdentifier) -> UniswapV3Deployment:
    """Get a router for a trading pair."""
    assert target_pair.exchange_address, f"Exchange address missing for {target_pair}"
    
    factory_address = Web3.toChecksumAddress(target_pair.exchange_address)
    assert factory_address == Web3.toChecksumAddress(address_map["factory"]), "address_map[\"factory\"] and target_pair.exchange_address should be equal"
    
    router_address = Web3.toChecksumAddress(address_map["router"])
    position_manager_address = Web3.toChecksumAddress(address_map["position_manager"])
    quoter_address = Web3.toChecksumAddress(address_map["quoter"])

    try:
        return fetch_deployment(
            web3,
            factory_address,
            router_address,
            position_manager_address,
            quoter_address
        )
    except ContractLogicError as e:
        raise RuntimeError(f"Could not fetch deployment data for router address {router_address} (factory {factory_address}) - data is likely wrong") from e
