# Import modules
import abis
import config
import requests
import math

# Pricing Class

class Pricing:
    def __init__(
        self, cg_provider, w3_provider, index_address, chain_id, wrapped_native_address
    ):
        self.cg = cg_provider
        self.w3 = w3_provider
        self.index_address = index_address
        self.wrapped_native_address = wrapped_native_address
        self.chain_id = chain_id

    def native_asset_symbol(self):
        if self.chain_id == 1:
            return "ETH"
        elif self.chain_id == 43114:
            return "AVAX"

    def update_w3_provider(self, new_provider):
        self.w3 = new_provider

    def get_price(self, asset):
        return float(self.cg.get_price(asset, "usd")[asset]["usd"])

    def get_native_price(self):
        if self.chain_id == 1:
            return self.get_price("ethereum")
        elif self.chain_id == 43114:
            return self.get_price("avalanche-2")

    def get_index_helper(self):
        mapping = {
            1: config.ethereum_index_helper,
            43114: config.avalanche_index_helper,
        }
        return mapping.get(self.chain_id)

    def get_price_mapping(self, pool):
        mapping = {
            43114: {
                "0xE5e9d67e93aD363a50cABCB9E931279251bBEFd0": [
                    "avalanche-2",
                    self.get_tj_v1_price,
                ],
                "0x2219bc1C06e303172d35deEB9C637D074BA4F277": [
                    "avalanche-2",
                    self.get_tj_v2_price,
                ],
            },
            1: {
                "0xF5FE7ea8537CBd9E5e7b81A93828F48037D220c2": [
                    "ethereum",
                    self.get_uniswap_v3_price,
                ]
            },
        }
        return mapping[self.chain_id][pool]

    def get_trade_size_mapping(self, pool):
        mapping = {
            43114: {
                "0xE5e9d67e93aD363a50cABCB9E931279251bBEFd0": self.calculate_tj_v1_trade_size,
                "0x2219bc1C06e303172d35deEB9C637D074BA4F277": self.calculate_tj_v2_trade_size,
            },
            1: {
                "0xF5FE7ea8537CBd9E5e7b81A93828F48037D220c2": self.calculate_uni_v3_trade_size
            },
        }
        return mapping[self.chain_id][pool]

    def get_uniswap_v3_price(self, pool, quote):
        # Create pool contract
        pool_contract = self.w3.eth.contract(address=pool, abi=abis.uniswap_v3_pool)
        # Retrieve slot 0 data
        (sqrt_price_x96, _, _, _, _, _, _) = pool_contract.functions.slot0().call()
        # Retrieve ethereum usd price
        quote_price = self.get_price(quote)
        # Compute price of PDI in usd
        token_0_price = (sqrt_price_x96**2) / (2**192) * quote_price
        return token_0_price

    def get_tj_v1_price(self, exchange, quote):
        pool_contract = self.w3.eth.contract(address=exchange, abi=abis.trader_joe_v1)
        x, y, _ = pool_contract.functions.getReserves().call()
        return y / x * self.get_price(quote)

    def get_tj_v2_price(self, pool, quote):
        pool_contract = self.w3.eth.contract(address=pool, abi=abis.trader_joe_v2)
        bin_id = pool_contract.functions.getActiveId().call()
        raw_price = pool_contract.functions.getPriceFromId(bin_id).call()
        return raw_price / (2**128) * self.get_price(quote)

    def tj_v2_get_swap_in(self, pool, amount, premium):
        assert type(premium) == bool, "is_buy param should be bool"
        pool_contract = self.w3.eth.contract(address=pool, abi=abis.trader_joe_v2)
        if premium:
            swap_in_amount, _, _ = pool_contract.functions.getSwapIn(
                amount, premium
            ).call()
            convert_to_native = (
                swap_in_amount * self.get_nav_price()
            ) / self.get_native_price()
            return int(convert_to_native)
        else:
            swap_in_amount, _, _ = pool_contract.functions.getSwapIn(
                amount, premium
            ).call()
            return int(swap_in_amount)

    def get_nav_price(self, currency="usd"):
        # Create index helper contract
        index_helper_contract = self.w3.eth.contract(
            address=self.get_index_helper(), abi=abis.index_helper
        )
        # Retrieve nav price
        _, price = index_helper_contract.functions.totalEvaluation(
            self.index_address
        ).call()
        # Divide by appropriate decimal places
        if currency == "usd":
            return price / config.index_helper_decimals
        else:
            return price / config.index_helper_decimals / self.get_price(currency)

    def get_price_delta(self, exchange):
        # Get correct function to price exchange
        quote, func = self.get_price_mapping(exchange)
        # Compute difference between nav price and exchange price
        delta = func(exchange, quote) / self.get_nav_price() - 1
        # If delta is positive then exchange price is at a premium. If negative then exchange price is at a discount.
        return delta

    def tick_to_price(self, tick):
        return 1.0001**tick

    # amount of x in range; sp - sqrt of current price, smax - sqrt of max price
    def x_in_range(self, L, sp, smax):
        return L * (smax - sp) / (sp * smax)

    # amount of y in range; sp - sqrt of current price, smin - sqrt of min price
    def y_in_range(self, L, sp, smin):
        return L * (sp - smin)

    def price_to_sqrt_price(self, price, quote_currency="eth"):
        if quote_currency == "eth":
            price = (
                price / self.cg.get_price("ethereum", "usd")["ethereum"]["usd"]
            ) ** 0.5
            return price

    def pool_liquidity(self):
        pool_contract = self.w3.eth.contract(
            address=config.pdi_weth_pool, abi=abis.uniswap_v3_pool
        )
        return pool_contract.functions.liquidity().call()

    def get_current_sqrt_price(self):
        pool_contract = self.w3.eth.contract(
            address=config.pdi_weth_pool, abi=abis.uniswap_v3_pool
        )
        return (pool_contract.functions.slot0().call()[0] ** 2 / 2**192) ** 0.5

    def calculate_tj_v1_trade_size(self, pool, target_price, premium):
        pool_contract = self.w3.eth.contract(address=pool, abi=abis.trader_joe_v1)
        x, y, _ = pool_contract.functions.getReserves().call()
        k = x * y
        new_x = (k / target_price) ** 0.5
        new_y = (k * target_price) ** 0.5
        delta_x = new_x - x
        delta_y = new_y - y
        if premium:
            if delta_x > 0:
                convert_to_avax = int(
                    (delta_x * self.get_nav_price()) / self.get_native_price()
                )
                return convert_to_avax
        else:
            return int(delta_y)

    def calculate_tj_v2_trade_size(self, pool, target_price, premium):
        pool_contract = self.w3.eth.contract(address=pool, abi=abis.trader_joe_v2)
        active_bin = int(pool_contract.functions.getActiveId().call())
        bin_step = pool_contract.functions.getBinStep().call()
        target_bin = (
            round(
                (math.log(target_price) / math.log(1 + bin_step / 10000) + 8388608) / 5
            )
            * 5
        )
        print(active_bin, target_bin)
        current_bin = active_bin
        output = 0
        if target_bin > current_bin:
            while current_bin != target_bin:
                x, y = pool_contract.functions.getBin(current_bin).call()
                output += x
                next_bin = int(
                    pool_contract.functions.getNextNonEmptyBin(
                        False, current_bin
                    ).call()
                )
                if next_bin != 0 and next_bin < target_bin:
                    current_bin = next_bin
                elif next_bin == target_bin:
                    x, y = pool_contract.functions.getBin(next_bin).call()
                    output += x
                    current_bin = target_bin
                else:
                    current_bin = target_bin
        elif target_bin < current_bin:
            while current_bin != target_bin:
                x, y = pool_contract.functions.getBin(current_bin).call()
                output += y
                next_bin = int(
                    pool_contract.functions.getNextNonEmptyBin(True, current_bin).call()
                )
                if next_bin != 0 and next_bin > target_bin:
                    current_bin = next_bin
                elif next_bin == target_bin:
                    x, y = pool_contract.functions.getBin(next_bin).call()
                    output += x
                    current_bin = target_bin
                else:
                    current_bin = target_bin
        return self.tj_v2_get_swap_in(pool, output, premium)

    def calculate_uni_v3_trade_size(self, pool, target_price, premium):
        pool_contract = self.w3.eth.contract(
            address=self.w3.to_checksum_address(pool), abi=abis.uniswap_v3_pool
        )
        url = "https://api.thegraph.com/subgraphs/name/uniswap/uniswap-v3"
        query = f"""query{{pool(id:\"{pool.lower()}\"){{
                        ticks {{
                        tickIdx
                        liquidityNet
                        }} }}
                    }}"""
        tick_array = requests.post(url=url, json={"query": query}).json()["data"][
            "pool"
        ]["ticks"]
        sorted_tick_array = [
            {"tickIdx": int(i["tickIdx"]), "liquidityNet": int(i["liquidityNet"])}
            for i in tick_array
            if int(i["liquidityNet"]) != 0
        ]
        sorted_tick_array = sorted(sorted_tick_array, key=lambda x: x["tickIdx"])
        target_tick = int(math.log(target_price, 1.0001))
        _, current_tick, _, _, _, _, _ = pool_contract.functions.slot0().call()
        current_liquidity = self.pool_liquidity()
        output = 0
        while current_tick != target_tick:
            if target_tick > current_tick:
                next_tick, next_liquiidty = list(
                    filter(lambda x: x["tickIdx"] > current_tick, sorted_tick_array)
                )[0].values()
                if target_tick < next_tick:
                    output += self.x_in_range(
                        current_liquidity,
                        self.tick_to_price(current_tick) ** 0.5,
                        self.tick_to_price(target_tick) ** 0.5,
                    )
                    current_tick = target_tick
                elif target_tick > next_tick:
                    output += self.x_in_range(
                        current_liquidity,
                        self.tick_to_price(current_tick) ** 0.5,
                        self.tick_to_price(next_tick) ** 0.5,
                    )
                    current_tick = next_tick
                    current_liquidity += next_liquiidty
            elif target_tick < current_tick:
                next_tick = list(
                    filter(lambda x: x["tickIdx"] < current_tick, sorted_tick_array)
                )
                next_tick.reverse()
                next_tick, next_liquiidty = next_tick[0].values()
                if target_tick > next_tick:
                    output += self.y_in_range(
                        current_liquidity,
                        self.tick_to_price(current_tick) ** 0.5,
                        self.tick_to_price(target_tick) ** 0.5,
                    )
                    current_tick = target_tick
                elif target_tick < next_tick:
                    output += self.y_in_range(
                        current_liquidity,
                        self.tick_to_price(current_tick) ** 0.5,
                        self.tick_to_price(next_tick) ** 0.5,
                    )
                    current_tick = next_tick
                    current_liquidity += next_liquiidty
        return self.uniswap_v3_get_swap_in(output, premium)

    def uniswap_v3_get_swap_in(self, amount, premium):
        uniswap_quoter_contract = self.w3.eth.contract(
            address=config.uniswap_quoter, abi=abis.uniswap_quoter
        )
        QuoteExactOutputSingleParams = {}
        QuoteExactOutputSingleParams["tokenIn"] = (
            self.index_address if premium else self.wrapped_native_address
        )
        QuoteExactOutputSingleParams["tokenOut"] = (
            self.wrapped_native_address if premium else self.index_address
        )
        QuoteExactOutputSingleParams["amount"] = int(amount)
        QuoteExactOutputSingleParams["fee"] = 3000
        QuoteExactOutputSingleParams["sqrtPriceLimitX96"] = 0
        (
            swap_in_amount,
            _,
            _,
            _,
        ) = uniswap_quoter_contract.functions.quoteExactOutputSingle(
            QuoteExactOutputSingleParams
        ).call()
        if premium:
            convert_to_native = (
                swap_in_amount * self.get_nav_price()
            ) / self.get_native_price()
            return int(convert_to_native)
        else:
            return int(swap_in_amount)
