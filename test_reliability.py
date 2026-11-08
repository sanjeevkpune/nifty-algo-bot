#!/usr/bin/env python3
"""
test_reliability.py - Comprehensive Infrastructure & API Diagnostic Tool
Built specifically to validate the V3.2 adaptive_strangle_v2.py architecture.
"""
import os
import sys
import time
import datetime
import traceback
import gspread

# 1. Force load the environment variables exactly as the crontab would
CONFIG_ENV_PATH = "/etc/trade-config/config.env"
if os.path.exists(CONFIG_ENV_PATH):
    with open(CONFIG_ENV_PATH, "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())
else:
    print(f"⚠️  WARNING: {CONFIG_ENV_PATH} not found. Relying on current shell environment.")

# 2. Import the main strategy file (User confirmed V3.2 code is saved as adaptive_strangle_v2.py)
try:
    import adaptive_strangle_v2 as strategy
except ImportError:
    print("❌ ERROR: Could not import 'adaptive_strangle_v2.py'. Ensure it is in the same directory.")
    sys.exit(1)


def print_header(title):
    print(f"\n{'-'*60}\n{title}\n{'-'*60}")

def run_all_tests():
    print("============================================================")
    print("🚀 STARTING V3.2 INFRASTRUCTURE & BROKER RELIABILITY TEST")
    print("============================================================")

    # Instantiate the bot to test its internal methods
    bot = strategy.Bot()
    passed_tests = 0
    total_tests = 7

    # ---------------------------------------------------------
    print_header("1. ENVIRONMENT & TIMEZONE CHECK")
    # ---------------------------------------------------------
    try:
        tz = time.tzname[0]
        now = strategy.now_ist()
        print(f"➜ System Timezone: {tz}")
        print(f"➜ IST Time Calculated: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        
        if tz != "IST":
            print("❌ FAIL: System timezone is not IST.")
        else:
            print("✅ PASS: Timezone is correctly set to IST.")
            passed_tests += 1
            
        print(f"➜ PAPER_MODE status: {'ON (Safe)' if strategy.PAPER_MODE else 'OFF (LIVE TRADING)'}")
    except Exception as e:
        print(f"❌ FAIL: Environment check crashed: {e}")

    # ---------------------------------------------------------
    print_header("2. ANGEL ONE AUTHENTICATION (SmartAPI + TOTP)")
    # ---------------------------------------------------------
    try:
        bot.login()
        print("✅ PASS: Successfully authenticated with Angel One and generated session.")
        passed_tests += 1
    except Exception as e:
        print(f"❌ FAIL: Angel One login rejected. Check API_KEY, PIN, and TOTP_SECRET.\nError: {e}")
        sys.exit(1) # Stop here if auth fails, rest won't work

    # ---------------------------------------------------------
    print_header("3. SCRIP MASTER & CONTRACT PARSING")
    # ---------------------------------------------------------
    try:
        bot.load_master()
        if bot.options_df is not None and not bot.options_df.empty:
            print(f"➜ Found {len(bot.options_df)} valid NIFTY option contracts.")
            print(f"➜ Target Expiry Selected ({strategy.EXPIRY_MODE}): {bot.expiry}")
            
            # Test a specific contract fetch
            test_ce = bot.find_contract(25000, "CE")
            if test_ce:
                print(f"➜ Sample Contract Found: {test_ce['symbol']} (Token: {test_ce['token']}, Lot: {test_ce['lotsize']})")
                print("✅ PASS: Scrip master downloaded and parsed correctly.")
                passed_tests += 1
            else:
                print("❌ FAIL: Could not find a sample 25000 CE contract in the parsed data.")
        else:
            print("❌ FAIL: Options dataframe is empty after parsing.")
    except Exception as e:
        print(f"❌ FAIL: Scrip master fetch failed: {e}")

    # ---------------------------------------------------------
    print_header("4. LIVE MARKET DATA & OHLC FEED")
    # ---------------------------------------------------------
    try:
        # Test 1: Batched LTP Feed
        ltps = bot.get_ltps({"NSE": [strategy.NIFTY_SPOT_TOKEN]})
        spot_ltp = ltps.get(strategy.NIFTY_SPOT_TOKEN)
        
        # Test 2: FULL OHLC Feed (Specifically testing the Previous Close gap fix)
        ohlc = bot.spot_ohlc()
        
        if spot_ltp and ohlc:
            print(f"➜ Nifty Spot Live LTP: {spot_ltp}")
            print(f"➜ OHLC Data -> Open: {ohlc['open']}, High: {ohlc['high']}, Low: {ohlc['low']}, Prev Close: {ohlc['close']}")
            if ohlc['close'] > 0:
                print("✅ PASS: Market data feed is active and Previous Close is valid for Gap calculations.")
                passed_tests += 1
            else:
                print("❌ FAIL: Previous Close is 0. Gap calculation will fail.")
        else:
            print("❌ FAIL: Could not fetch Nifty Spot LTP or OHLC data. (Is it a weekend/holiday?)")
    except Exception as e:
        print(f"❌ FAIL: Market data feed crashed: {e}")

    # ---------------------------------------------------------
    print_header("5. GHOST-ORDER PROTECTIONS (Position & Order Books)")
    # ---------------------------------------------------------
    try:
        # We simulate the bot having traded a dummy symbol to test the position read
        bot.traded_symbols.add("NIFTY_DUMMY_SYMBOL") 
        
        net_pos = bot.get_net_positions(tries=1)
        open_ords = bot.open_orders()
        
        if net_pos is not None and open_ords is not None:
            print("➜ Successfully queried Angel One Position Book.")
            print("➜ Successfully queried Angel One Order Book.")
            print("✅ PASS: Broker-side safety verification APIs are responding.")
            passed_tests += 1
        else:
            print("❌ FAIL: Could not read positions or open orders from broker. Ghost-order protection would trigger a halt.")
    except Exception as e:
        print(f"❌ FAIL: Position/Order book query failed: {e}")

    # ---------------------------------------------------------
    print_header("6. GOOGLE SHEETS LOGGING (I/O Test)")
    # ---------------------------------------------------------
    try:
        if not os.path.exists(strategy.SERVICE_ACCOUNT_FILE):
            print(f"❌ FAIL: Service account JSON not found at {strategy.SERVICE_ACCOUNT_FILE}")
        else:
            gc = gspread.service_account(filename=strategy.SERVICE_ACCOUNT_FILE)
            sh = gc.open_by_key(strategy.GSHEET_ID_V2)
            ws = sh.sheet1
            
            # Write a dummy test row
            now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            test_row = [now_str[:10], now_str[11:], "DIAGNOSTIC_TEST", "TEST_OK", 0, 0, 0, "N/A", "N/A", 0, "N/A", "N/A", 0, "No", "Diagnostic Test", 0, 0, 0]
            ws.append_row(test_row)
            
            # Immediately delete it to keep sheet clean
            all_vals = ws.get_all_values()
            ws.delete_rows(len(all_vals))
            
            print(f"➜ Connected to Sheet: '{sh.title}'")
            print("✅ PASS: Successfully wrote and deleted a test row in Google Sheets.")
            passed_tests += 1
    except Exception as e:
        print(f"❌ FAIL: Google Sheets integration failed. Check GSHEET_ID_V2 and service_account permissions.\nError: {e}")

    # ---------------------------------------------------------
    print_header("7. TELEGRAM ALERTS")
    # ---------------------------------------------------------
    try:
        strategy.tg(f"🧪 V3.2 Infrastructure Diagnostic Test Completed.\nScore: {passed_tests}/{total_tests} Tests Passed.")
        print("✅ PASS: Telegram alert dispatched successfully.")
        passed_tests += 1
    except Exception as e:
        print(f"❌ FAIL: Telegram alert failed. Check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.\nError: {e}")

    # ---------------------------------------------------------
    print("\n============================================================")
    if passed_tests == total_tests:
        print(f"🎉 SUCCESS: All {total_tests}/{total_tests} systems are operational and ready for production.")
    else:
        print(f"⚠️ WARNING: Only {passed_tests}/{total_tests} tests passed. Review the failures above before live trading.")
    print("============================================================")

if __name__ == "__main__":
    try:
        run_all_tests()
    except KeyboardInterrupt:
        print("\nTest cancelled by user.")
        sys.exit(0)
    except Exception as e:
        print(f"\n🚨 FATAL ERROR IN DIAGNOSTICS: {e}")
        traceback.print_exc()
        sys.exit(1)
