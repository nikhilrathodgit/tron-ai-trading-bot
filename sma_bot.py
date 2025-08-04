import ccxt
import pandas as pd

# Getting BTC data 
exchange = ccxt.binance()
ohlcv = exchange.fetch_ohlcv('BTC/USDT', '1h', limit=50)

# Converting to DataFrame
df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])

# Calculating SMAs
df['SMA10'] = df['close'].rolling(10).mean()
df['SMA30'] = df['close'].rolling(30).mean()

# Generating signals (1 = Buy/Long, -1 = Sell/Short, 0 = No Action)
df['Signal'] = 0
df.loc[df['SMA10'] > df['SMA30'], 'Signal'] = 1
df.loc[df['SMA10'] < df['SMA30'], 'Signal'] = -1

# Printing last 5 rows to check data looks correct
print(df.tail())

