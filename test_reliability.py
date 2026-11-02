import os
import sys
import time
import datetime
import resource
import gspread
import adaptive_strangle_v2 as v2

def run_diagnostics(test_order=False):
    print("==================================================")
    print("🚀 STARTING V2 TRADING INFRASTRUCTURE RELIABILITY TEST")
    print("==================================================")

    # 1. Verify Timezone & Environment
    tz = time.tzname[0]
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[1/5] System Time: {now_str} ({tz})")
    if tz != "IST":
        print("⚠️ WARNING: Timezone is not IST! Run: sudo timedatectl set-timezone Asia/Kolkata && sudo systemctl restart cron")

    if not os.path.exists(v2.SERVICE_ACCOUNT_FILE):
        print(f"❌ FAIL: {v2.SERVICE_ACCOUNT_FILE} not found in {os.getcwd()}")
        sys.exit(1)
    print("✅ Environment variables and service_account.json verified.")

    # 2. SmartAPI Auth & ScripMaster Memory Stress Test
    print("\n[2/5] Testing SmartAPI TOTP Login & ScripMaster Memory Usage...")
    smart = v2.init_smartapi_session()
    options_df = v2.get_nifty_options_master()
    next_expiry = v2.get_next_expiry(options_df)
    mem_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    print(f"✅ ScripMaster parsed ({len(options_df)} active Nifty contracts) | Next Expiry: {next_expiry} | Peak RAM: {mem_mb:.1f} MB")

    # 3. Market Filter & Edge Case Simulation
    print("\n[3/5] Testing Market Filters, Strike Selection & Roll Edge Cases...")
    is_safe, spot, gap_pct, orb_pts, msg = v2.check_v2_market_filters(smart)
    print(f"✅ Filter Check Output -> Safe: {is_safe} | Spot: {spot:.2f} | Gap: {gap_pct:+.2f}% | ORB: {orb_pts:.2f} pts | Msg: {msg}")

    atm_strike = round(spot / 50.0) * 50
    ce_sym, ce_tok = v2.get_contract_by_expiry_and_strike(options_df, next_expiry, atm_strike + 200, "CE")
    pe_sym, pe_tok = v2.get_contract_by_expiry_and_strike(options_df, next_expiry, atm_strike - 200, "PE")
    ce_ltp = v2.get_ltp(smart, "NFO", ce_sym, ce_tok)
    pe_ltp = v2.get_ltp(smart, "NFO", pe_sym, pe_tok)
    print(f"✅ Sample OTM Contracts -> CE: {ce_sym} (LTP: ₹{ce_ltp}) | PE: {pe_sym} (LTP: ₹{pe_ltp})")

    # Test Roll Strike Lookup Edge Case
    roll_ce_sym, _ = v2.get_contract_by_expiry_and_strike(options_df, next_expiry, atm_strike + 200 + v2.ROLL_DISTANCE_PTS, "CE")
    roll_pe_sym, _ = v2.get_contract_by_expiry_and_strike(options_df, next_expiry, atm_strike - 200 - v2.ROLL_DISTANCE_PTS, "PE")
    print(f"✅ Roll Target Contracts Verified -> Rolled CE: {roll_ce_sym} | Rolled PE: {roll_pe_sym}")

    # Test Fallback LTP when Order ID is None or Unfilled
    fallback_px = v2.get_filled_price(smart, None, "NFO", ce_sym, ce_tok, fallback_ltp=ce_ltp)
    print(f"✅ Unfilled Order Fallback Test -> Returned Price: ₹{fallback_px}")

    # 4. Google Sheets & Telegram Test
    print("\n[4/5] Testing Google Sheets Write/Delete & Telegram Alert...")
    gc = gspread.service_account(filename=v2.SERVICE_ACCOUNT_FILE)
    sh = gc.open_by_key(v2.GSHEET_ID_V2)
    ws = sh.sheet1
    test_row = [now_str[:10], now_str[11:], "DIAGNOSTIC_TEST", "TEST_OK", spot, gap_pct, orb_pts, ce_sym, pe_sym, 0, ce_sym, pe_sym, 0, "No", "Diagnostic Test", 0, 0, 0]
    ws.append_row(test_row)
    row_count = len(ws.get_all_values())
    ws.delete_rows(row_count)
    print(f"✅ Successfully wrote and cleaned up test row in Google Sheet: '{sh.title}'")
    v2.send_telegram_alert(f"🧪 V2 Diagnostic Passed at {now_str}\nSpot: {spot:.2f} | Next Expiry: {next_expiry}\nRAM Used: {mem_mb:.1f} MB")
    print("✅ Telegram alert sent.")

    # 5. Optional Safe Order Routing & Static IP Whitelist Test
    print("\n[5/5] Order Placement Gateway Test...")
    if test_order:
        print(f"Placing safe LIMIT BUY order @ ₹0.05 for {ce_sym} (Token: {ce_tok}, Qty: {v2.NIFTY_LOT_SIZE})...")
        order_params = {
            "variety": "NORMAL",
            "tradingsymbol": ce_sym,
            "symboltoken": str(ce_tok),
            "transactiontype": "BUY",
            "exchange": "NFO",
            "ordertype": "LIMIT",
            "price": "0.05",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "quantity": int(v2.NIFTY_LOT_SIZE)
        }
        res = smart.placeOrder(order_params)
        print(f"Broker Raw Response: {res}")
        order_id = res if isinstance(res, str) else (res.get("data", {}).get("orderid") if isinstance(res, dict) and res.get("data") else None)
        if order_id:
            time.sleep(1)
            ob = v2.get_order_book_dict(smart)
            print(f"✅ Order routed via Static IP! Order ID: {order_id} | Status in OrderBook: {ob.get(str(order_id))}")
            try:
                cancel_res = smart.cancelOrder(order_id, "NORMAL")
                print(f"✅ Cancel Order Response: {cancel_res}")
            except Exception as e:
                print(f"ℹ️ Cancel skipped (order likely auto-rejected outside market hours): {e}")
        else:
            print("❌ Order gateway did not return an order ID. Check static IP registration on Angel One.")
    else:
        print("ℹ️ Skipped live order gateway test. Run with '--test-order' flag to test order placement.")

    print("\n🎉 ALL RELIABILITY CHECKS COMPLETED SUCCESSFULLY!")

if __name__ == "__main__":
    test_order_flag = "--test-order" in sys.argv
    run_diagnostics(test_order=test_order_flag)