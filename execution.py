# Import modules
from pricing import Pricing
import abis
import config
import decouple
from web3 import Web3
import requests
import pycoingecko
import time
import subprocess
import sys


class ArbBotBase(Pricing):
    def __init__(
        self,
        mode,
        chain_id,
        w3_endpoint,
        zero_ex_base_url,
        exchanges,
        index_address,
        index_symbol,
        index_router_address,
        wrapped_native_address,
        profit_threshold,
    ):
        self.mode = None
        self.chain_id = chain_id
        self.mainnet_endpoint = w3_endpoint
        self.zero_ex_base_url = zero_ex_base_url
        self.index_address = index_address
        self.exchange_addresses = exchanges
        self.index_router_address = index_router_address
        self.wrapped_native_address = wrapped_native_address
        self.index_symbol = index_symbol
        self.w3 = None
        self.account = None
        self.address = None
        self.private_key = None
        self.cg = pycoingecko.CoinGeckoAPI()
        self.index_token_contract = None
        self.index_router_contract = None
        self.wrapped_native_contract = None
        super().__init__(
            self.cg, self.w3, self.index_address, self.chain_id, wrapped_native_address
        )
        self.change_mode(mode, False)
        self.change_mode(0, True, self.create_contract_instances)
        self.profit_threshold = self.w3.to_wei(profit_threshold, "ether")

    # Function that retrieves the correct swapping function based on the pool address
    def func_for_exchange(self, pool):
        mapping = {
            43114: {
                "0xE5e9d67e93aD363a50cABCB9E931279251bBEFd0": self.swap_via_trader_joe,
                "0x2219bc1C06e303172d35deEB9C637D074BA4F277": self.swap_via_trader_joe,
            },
            1: {"0xF5FE7ea8537CBd9E5e7b81A93828F48037D220c2": self.swap_via_uniswap},
        }
        return mapping[self.chain_id][pool]

    # Function allows the bot to switch between a local forked network or mainnet. 
    def change_mode(self, new_mode, switch_back, contract_func=None, *args):
        initial_mode = self.mode
        if new_mode == self.mode:
            return contract_func(*args)
        else:
            self.mode = new_mode
            if self.mode == 0:
                # Set up web3 object
                self.w3 = Web3(
                    Web3.HTTPProvider(
                        endpoint_uri=decouple.config("GANACHE_MAINNET_FORK"),
                        request_kwargs={"timeout": 60},
                    )
                )
                super().update_w3_provider(self.w3)
                # Importing account
                self.account = self.w3.eth.account.from_key(
                    decouple.config("GANACHE_FORK_PK")
                )
                # Storing address
                self.address = self.account.address
                # Importing private key
                self.private_key = decouple.config("GANACHE_FORK_PK")
                # Setting default account
                self.w3.eth.default_account = self.address
                self.create_contract_instances()
                if contract_func is not None:
                    response = contract_func(*args)
                if switch_back:
                    self.change_mode(initial_mode, False)
                return response if contract_func is not None else None
            elif self.mode == 1:
                # Set up web3 object
                self.w3 = Web3(
                    Web3.HTTPProvider(
                        endpoint_uri=self.mainnet_endpoint,
                        request_kwargs={"timeout": 60},
                    )
                )
                super().update_w3_provider(self.w3)
                # Importing account
                self.account = self.w3.eth.account.from_key(
                    decouple.config("PROD_ACCOUNT_PK")
                )
                # Storing address
                self.address = self.account.address
                # Importing private key
                self.private_key = decouple.config("PROD_ACCOUNT_PK")
                # Setting default account
                self.w3.eth.default_account = self.address
                self.create_contract_instances()
                if contract_func is not None:
                    response = contract_func(*args)
                if switch_back:
                    self.change_mode(initial_mode, False)
                return response if contract_func is not None else None

    def start_ganache_node(self, gas_price):
        # Spin up a daemon version of ganche
        sub_process_response = subprocess.run(
            f"npx ganache --fork {self.mainnet_endpoint} -q -g {gas_price} --wallet.deterministic --detach -e 100000",
            shell=True,
            capture_output=True,
            text=True,
        )
        return sub_process_response.stdout.strip()

    def kill_ganache_node(self, instance_name):
        subprocess.run(["npx", "ganache", "instances", "stop", str(instance_name)])

    # Retrieves list of active index assets
    def get_index_anatomy(self):
        # Retrieve index anatomy
        assets, weights = self.index_token_contract.functions.anatomy().call()
        # Zip asset and weights lists together
        zipped_list = list(zip(assets, weights))
        return zipped_list

    def get_inactive_assets(self):
        inactive_assets = self.index_token_contract.functions.inactiveAnatomy().call()
        return inactive_assets

    # Amount should be in wei
    def mint(self, amount, flag="CALL"):
        recipient = self.address
        # Retrieve index anatomy
        anatomy = self.get_index_anatomy()
        # Array to hold each quote
        mint_quote_params = []
        # Keep track of residual eth amount to ensure the amount of eth traded equals the amount of eth sent
        amount_remaining = amount
        # Retrieve quote for each asset from 0x API
        for asset, weight in anatomy:
            # Sleep to avoid API rate limiting
            time.sleep(1)
            query_params = {
                "enableSlippageProtection": "true",
                "sellToken": self.wrapped_native_address,
                "slippagePercentage": 0.99
                if self.mode == 0
                else config.slippage_threshold,
            }
            query_params["buyToken"] = asset
            # If this is the last asset in the list use the remaining eth as the sell amount
            if asset != self.w3.to_checksum_address(self.wrapped_native_address):
                query_params["sellAmount"] = (
                    amount_remaining
                    if asset == anatomy[-1][0]
                    else int(amount * weight / 255)
                )
                amount_remaining -= query_params["sellAmount"]
            else:
                # Directly create quote bypassing 0x API request since no swap is required
                quote = {
                    "asset": self.w3.to_checksum_address(self.wrapped_native_address),
                    "buyAssetMinAmount": amount_remaining
                    if asset == anatomy[-1][0]
                    else int(amount * weight / 255),
                    "swapTarget": self.w3.to_checksum_address(
                        "0x0000000000000000000000000000000000000000"
                    ),
                    "assetQuote": self.w3.to_checksum_address(
                        "0x0000000000000000000000000000000000000000"
                    ),
                }
                amount_remaining -= quote["buyAssetMinAmount"]
                mint_quote_params.append(quote)
                continue
            # Query 0x API
            zero_ex_quote = requests.get(
                self.zero_ex_base_url,
                query_params,
                headers={"0x-api-key": decouple.config("ZERO_X_KEY")},
            )
            zero_ex_quote = zero_ex_quote.json()
            # Calculate minumum buy amount using guaranteed price
            buy_asset_min_amount = int(
                int(zero_ex_quote["sellAmount"])
                * float(zero_ex_quote["guaranteedPrice"])
            )
            # Create quote dict for MintQuoteParams struct and append to mint_quote_params list
            quote = {
                "asset": self.w3.to_checksum_address(zero_ex_quote["buyTokenAddress"]),
                "swapTarget": self.w3.to_checksum_address(zero_ex_quote["to"]),
                "buyAssetMinAmount": 0 if self.mode == 0 else buy_asset_min_amount,
                "assetQuote": zero_ex_quote["data"],
            }
            mint_quote_params.append(quote)
        # Create dict with all data required for MintSwapValueParams struct
        mint_swap_value_params = {
            "index": self.w3.to_checksum_address(self.index_address),
            "recipient": recipient,
            "quotes": mint_quote_params,
        }
        # Determine desired return outcome based on flag
        if flag.upper() == "CALL":
            # Get output from call static
            mint_swap_value_params["inputToken"] = self.w3.to_checksum_address(
                self.wrapped_native_address
            )
            mint_swap_value_params["amountInInputToken"] = amount
            call_static = self.index_router_contract.functions.mintSwapValue(
                mint_swap_value_params
            ).call({"value": amount})
            gas_estimate = int(
                self.index_router_contract.functions.mintSwapValue(
                    mint_swap_value_params
                ).estimate_gas({"value": amount})
                * config.gas_multiplier
            )
            return {"output": call_static, "gas": gas_estimate}
        elif flag.upper() == "BUILD":
            # Build transaction dictionary
            built_transaction = self.index_router_contract.functions.mintSwapValue(
                mint_swap_value_params
            ).build_transaction(
                {
                    "value": amount,
                    "nonce": self.w3.eth.get_transaction_count(self.address),
                    "gas": int(
                        self.index_router_contract.functions.mintSwapValue(
                            mint_swap_value_params
                        ).estimate_gas({"value": amount})
                        * config.gas_multiplier
                    ),
                }
            )
            return built_transaction

    # Index amount should be in wei
    def burn(self, index_amount, flag="CALL"):
        recipient = self.address
        # Retrieve active assets
        assets = self.get_index_anatomy()
        # Remove weights and add inactive assets
        assets = [i[0] for i in assets] + self.get_inactive_assets()
        # Retrieve sell amounts of constituent based on the amount of index tokens burned
        constituent_sell_amounts = (
            self.index_router_contract.functions.burnTokensAmount(
                self.w3.to_checksum_address(self.index_address), index_amount
            ).call()
        )
        # Check that both lists are the same length
        assert len(assets) == len(constituent_sell_amounts)
        zipped_list = list(zip(assets, constituent_sell_amounts))
        # List to hold BurnQuoteParams structs
        burn_quote_params = []
        for asset, sell_amount in zipped_list:
            # Sleep to avoid API rate limiting
            time.sleep(1)
            if sell_amount == 0 or asset == self.wrapped_native_address:
                quote = {
                    "swapTarget": self.w3.to_checksum_address(
                        "0x0000000000000000000000000000000000000000"
                    ),
                    "buyAssetMinAmount": 0,
                    "assetQuote": "0x",
                }
                burn_quote_params.append(quote)
            else:
                query_params = {
                    "enableSlippageProtection": "true",
                    "slippagePercentage": 0.99
                    if self.mode == 0
                    else config.slippage_threshold,
                    "buyToken": self.wrapped_native_address,
                    "sellToken": asset,
                    "sellAmount": int(sell_amount * 0.99999),
                }
                # Query 0x api
                zero_ex_quote = requests.get(
                    self.zero_ex_base_url,
                    query_params,
                    headers={"0x-api-key": decouple.config("ZERO_X_KEY")},
                )
                zero_ex_quote = zero_ex_quote.json()
                # Calculate minumum buy amount using guaranteed price
                buy_asset_min_amount = (
                    0
                    if self.mode == 0
                    else int(
                        int(zero_ex_quote["sellAmount"])
                        * float(zero_ex_quote["guaranteedPrice"])
                    )
                )
                # Create quote dict for BurnQuoteParams struct and append to burn_quote_params list
                quote = {
                    "swapTarget": self.w3.to_checksum_address(zero_ex_quote["to"]),
                    "buyAssetMinAmount": buy_asset_min_amount,
                    "assetQuote": zero_ex_quote["data"],
                }
                burn_quote_params.append(quote)
        # Create dict with all data required for BurnSwapParams struct
        burn_swap_params = {
            "index": self.w3.to_checksum_address(self.index_address),
            "amount": index_amount,
            "outputAsset": self.w3.to_checksum_address(self.wrapped_native_address),
            "recipient": recipient,
            "quotes": burn_quote_params,
        }
        # Determine desired return outcome based on flag
        if flag.upper() == "CALL":
            # Call static burn to get output
            call_static = self.index_router_contract.functions.burnSwapValue(
                burn_swap_params
            ).call()
            gas_estimate = int(
                self.index_router_contract.functions.burnSwapValue(
                    burn_swap_params
                ).estimate_gas()
                * config.gas_multiplier
            )
            return {"output": call_static, "gas": gas_estimate}
        elif flag.upper() == "BUILD":
            # Build transaction dictionary
            built_transaction = self.index_router_contract.functions.burnSwapValue(
                burn_swap_params
            ).build_transaction(
                {
                    "nonce": self.w3.eth.get_transaction_count(self.address),
                    "maxFeePerGas": int(self.w3.eth.gas_price * 10)
                    if self.mode == 0
                    else int(self.w3.eth.gas_price * config.gas_multiplier),
                    "gas": int(
                        self.index_router_contract.functions.burnSwapValue(
                            burn_swap_params
                        ).estimate_gas()
                        * config.gas_multiplier
                    ),
                }
            )
            return built_transaction

    def execute_transaction(self, transaction):
        # Sign transaction
        signed_transaction = self.w3.eth.account.sign_transaction(
            transaction, self.private_key
        )
        # Send to network
        transaction_hash = self.w3.eth.send_raw_transaction(
            signed_transaction.rawTransaction
        )
        # Wait for reciept confirmation
        receipt = self.w3.eth.wait_for_transaction_receipt(transaction_hash)
        if receipt["status"] == 0:
            response_dict = {"status": False, "response": receipt}
            print(response_dict)
            return response_dict
        else:
            response_dict = {"status": True, "response": receipt}
            return response_dict

    def set_allowances(self, amount, contract_object, owner, spender):
        amount = 2**256 - 1 if amount == "inf" else amount
        # Build transaction to set allowance to infinity
        build_transaction = contract_object.functions.approve(
            spender, 2**256 - 1
        ).build_transaction({"nonce": self.w3.eth.get_transaction_count(self.address)})
        return build_transaction

    def preflight_checks(self):
        # Run these transactions to set up forked environment - (allowances and asset balances)
        self.execute_transaction(
            self.set_allowances(
                "inf",
                self.index_token_contract,
                self.address,
                self.index_router_address,
            )
        )
        if self.chain_id == 1:
            self.execute_transaction(
                self.mint(self.w3.to_wei(10, "ether"), flag="BUILD")
            )
            self.execute_transaction(
                self.set_allowances(
                    "inf",
                    self.index_token_contract,
                    self.address,
                    self.w3.to_checksum_address(config.uniswap_swap_router),
                )
            )
            self.execute_transaction(
                self.wrapped_native(self.w3.to_wei(25, "ether"), True)
            )
        elif self.chain_id == 43114:
            self.execute_transaction(
                self.mint(self.w3.to_wei(20000, "ether"), flag="BUILD")
            )
            self.execute_transaction(
                self.wrapped_native(self.w3.to_wei(3000, "ether"), True)
            )
            self.execute_transaction(
                self.set_allowances(
                    "inf",
                    self.index_token_contract,
                    self.address,
                    config.trader_joe_router,
                )
            )

    def set_allowances_prod(self):
        self.execute_transaction(
            self.set_allowances(
                "inf",
                self.index_token_contract,
                self.address,
                self.index_router_address,
            )
        )
        if self.chain_id == 1:
            self.execute_transaction(
                self.set_allowances(
                    "inf",
                    self.index_token_contract,
                    self.address,
                    self.w3.to_checksum_address(config.uniswap_swap_router),
                )
            )
        elif self.chain_id == 43114:
            self.execute_transaction(
                self.set_allowances(
                    "inf",
                    self.index_token_contract,
                    self.address,
                    config.trader_joe_router,
                )
            )

    def create_contract_instances(self):
        self.index_token_contract = self.w3.eth.contract(
            self.index_address, abi=abis.pdi_token
        )
        self.index_router_contract = self.w3.eth.contract(
            self.index_router_address, abi=abis.index_router
        )
        self.wrapped_native_contract = self.w3.eth.contract(
            address=self.wrapped_native_address, abi=abis.weth
        )

    def retrieve_index_balance(self, wei):
        assert type(wei) == bool, "Param should be bool"
        if wei:
            return self.index_token_contract.functions.balanceOf(self.address).call()
        else:
            return float(
                self.w3.from_wei(
                    self.index_token_contract.functions.balanceOf(self.address).call(),
                    "ether",
                )
            )

    def calculate_trade_size(self, premium, exchange):
        assert type(premium) == bool, "Premium must be bool"
        if premium:
            # Retrieve the correct function for calcualting the optimal trade size based on the pool address
            calc_trade_size_func = self.get_trade_size_mapping(exchange)
            # Get the function used to execute trades on the given pool address
            trading_func = self.func_for_exchange(exchange)
            # Get the optimal trade size
            trade_size = calc_trade_size_func(
                exchange,
                self.get_nav_price(self.get_price_mapping(exchange)[0]),
                premium,
            )
            print(
                f"{trade_size/1e18} {self.native_asset_symbol()} worth of {self.index_symbol} must be minted"
            )
            # Call the mint and swap functions to assess profit
            index_bought, gas_leg_1 = self.mint(trade_size).values()
            native_received, gas_leg_2 = trading_func(
                exchange, index_bought, False
            ).values()
            print(
                f"Estimate of {self.native_asset_symbol()} received (typically understated): {native_received/1e18}"
            )
            # Ensure profit from arb is greater than gas costs
            is_profitable = (
                native_received
                - self.estimate_gas_costs(gas_leg_1, gas_leg_2)
                - trade_size
            )
            if is_profitable < self.profit_threshold:
                print(
                    f"Arb not profitable enough using {trade_size/1e18} {self.native_asset_symbol()} with an expected output of {is_profitable/1e18}"
                )
                return 0
            else:
                print(
                    f"Arb is profitable using {trade_size/1e18} {self.native_asset_symbol()} with a profit of {is_profitable/1e18}"
                )
                return trade_size
        else:
            # Retrieve the correct function for calcualting the optimal trade size based on the pool address
            calc_trade_size_func = self.get_trade_size_mapping(exchange)
            # Get the function used to execute trades on the given pool address
            trading_func = self.func_for_exchange(exchange)
            # Get the optimal trade size
            trade_size = calc_trade_size_func(
                exchange,
                self.get_nav_price(self.get_price_mapping(exchange)[0]),
                premium,
            )
            print(f"Optimal swap is {trade_size/1e18} {self.native_asset_symbol()}")
            # Call the swap and burn functions to assess profit
            index_bought, gas_leg_1 = trading_func(exchange, trade_size, True).values()
            native_received, gas_leg_2 = self.burn(index_bought).values()
            print(
                f"Estimate of {self.native_asset_symbol()} received (typically understated): {native_received/1e18}"
            )
            # Ensure profit from arb is greater than gas costs
            is_profitable = (
                native_received
                - self.estimate_gas_costs(gas_leg_1, gas_leg_2)
                - trade_size
            )
            if is_profitable < self.profit_threshold:
                print(
                    f"Arb not profitable enough using {trade_size/1e18} {self.native_asset_symbol()} with an expected output of {is_profitable/1e18}"
                )
                return 0
            else:
                print(
                    f"Arb is profitable using {trade_size/1e18} {self.native_asset_symbol()} with a profit of {is_profitable/1e18}"
                )
                return trade_size

    def estimate_gas_costs(self, leg_1, leg_2):
        gas_cost_in_eth = (leg_1 + leg_2) * self.w3.eth.gas_price / 1e18
        return gas_cost_in_eth

    # Function for wrapping and unwrapping the native token
    def wrapped_native(self, amount, is_wrap):
        assert type(is_wrap) == bool, "Please enter a bool"
        if is_wrap:
            return self.wrapped_native_contract.functions.deposit().build_transaction(
                {
                    "value": amount,
                    "nonce": self.w3.eth.get_transaction_count(self.address),
                }
            )
        else:
            return self.wrapped_native_contract.functions.withdraw(
                amount
            ).build_transaction(
                {"nonce": self.w3.eth.get_transaction_count(self.address)}
            )

    def get_total_native_balance(self, wei):
        assert type(wei) == bool, "Param should be bool"
        if wei:
            native = self.w3.eth.get_balance(self.address)
            wrapped = self.wrapped_native_contract.functions.balanceOf(
                self.address
            ).call()
        else:
            native = self.w3.from_wei(self.w3.eth.get_balance(self.address), "ether")
            wrapped = self.w3.from_wei(
                self.wrapped_native_contract.functions.balanceOf(self.address).call(),
                "ether",
            )
        return native + wrapped

    def swap_via_uniswap(self, pool, amount, is_buy, flag="CALL"):
        assert type(is_buy) == bool,"Param should be bool"
        # Instantiate contract address
        uniswap_router_contract = self.w3.eth.contract(
            address=config.uniswap_swap_router, abi=abis.uniswap_swap_router
        )
        # Create struct
        exact_input_single_params = {}
        exact_input_single_params["tokenIn"] = (
            self.wrapped_native_address if is_buy else self.index_address
        )
        exact_input_single_params["tokenOut"] = (
            self.index_address if is_buy else self.wrapped_native_address
        )
        exact_input_single_params["fee"] = int(3000)
        exact_input_single_params["recipient"] = self.address
        exact_input_single_params["deadline"] = (
            self.w3.eth.get_block("latest")["timestamp"] + 10000
        )
        exact_input_single_params["amountIn"] = int(amount)
        exact_input_single_params["amountOutMinimum"] = 0
        exact_input_single_params["sqrtPriceLimitX96"] = 0
        if flag.upper() == "CALL":
            # Call static for output 
            call_static = uniswap_router_contract.functions.exactInputSingle(
                exact_input_single_params
            ).call({"value": amount if is_buy else 0})
            gas_estimate = int(
                uniswap_router_contract.functions.exactInputSingle(
                    exact_input_single_params
                ).estimate_gas({"value": amount if is_buy else 0})
                * config.zero_ex_multiplier
            )
            return {"output": call_static, "gas": gas_estimate}
        elif flag.upper() == "BUILD":
            built_transaction = uniswap_router_contract.functions.exactInputSingle(
                exact_input_single_params
            ).build_transaction(
                {
                    "value": amount if is_buy else 0,
                    "nonce": self.w3.eth.get_transaction_count(self.address),
                    "gas": int(
                        uniswap_router_contract.functions.exactInputSingle(
                            exact_input_single_params
                        ).estimate_gas({"value": amount if is_buy else 0})
                        * config.zero_ex_multiplier
                    ),
                }
            )
            return built_transaction

    def swap_via_trader_joe(self, pool, amount, is_buy, flag="CALL"):
        assert type(is_buy) == bool,"Param should be bool"
        # Instantiate contract instance
        trader_joe_router = self.w3.eth.contract(
            address=config.trader_joe_router, abi=abis.trader_joe_router
        )
        # Create params and struct
        amountOutMin = 0
        path = {
            "pairBinSteps": [50] if pool == config.cai_tj_v2_pool else [0],
            "versions": [2] if pool == config.cai_tj_v2_pool else [0],
            "tokenPath": [self.wrapped_native_address, self.index_address]
            if is_buy
            else [self.index_address, self.wrapped_native_address],
        }
        to = self.address
        deadline = self.w3.eth.get_block("latest")["timestamp"] + 10000
        if is_buy:
            if flag.upper() == "CALL":
                # Call static for output
                call_static = trader_joe_router.functions.swapExactNATIVEForTokens(
                    amountOutMin, path, to, deadline
                ).call({"value": amount})
                gas_estimate = int(
                    trader_joe_router.functions.swapExactNATIVEForTokens(
                        amountOutMin, path, to, deadline
                    ).estimate_gas({"value": amount})
                    * config.zero_ex_multiplier
                )
                return {"output": call_static, "gas": gas_estimate}
            elif flag.upper() == "BUILD":
                built_transaction = (
                    trader_joe_router.functions.swapExactNATIVEForTokens(
                        amountOutMin, path, to, deadline
                    ).build_transaction(
                        {
                            "value": amount,
                            "nonce": self.w3.eth.get_transaction_count(self.address),
                            "gas": int(
                                trader_joe_router.functions.swapExactNATIVEForTokens(
                                    amountOutMin, path, to, deadline
                                ).estimate_gas({"value": amount if is_buy else 0})
                                * config.zero_ex_multiplier
                            ),
                        }
                    )
                )
                return built_transaction
        else:
            if flag.upper() == "CALL":
                # Call static for output
                call_static = trader_joe_router.functions.swapExactTokensForNATIVE(
                    amount, amountOutMin, path, to, deadline
                ).call()
                gas_estimate = int(
                    trader_joe_router.functions.swapExactTokensForNATIVE(
                        amount, amountOutMin, path, to, deadline
                    ).estimate_gas()
                    * config.zero_ex_multiplier
                )
                return {"output": call_static, "gas": gas_estimate}
            elif flag.upper() == "BUILD":
                built_transaction = (
                    trader_joe_router.functions.swapExactTokensForNATIVE(
                        amount, amountOutMin, path, to, deadline
                    ).build_transaction(
                        {
                            "nonce": self.w3.eth.get_transaction_count(self.address),
                            "gas": int(
                                trader_joe_router.functions.swapExactTokensForNATIVE(
                                    amount, amountOutMin, path, to, deadline
                                ).estimate_gas()
                                * config.zero_ex_multiplier
                            ),
                        }
                    )
                )
                return built_transaction

    def get_gas_price(self):
        return self.w3.eth.gas_price

    def select_arb_type(self, exchange_address):
        # Get delta between NAV and exchange price
        delta = super().get_price_delta(exchange_address)
        print(f"Current price delta is {delta}")
        if delta > 0:
            # Check profitability and trade size. Switch mode to simulate tx on forked network.
            trade_size = self.change_mode(
                0, True, self.calculate_trade_size, True, exchange_address
            )
            if trade_size == 0:
                return
            # Get native asset balance before arb
            native_balance_before = self.get_total_native_balance(True)
            # Get index balance before
            index_balance_before = self.retrieve_index_balance(True)
            # Execute mint transaction
            tx1 = self.execute_transaction(self.mint(trade_size, flag="build"))
            if tx1["status"] == False:
                return
            # Retrieve index balance
            index_balance_after = self.retrieve_index_balance(True)
            # Calculate net index tokens gained
            diff = index_balance_after - index_balance_before
            # Execute swap transaction
            trading_func = self.func_for_exchange(exchange_address)
            tx2 = self.execute_transaction(
                trading_func(exchange_address, diff, False, "build")
            )
            if tx2["status"] == False:
                return
            # Get native asset balance after arb
            native_balance_after = self.get_total_native_balance(True)
            profit = native_balance_after - native_balance_before
            return profit
        elif delta < 0:
            # Check profitability and trade size. Switch mode to simulate tx on forked network
            trade_size = self.change_mode(
                0, True, self.calculate_trade_size, False, exchange_address
            )
            if trade_size == 0:
                return
            # Get native asset balance before arb
            native_balance_before = self.get_total_native_balance(True)
            # Get index balance before
            index_balance_before = self.retrieve_index_balance(True)
            # Execute swap transaction
            trading_func = self.func_for_exchange(exchange_address)
            tx1 = self.execute_transaction(
                trading_func(exchange_address, trade_size, True, "build")
            )
            if tx1["status"] == False:
                return
            # Retrieve index balance after
            index_balance_after = self.retrieve_index_balance(True)
            # Calculate net index tokens gained
            diff = index_balance_after - index_balance_before
            # Execute burn transaction
            tx2 = self.execute_transaction(self.burn(diff, flag="build"))
            if tx2["status"] == False:
                return
            # Get native balance after arb
            native_balance_after = self.get_total_native_balance(True)
            profit = native_balance_after - native_balance_before
            return profit

    def query_arb(self):
        print(
            f"Starting {self.index_symbol} arb bot in {'dev mode' if self.mode == 0 else 'production mode' }"
        )
        gas_price = self.change_mode(1, True, self.get_gas_price)
        print(f"Current gas price is {gas_price/1e9}")
        print("Spinning up ganache mainnet fork instance")
        ganache_instance = self.start_ganache_node(gas_price)
        self.change_mode(0, True, self.preflight_checks)
        for exchange in self.exchange_addresses:
            print(f"Assessing {exchange} on chain id {self.chain_id}")
            arb = self.select_arb_type(exchange)
            if arb is not None:
                print(
                    f"Arb on exchange {exchange} was successful with a profit of {arb/1e18} {self.native_asset_symbol()}"
                )
            else:
                print(f"Arb on exchange {exchange} was unsuccessful")
        print("Killing ganache mainnet fork instance")
        self.kill_ganache_node(ganache_instance)


