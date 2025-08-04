import ccxt
import pandas as pd

# Step 1: Get BTC data
exchange = ccxt.binance()
ohlcv = exchange.fetch_ohlcv('BTC/USDT', '1h', limit=50)

# Step 2: Convert to DataFrame
df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])

# Step 3: Calculate SMAs
df['SMA10'] = df['close'].rolling(10).mean()
df['SMA30'] = df['close'].rolling(30).mean()

# Step 4: Generate signals (1 = Buy, -1 = Sell, 0 = No Action)
df['Signal'] = 0
df.loc[df['SMA10'] > df['SMA30'], 'Signal'] = 1
df.loc[df['SMA10'] < df['SMA30'], 'Signal'] = -1

# Step 5: Print last 5 rows
print(df.tail())

