import time
import pyotp
import os

# Read the secret from your config file
secret = None
with open("dhan_test_config.env", "r") as f:
    for line in f:
        if "DHAN_TOTP_SECRET" in line:
            secret = line.split("=")[1].strip()

if not secret:
    print("Could not find DHAN_TOTP_SECRET in config.")
else:
    print("Local VM Time:", time.ctime())
    totp = pyotp.TOTP(secret)
    print("Current TOTP Code:", totp.now())
    print("\n👉 Open your Google Authenticator app on your phone.")
    print("👉 Compare the code above to the one on your phone.")