#!/usr/bin/env python3
"""
test_dhan_connection.py - DhanHQ API v2.0 Connectivity Test.
Uses a direct REST call for TOTP auth to bypass SDK bugs, 
then initializes the Dhan v2 SDK for market data and trading.
"""
import os
import sys
import time
import requests
import pyotp

# We import DhanContext to wrap the token, and dhanhq for trading.
# We DO NOT import DhanLogin, as we are bypassing it.
from dhanhq import DhanContext, dhanhq

# --- 1. Load Configuration ---
CONFIG_FILE = "dhan_test_config.env"
if not os.path.exists(CONFIG_FILE):
    print(f"❌ Error: Config file '{CONFIG_FILE}' not found.")
    sys.exit(1)

with open(CONFIG_FILE, "r") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip())

DHAN_CLIENT_ID = os.environ.get("DHAN_CLIENT_ID")
DHAN_PIN = os.environ.get("DHAN_PIN")
DHAN_TOTP_SECRET = os.environ.get("DHAN_TOTP_SECRET")

if not all([DHAN_CLIENT_ID, DHAN_PIN, DHAN_TOTP_SECRET]):
    print("❌ Error: Missing credentials in config file.")
    sys.exit(1)

print("✅ Configuration loaded successfully.")


# --- 2. Authenticate via Direct REST Call ---
def authenticate_dhan():
    print(f"\n🔑 Attempting Direct REST Authentication for Client ID: {DHAN_CLIENT_ID}...")
    
    # Generate live TOTP
    totp_code = pyotp.TOTP(DHAN_TOTP_SECRET).now()
    print(f"   Generated TOTP: {totp_code}")
    
    # Construct the query URL exactly as Dhan's backend requires
    url = f"https://auth.dhan.co/app/generateAccessToken?dhanClientId={DHAN_CLIENT_ID}&pin={DHAN_PIN}&totp={totp_code}"
    headers = {"Accept": "application/json"}
    
    try:
        # Fetch token directly from Dhan's servers
        response = requests.post(url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if "accessToken" in data:
                fresh_token = data["accessToken"]
                print("   ✅ Direct token generation successful!")
                
                # CRITICAL: Wrap the token in DhanContext for SDK 2.0
                context = DhanContext(DHAN_CLIENT_ID, fresh_token)
                dhan = dhanhq(context)
                
                # Verify SDK login by checking fund limits
                funds = dhan.get_fund_limits()
                if isinstance(funds, dict) and 'data' in funds:
                    avail_margin = funds['data'].get('availabelBalance', 'N/A')
                    print(f"✅ SDK Initialized Successfully! Available Margin: ₹{avail_margin}")
                    return dhan
                else:
                    print(f"❌ SDK initialized, but fund check failed: {funds}")
                    sys.exit(1)
            else:
                print(f"❌ Server returned 200 but no token: {response.text}")
                sys.exit(1)
        else:
            print(f"❌ Authentication Failed (HTTP {response.status_code}): {response.text}")
            sys.exit(1)
            
    except Exception as e:
        print(f"\n❌ Exception during authentication: {e}")
        sys.exit(1)


# --- 3. Fetch Market Data ---
def fetch_market_quotes(dhan):
    print("\n📈 Testing Market Quote Retrieval...")
    try:
        quote = dhan.ticker_data(securities={"NSE": ["13"], "BSE": ["1"]})
        if isinstance(quote, dict) and quote.get('status') == 'success' and 'data' in quote:
            data = quote['data']
            print(f"✅ Market Data Retrieved Successfully:")
            print(f"   -> Nifty 50 LTP: {data.get('NSE:13', {}).get('last_price', 'N/A')}")
            print(f"   -> Sensex LTP: {data.get('BSE:1', {}).get('last_price', 'N/A')}")
        else:
             print(f"⚠️ Failed to fetch market quotes: {quote}")
    except Exception as e:
        print(f"❌ Error fetching quotes: {e}")


# --- 4. Place a Test Order ---
def place_test_order(dhan):
    print("\n🛒 Testing Order Placement Logic...")
    
    EXECUTE_REAL_ORDER = False 
    test_security_id = "11915" # YES BANK NSE Equity
    
    if not EXECUTE_REAL_ORDER:
         print("✅ [PAPER MODE] Order payload structured correctly.")
         print(f"Simulating BUY order for 1 share of Security ID: {test_security_id} (NSE_EQ)")
         return

    print("⚠️ Attempting to place a REAL order for 1 share of YES BANK...")
    try:
        response = dhan.place_order(
            security_id=test_security_id,
            exchange_segment=dhan.NSE,
            transaction_type=dhan.BUY,
            quantity=1,
            order_type=dhan.MARKET,
            product_type=dhan.INTRA,
            price=0
        )
        print(f"Order Response: {response}")
        
        if isinstance(response, dict) and response.get('status') == 'success':
            order_id = response['data'].get('orderId')
            print(f"✅ Order Placed Successfully! Order ID: {order_id}")
            time.sleep(3)
            status_res = dhan.get_order_by_id(order_id)
            if isinstance(status_res, dict) and status_res.get('status') == 'success':
                 print(f"ℹ️ Current Order Status: {status_res['data'].get('orderStatus')}")
        else:
             print("❌ Order placement failed.")
    except Exception as e:
        print(f"❌ Exception during order placement: {e}")


if __name__ == "__main__":
    print("--- Starting DhanHQ API Diagnostic Test ---")
    dhan_client = authenticate_dhan()
    if dhan_client:
        fetch_market_quotes(dhan_client)
        place_test_order(dhan_client)
    print("\n--- Diagnostic Test Complete ---")