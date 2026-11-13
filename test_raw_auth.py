#!/usr/bin/env python3
import os
import requests
import pyotp

# Load Config
secret = None
with open("dhan_test_config.env", "r") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip())

CLIENT_ID = os.environ.get("DHAN_CLIENT_ID")
PIN = os.environ.get("DHAN_PIN")
TOTP_SECRET = os.environ.get("DHAN_TOTP_SECRET")

print("--- Direct REST Authentication Test ---")
print(f"Client ID: {CLIENT_ID}")

# Generate String TOTP
totp_str = pyotp.TOTP(TOTP_SECRET).now()
print(f"Generated 6-Digit TOTP: {totp_str}")

# Target URL with Query Parameters (Dhan requires them in the URL, not the body)
url = f"https://auth.dhan.co/app/generateAccessToken?dhanClientId={CLIENT_ID}&pin={PIN}&totp={totp_str}"

headers = {
    "Accept": "application/json"
}

print("Attempting to fetch token...")
try:
    # POST request, but no JSON body payload
    response = requests.post(url, headers=headers, timeout=10)
    print(f"\nHTTP Status Code: {response.status_code}")
    
    if response.status_code == 200:
        data = response.json()
        if "accessToken" in data:
            print("\n✅ SUCCESS! A valid JWT token was generated.")
            print(f"Token (First 20 chars): {data['accessToken'][:20]}...")
        else:
            print(f"\n❌ FAILED. The server returned 200 OK but no token was provided: {response.text}")
    else:
        print(f"\n❌ FAILED. The server rejected the credentials: {response.text}")
        
except Exception as e:
    print(f"\n❌ REQUEST CRASHED: {e}")