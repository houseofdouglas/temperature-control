#!/usr/bin/env python3
"""
One-time OAuth token helper.
Run this script ONCE to get your Google refresh token, then add it to .env.

Usage:
    python get_token.py

It will open a browser, ask you to log in with the Google account that owns
your Nest devices, and then print the refresh token to paste into .env.
"""

import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID     = os.environ["GOOGLE_CLIENT_ID"]
CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]

# SDM scope — needed to read devices and control the fan
SCOPE         = "https://www.googleapis.com/auth/sdm.service"
REDIRECT_URI  = "http://localhost:8765/callback"
AUTH_URL      = f"https://nestservices.google.com/partnerconnections/{os.environ['SDM_PROJECT_ID']}/auth"
TOKEN_URL     = "https://oauth2.googleapis.com/token"

auth_code: str | None = None
server_done = threading.Event()


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        parsed = urlparse(self.path)
        if parsed.path == "/callback":
            params = parse_qs(parsed.query)
            if "code" in params:
                auth_code = params["code"][0]
                body = b"<h2>Authorization complete - you can close this tab.</h2>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                error = params.get("error", ["unknown"])[0]
                body = f"<h2>Error: {error}</h2>".encode()
                self.send_response(400)
                self.end_headers()
                self.wfile.write(body)
        server_done.set()

    def log_message(self, format, *args):
        pass  # silence request logs


def run_server():
    server = HTTPServer(("localhost", 8765), CallbackHandler)
    server.handle_request()  # handle exactly one request then stop


def main():
    params = {
        "client_id":     CLIENT_ID,
        "redirect_uri":  REDIRECT_URI,
        "response_type": "code",
        "scope":         SCOPE,
        "access_type":   "offline",
        "prompt":        "consent",  # force refresh_token to be returned
    }
    url = f"{AUTH_URL}?{urlencode(params)}"

    print("Starting local callback server on http://localhost:8765 …")
    t = threading.Thread(target=run_server, daemon=True)
    t.start()

    print(f"\nOpening browser for Google authorization…")
    print(f"If the browser doesn't open automatically, visit:\n  {url}\n")
    webbrowser.open(url)

    server_done.wait(timeout=120)

    if not auth_code:
        print("ERROR: No authorization code received within 2 minutes.")
        return

    print("Authorization code received. Exchanging for tokens…")

    resp = requests.post(TOKEN_URL, data={
        "code":          auth_code,
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri":  REDIRECT_URI,
        "grant_type":    "authorization_code",
    }, timeout=10)
    resp.raise_for_status()
    tokens = resp.json()

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        print("ERROR: No refresh_token in response. "
              "Make sure you passed prompt=consent and access_type=offline.")
        print("Full response:", tokens)
        return

    print("\n" + "═" * 60)
    print("SUCCESS! Add this to your .env file:")
    print("═" * 60)
    print(f"GOOGLE_REFRESH_TOKEN={refresh_token}")
    print("═" * 60)


if __name__ == "__main__":
    main()
