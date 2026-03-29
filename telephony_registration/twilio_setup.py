"""
One-time setup script: registers your Twilio credentials with Ultravox.

This must be run ONCE before making any outbound calls.
Ultravox will store your credentials securely and use them whenever
an outbound call is created with medium: { twilio: { outgoing: {...} } }

Usage:
    python setup_telephony.py
"""

import asyncio
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

ULTRAVOX_BASE_URL = "https://api.ultravox.ai/api"


async def register_twilio_credentials() -> None:
    api_key = os.getenv("ULTRAVOX_API_KEY", "").strip()
    account_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()

    # Validate all required values are present
    missing = []
    if not api_key:
        missing.append("ULTRAVOX_API_KEY")
    if not account_sid:
        missing.append("TWILIO_ACCOUNT_SID")
    if not auth_token:
        missing.append("TWILIO_AUTH_TOKEN")

    if missing:
        print(f"❌ Missing in .env: {', '.join(missing)}")
        return

    print("Registering Twilio credentials with Ultravox...")
    print(f"  Account SID : {account_sid[:12]}...")

    payload = {
        "twilio": {
            "accountSid": account_sid,
            "authToken": auth_token,
            "callCreationAllowAllAgents": True,
        }
    }

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.patch(
            f"{ULTRAVOX_BASE_URL}/accounts/me/telephony_config",
            headers={
                "X-API-Key": api_key,
                "Content-Type": "application/json",
            },
            json=payload,
        )

    if response.status_code in (200, 201):
        print("✅ Twilio credentials registered with Ultravox successfully!")
        print("   You can now make outbound calls using POST /outbound/call")
    elif response.status_code == 409:
        print("ℹ️  Credentials already registered. No action needed.")
    else:
        print(f"❌ Failed: HTTP {response.status_code}")
        print(f"   Response: {response.text}")


if __name__ == "__main__":
    asyncio.run(register_twilio_credentials())
