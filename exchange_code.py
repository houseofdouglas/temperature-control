#!/usr/bin/env python3
"""
Paste the full redirect URL (or just the code= value) as the argument.
Example:
    python exchange_code.py "http://localhost:8765/callback?code=4/XXXX&scope=..."
"""

import os, sys, requests
from dotenv import load_dotenv

load_dotenv()

raw = sys.argv[1] if len(sys.argv) > 1 else input("Paste the full redirect URL: ")

# Extract just the code value whether they paste the full URL or just the code
if "code=" in raw:
    code = raw.split("code=")[1].split("&")[0]
else:
    code = raw.strip()

resp = requests.post("https://oauth2.googleapis.com/token", data={
    "code":          code,
    "client_id":     os.environ["GOOGLE_CLIENT_ID"],
    "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
    "redirect_uri":  "http://localhost:8765/callback",
    "grant_type":    "authorization_code",
}, timeout=10)

data = resp.json()
if "refresh_token" not in data:
    print("ERROR:", data)
    sys.exit(1)

token = data["refresh_token"]
print("\n" + "═" * 60)
print("SUCCESS! Updating .env automatically...")
print("═" * 60)

env_path = os.path.join(os.path.dirname(__file__), ".env")
with open(env_path) as f:
    content = f.read()

import re
content = re.sub(r"^GOOGLE_REFRESH_TOKEN=.*$", f"GOOGLE_REFRESH_TOKEN={token}", content, flags=re.MULTILINE)

with open(env_path, "w") as f:
    f.write(content)

print(f"GOOGLE_REFRESH_TOKEN={token}")
print("═" * 60)
print(".env updated!")
