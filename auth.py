"""
Run once each morning before market hours:
    python auth.py
It will open a browser for Kite login, then ask you to paste the redirect URL.
The access token is saved to .env automatically.
"""

import os
import re
import webbrowser
from kiteconnect import KiteConnect
from dotenv import load_dotenv, set_key

load_dotenv()

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

kite = KiteConnect(api_key=API_KEY)

login_url = kite.login_url()
print(f"\nOpening Kite login page...\n{login_url}\n")
webbrowser.open(login_url)

print("After login, your browser will redirect to a URL like:")
print("  http://127.0.0.1?request_token=XXXXXXXX&action=login&status=success")
print("\nPaste that full URL here:")
redirect_url = input("> ").strip()

match = re.search(r"request_token=([^&]+)", redirect_url)
if not match:
    print("Could not find request_token in the URL. Please try again.")
    exit(1)

request_token = match.group(1)
data = kite.generate_session(request_token, api_secret=API_SECRET)
access_token = data["access_token"]

env_path = os.path.join(os.path.dirname(__file__), ".env")
set_key(env_path, "ACCESS_TOKEN", access_token)

print(f"\nAccess token saved to .env")
print(f"Token: {access_token[:10]}...{access_token[-4:]}")
print("\nYou can now run:  python monitor.py")