while True:
    if sys.argv[1] == "dev":
        # Create instances of the arb bot that run on the local forked network
        arb_bot_avax = ArbBotBase(
            0,
            43114,
            decouple.config("AVALANCHE_INFURA_URL"),
            config.zero_ex_base_url_avax,
            [config.cai_tj_v1_pool, config.cai_tj_v2_pool],
            config.cai_address,
            "CAI",
            config.index_router_avax,
            config.wavax,
            1,
        )
        arb_bot_eth = ArbBotBase(
            0,
            1,
            decouple.config("ETHEREUM_INFURA_URL"),
            config.zero_ex_base_url,
            [config.pdi_weth_pool],
            config.pdi_address,
            "PDI",
            config.index_router,
            config.weth,
            0.02,
        )
        arb_bot_eth.query_arb()
        arb_bot_avax.query_arb()
        print("Arb bot will retry in 60 minutes")
        time.sleep(3600)
    elif sys.argv[1] == "prod":
        # Create instances of the arb bot that run on mainnet
        arb_bot_avax = ArbBotBase(
            1,
            43114,
            decouple.config("AVALANCHE_INFURA_URL"),
            config.zero_ex_base_url_avax,
            [config.cai_tj_v1_pool, config.cai_tj_v2_pool],
            config.cai_address,
            "CAI",
            config.index_router_avax,
            config.wavax,
            0.5,
        )
        arb_bot_eth = ArbBotBase(
            1,
            1,
            decouple.config("ETHEREUM_INFURA_URL"),
            config.zero_ex_base_url,
            [config.pdi_weth_pool],
            config.pdi_address,
            "PDI",
            config.index_router,
            config.weth,
            0.02,
        )
        arb_bot_avax.query_arb()
        arb_bot_eth.query_arb()
        print("Arb bot will retry in 60 minutes")
        time.sleep(3600)
