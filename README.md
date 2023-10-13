# Index-arb-bot
Arbitrage bot that ensures the exchange price of Phuture index products tracks the NAV. 

Arb bot currently tracks:
* PDI - Chain ID: 1, Address: 0x632806BF5c8f062932Dd121244c9fbe7becb8B48
* CAI - Chain ID: 43114, Address: 0x48f88A3fE843ccb0b5003e70B4192c1d7448bEf0

## Setup
1. Run npm install to install the required node packages
2. Run pipenv install to create a virtual environment and install the required python packages.
3. Create a .env file with the following variables:
* ETHEREUM_INFURA_URL
* AVALANCHE_INFURA_URL
* GANACHE_MAINNET_FORK
* PROD_ACCOUNT_PK
* GANACHE_FORK_PK 
* ZERO_X_KEY

5. Run execution.py with either a 'dev' or 'prod' argument. Dev will only execute transactions within a Ganache fork, whilst prod will execute transactions in a live environment.

