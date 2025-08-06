import ccxt
import pandas as pd

def fetch_btc_data(limit=50):
    """
    Fetches the latest OHLCV data for BTC/USDT from Binance.
    limit: Number of candles (default 50)
    """
    exchange = ccxt.binance()
    ohlcv = exchange.fetch_ohlcv('BTC/USDT', '1h', limit=limit)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    return df

def calculate_sma_signals(df):
    """
    Adds SMA10, SMA30, and Signal columns to the dataframe.
    Signal: 1 = Buy, -1 = Sell, 0 = No Action
    """
    df['SMA10'] = df['close'].rolling(10).mean()
    df['SMA30'] = df['close'].rolling(30).mean()

    df['Signal'] = 0
    df.loc[df['SMA10'] > df['SMA30'], 'Signal'] = 1
    df.loc[df['SMA10'] < df['SMA30'], 'Signal'] = -1
    return df

def get_latest_signal():
    """
    Returns the latest SMA crossover signal: "Buy", "Sell", or "Null Trade"
    """
    df = fetch_btc_data()
    df = calculate_sma_signals(df)
    latest_signal = df['Signal'].iloc[-1]  # Last signal

    if latest_signal == 1:
        return "Buy"
    elif latest_signal == -1:
        return "Sell"
    else:
        return "Null Trade"

# Test run
if __name__ == "__main__":
    print("Latest Signal:", get_latest_signal())
