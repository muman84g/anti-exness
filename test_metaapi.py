from metaapi_bridge import MetaApiBridge
import logging
import asyncio

logging.basicConfig(level=logging.INFO)

async def test_metaapi():
    print("Testing MetaApi Connectivity...")
    bridge = MetaApiBridge()
    
    success = bridge.connect()
    if success:
        print("[OK] MetaApi Connected Successfully.")
        
        # Test data fetching
        df = bridge.get_historical_data("EURUSDm", 60, 5)
        if df is not None:
            print("[OK] Historical data fetched.")
            print(df.tail())
        else:
            print("[ERROR] Failed to fetch historical data.")
            
        bridge.disconnect()
    else:
        print("[ERROR] MetaApi Connection Failed. Check your Token and Account ID.")

if __name__ == "__main__":
    asyncio.run(test_metaapi())
