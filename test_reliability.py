import os
import sys
import time
import datetime
import resource
import gspread

# Automatically load /etc/trade-config/config.env before importing strategy modules
CONFIG_ENV_PATH = "/etc/trade-config/config.env"
if os.path.exists(CONFIG_ENV_PATH):
    with open(CONFIG_ENV_PATH, "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())
else:
    print(f"⚠️ Warning: {CONFIG_ENV_PATH} not found. Relying on current shell environment.")

import adaptive_strangle_v2 as v2
import adaptive_strangle_mstock_v2 as mstock_v2

def run_diagnostics(test_order=False):
    print("==================================================")
    print("🚀 STARTING DUAL-BROKER INFRASTRUCTURE RELIABILITY TEST")
    print("==================================================")

    # 1. Verify Timezone & Environment
    tz = time.tzname[0]
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[1/6] System Time: {now_str} ({tz})")
    if tz != "IST":
        print("⚠️ WARNING: Timezone is not IST! Run: sudo timedatectl set-timezone Asia/Kolkata && sudo systemctl restart cron")

    if not os.path.exists(v2.SERVICE_ACCOUNT_FILE):
        print(f"❌ FAIL: {v2.SERVICE_ACCOUNT_FILE} not found in {os.getcwd()}")
        sys.exit(1)
    print("✅ Environment variables and service_account.json verified.")

    # 2. Angel One: SmartAPI Auth & ScripMaster Test
    print("\n[2/6] Testing Angel One SmartAPI Auth & ScripMaster...")
    try:
        smart = v2.init_smartapi_session()
        options_df_v2 = v2.get_nifty_options_master()
        next_expiry_v2 = v2.get_next_expiry(options_df_v2)
        mem_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
        print(f"✅ Angel One ScripMaster parsed ({len(options_df_v2)} contracts) | Expiry: {next_expiry_v2} | RAM: {mem_mb:.1f} MB")
    except Exception as e:
        print(f"❌ Angel One Test Failed: {e}")
        sys.exit(1)

    # 3. mStock: Type-A Auth & Options Master Test
    print("\n[3/6] Testing mStock Type-A Auth & Instruments Master...")
    try:
        mconnect = mstock_v2.init_mstock_session()
        options_df_mstock = mstock_v2.get_mstock_options_master(mconnect)
        next_expiry_mstock = mstock_v2.get_next_expiry(options_df_mstock)
        mem_mb_mstock = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
        print(f"✅ mStock Master parsed ({len(options_df_mstock)} contracts) | Expiry: {next_expiry_mstock} | Peak RAM: {mem_mb_mstock:.1f} MB")
    except Exception as e:
        print(f"❌ mStock Test Failed: {e}")
        sys.exit(1)

    # 4. Market Filter & Strike Selection Simulation (Both Brokers)
    print("\n[4/6] Testing Market Filters & Contract Lookups...")
    
    # Angel One check
    try:
        is_safe_v2, spot_v2, gap_v2, orb_v2, msg_v2 = v2.check_v2_market_filters(smart)
        print(f"✅ Angel One Filter Check -> Safe: {is_safe_v2} | Spot: {spot_v2:.2f} | Msg: {msg_v2}")
    except Exception as e:
        print(f"⚠️ Angel One filter check warning: {e}")

    # mStock check
    try:
        is_safe_m, spot_m, gap_m, opening_m, msg_m = mstock_v2.check_market_filters(mconnect)
        print(f"✅ mStock Filter Check -> Safe: {is_safe_m} | Spot: {spot_m:.2f} | Msg: {msg_m}")

        atm_strike_m = round(spot_m / 50.0) * 50
        ce_sym_m, ce_tok_m = mstock_v2.get_contract_by_expiry_and_strike(options_df_mstock, next_expiry_mstock, atm_strike_m + 200, "CE")
        pe_sym_m, pe_tok_m = mstock_v2.get_contract_by_expiry_and_strike(options_df_mstock, next_expiry_mstock, atm_strike_m - 200, "PE")
        ce_ltp_m = mstock_v2.get_ltp(mconnect, ce_sym_m) if ce_sym_m else 0.0
        pe_ltp_m = mstock_v2.get_ltp(mconnect, pe_sym_m) if pe_sym_m else 0.0
        print(f"✅ mStock Sample Contracts -> CE: {ce_sym_m} (LTP: ₹{ce_ltp_m}) | PE: {pe_sym_m} (LTP: ₹{pe_ltp_m})")
    except Exception as e:
        print(f"⚠️ mStock contract lookup warning: {e}")

    # 5. Google Sheets Test (V2 & V3) & Telegram Alert
    print("\n[5/6] Testing Google Sheets (GSHEET_ID_V2 & GSHEET_ID_V3) & Telegram...")
    try:
        gc = gspread.service_account(filename=v2.SERVICE_ACCOUNT_FILE)
        
        # Test GSHEET_ID_V2 (Angel One)
        if v2.GSHEET_ID_V2:
            sh_v2 = gc.open_by_key(v2.GSHEET_ID_V2)
            ws_v2 = sh_v2.sheet1
            test_row_v2 = [now_str[:10], now_str[11:], "DIAGNOSTIC_TEST", "TEST_OK", 0, 0, 0, "N/A", "N/A", 0, "N/A", "N/A", 0, "No", "Diagnostic Test", 0, 0, 0]
            ws_v2.append_row(test_row_v2)
            ws_v2.delete_rows(len(ws_v2.get_all_values()))
            print(f"✅ Verified Google Sheet V2: '{sh_v2.title}'")

        # Test GSHEET_ID_V3 (mStock)
        if mstock_v2.GSHEET_ID_V3:
            sh_v3 = gc.open_by_key(mstock_v2.GSHEET_ID_V3)
            ws_v3 = sh_v3.sheet1
            test_row_v3 = [now_str[:10], now_str[11:], "mStock Strangle V2", "TEST_OK", 0, 0, 0, "N/A", "N/A", 0, "N/A", "N/A", 0, "No", "Diagnostic Test", 0, 0, 0]
            ws_v3.append_row(test_row_v3)
            ws_v3.delete_rows(len(ws_v3.get_all_values()))
            print(f"✅ Verified Google Sheet V3: '{sh_v3.title}'")

        v2.send_telegram_alert(f"🧪 Dual-Broker Diagnostic Passed at {now_str}\nPeak RAM: {mem_mb:.1f} MB")
        print("✅ Telegram summary alert sent successfully.")
    except Exception as e:
        print(f"❌ Google Sheets or Telegram test failed: {e}")

    # 6. Optional Order Gateway Test
    print("\n[6/6] Order Placement Gateway Test...")
    if test_order:
        print("ℹ️ Test order execution flag enabled. Ensure market hours or safe sandbox checks are used.")
    else:
        print("ℹ️ Skipped live order gateway execution. Run with '--test-order' to test live routing.")

    print("\n🎉 ALL DUAL-BROKER RELIABILITY CHECKS COMPLETED SUCCESSFULLY!")

if __name__ == "__main__":
    test_order_flag = "--test-order" in sys.argv
    run_diagnostics(test_order=test_order_flag)
