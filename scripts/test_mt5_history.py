import MetaTrader5 as mt5
import os
from dotenv import load_dotenv

def test_history():
    load_dotenv()
    login = int(os.getenv("MT5_LOGIN"))
    password = os.getenv("MT5_PASSWORD")
    server = os.getenv("MT5_SERVER")
    
    if not mt5.initialize(login=login, password=password, server=server):
        print("Failed to initialize")
        return

    # Get history for the last 24 hours
    from datetime import datetime, timedelta
    from_date = datetime.now() - timedelta(days=1)
    to_date = datetime.now()
    
    deals = mt5.history_deals_get(from_date, to_date)
    if deals:
        print(f"Found {len(deals)} deals in the last 24h:")
        for deal in deals:
            print(f"Deal: {deal.ticket} | Symbol: {deal.symbol} | Profit: {deal.profit} | Commission: {deal.commission} | Swap: {deal.swap}")
    else:
        print("No deals found in history.")

    mt5.shutdown()

if __name__ == "__main__":
    test_history()
