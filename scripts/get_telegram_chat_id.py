#!/usr/bin/env python3
"""
Telegram Chat ID Finder
Helps you find your Chat ID to enable bot notifications.
"""

import os
import sys
import time
import requests
from dotenv import load_dotenv

def main():
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    
    if not token:
        print("❌ Error: TELEGRAM_BOT_TOKEN not found in .env")
        return

    print("=" * 60)
    print("📱 TELEGRAM CHAT ID FINDER")
    print("=" * 60)
    print("Step 1: Open Telegram")
    print(f"Step 2: Find your bot (@{token.split(':')[0]}) or use your bot link.")
    print("Step 3: Send the message /start to your bot.")
    print("-" * 60)
    print("Waiting for your message... (Ctrl+C to stop)")
    
    last_update_id = 0
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    
    try:
        while True:
            try:
                resp = requests.get(url, params={"offset": last_update_id + 1, "timeout": 10}, timeout=15)
                data = resp.json()
                
                if data.get("ok") and data.get("result"):
                    for update in data["result"]:
                        last_update_id = update["update_id"]
                        msg = update.get("message")
                        if msg:
                            chat = msg.get("chat")
                            user = msg.get("from")
                            print(f"\n✅ FOUND CHAT ID!")
                            print(f"   Name:    {user.get('first_name')} {user.get('last_name', '')}")
                            print(f"   Username: @{user.get('username', 'N/A')}")
                            print(f"   CHAT ID:  {chat.get('id')}")
                            print("-" * 60)
                            print(f"👉 COPY THIS ID: {chat.get('id')}")
                            print(f"👉 PASTE IT INTO .env as TELEGRAM_CHAT_ID={chat.get('id')}")
                            print("-" * 60)
                            return
            except Exception as e:
                pass
            time.sleep(2)
    except KeyboardInterrupt:
        print("\nStopped.")

if __name__ == "__main__":
    main()
