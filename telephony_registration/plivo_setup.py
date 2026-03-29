import asyncio
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

ULTRAVOX_BASE_URL = "https://api.ultravox.ai/api"


async def register_plivo_credentials() -> None:
    api_key = os.getenv("ULTRAVOX_API_KEY", "").strip()
    auth_id = os.getenv("PLIVO_AUTH_ID", "").strip()
    auth_token = os.getenv("PLIVO_AUTH_TOKEN", "").strip()

    # Validate required values
    missing = []
    if not api_key:
        missing.append("ULTRAVOX_API_KEY")
    if not auth_id:
        missing.append("PLIVO_AUTH_ID")
    if not auth_token:
        missing.append("PLIVO_AUTH_TOKEN")

    if missing:
        print(f"❌ Missing in .env: {', '.join(missing)}")
        return

    print("Registering Plivo credentials with Ultravox...")
    print(f"  Auth ID : {auth_id[:10]}...")

    payload = {
        "plivo": {
            "authId": auth_id,
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
        print("✅ Plivo credentials registered successfully!")
        print("   You can now make outbound calls using Plivo.")
    elif response.status_code == 409:
        print("ℹ️  Credentials already registered.")
    else:
        print(f"❌ Failed: HTTP {response.status_code}")
        print(f"   Response: {response.text}")


if __name__ == "__main__":
    asyncio.run(register_plivo_credentials())