import os
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv, set_key

from helpers.ultravox import create_agent, patch_agent, list_webhooks, register_webhook
from helpers.prompts import SYSTEM_PROMPT
import helpers.db as db
from routers.call import router as call_router
from routers.outbound import router as outbound_router
from routers.logs import router as logs_router
from routers.webhook import router as webhook_router

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")


async def ensure_webhook(agent_id: str):
    """
    On startup: register the Ultravox webhook if not already registered.
    Checks existing webhooks first to avoid duplicates on every redeploy.
    Stores the webhookId in .env / env so it's only registered once.
    """
    webhook_id = os.getenv("WEBHOOK_ID", "").strip()
    if webhook_id:
        logger.info(f"Webhook already registered: {webhook_id} — skipping")
        return

    backend_url    = os.getenv("BACKEND_URL", "").strip().rstrip("/")
    webhook_secret = os.getenv("WEBHOOK_SECRET", "").strip()

    if not backend_url:
        logger.warning("BACKEND_URL not set — skipping webhook registration")
        return
    if not webhook_secret:
        logger.warning("WEBHOOK_SECRET not set — skipping webhook registration")
        return

    webhook_url = f"{backend_url}/webhook/ultravox"

    # Check if a webhook for this agent already exists (e.g. from a previous deploy)
    try:
        existing = await list_webhooks(agent_id)
        for wh in existing:
            if wh.get("url") == webhook_url:
                wh_id = wh["webhookId"]
                logger.info(f"Webhook already exists on Ultravox: {wh_id} — saving to env")
                os.environ["WEBHOOK_ID"] = wh_id
                try:
                    set_key(ENV_PATH, "WEBHOOK_ID", wh_id)
                except Exception:
                    pass
                return
    except Exception as e:
        logger.warning(f"Could not check existing webhooks: {e}")

    # Register a new webhook
    try:
        wh = await register_webhook(
            url=webhook_url,
            agent_id=agent_id,
            secret=webhook_secret,
        )
        wh_id = wh["webhookId"]
        logger.info(f"✅ Webhook registered: {wh_id} → {webhook_url}")
        os.environ["WEBHOOK_ID"] = wh_id
        try:
            set_key(ENV_PATH, "WEBHOOK_ID", wh_id)
        except Exception:
            logger.warning(
                f"Could not write WEBHOOK_ID to .env. "
                f"Set WEBHOOK_ID={wh_id} in your Railway environment variables."
            )
    except Exception as e:
        logger.error(f"Failed to register webhook: {e}")


async def ensure_agent():
    """
    On startup: check if AGENT_ID exists in .env.
    If not, create a new agent via Ultravox API and write the agentId back to .env.
    If yes, verify the agent still exists on Ultravox.
    """
    agent_id = os.getenv("AGENT_ID", "").strip()

    if agent_id:
        # Sync the full callTemplate on every startup so voice, model, selectedTools,
        # and all other settings always reflect .env.
        logger.info(f"Agent ID found: {agent_id} — syncing full config...")
        try:
            agent = await patch_agent(
                agent_id,
                system_prompt = SYSTEM_PROMPT,
                voice         = os.getenv("VOICE", "Mark"),
                model         = os.getenv("MODEL", "fixie-ai/ultravox-70B"),
                # language_hint = os.getenv("LANGUAGE_HINT", "en"),
                max_duration  = os.getenv("MAX_DURATION", "3600s"),
                corpus_id     = os.getenv("CORPUS_ID", ""),
            )
            logger.info(
                f"Agent '{agent['name']}' config synced "
                f"(voice={os.getenv('VOICE')}, corpus={os.getenv('CORPUS_ID')})"
            )
        except Exception as e:
            logger.warning(f"Could not sync agent config: {e}")
        return

    # No AGENT_ID — create one
    logger.info("No AGENT_ID found. Creating a new Ultravox agent...")

    name          = os.getenv("AGENT_NAME", "PromptorVoiceBot")
    voice         = os.getenv("VOICE", "Mark")
    model         = os.getenv("MODEL", "fixie-ai/ultravox-70B")
    # language_hint = os.getenv("LANGUAGE_HINT", "en")
    max_duration  = os.getenv("MAX_DURATION", "3600s")
    corpus_id     = os.getenv("CORPUS_ID", "")

    agent = await create_agent(
        name=name,
        system_prompt=SYSTEM_PROMPT,
        voice=voice,
        model=model,
        # language_hint=language_hint,
        max_duration=max_duration,
        corpus_id=corpus_id,
    )

    new_agent_id = agent["agentId"]
    logger.info(f"Agent created successfully: '{agent['name']}' (id={new_agent_id})")

    os.environ["AGENT_ID"] = new_agent_id
    # Try to persist to .env (works locally; silently skipped on ephemeral filesystems like Railway)
    try:
        set_key(ENV_PATH, "AGENT_ID", new_agent_id)
        logger.info("AGENT_ID saved to .env")
    except Exception:
        logger.warning(
            f"Could not write AGENT_ID to .env (ephemeral filesystem?). "
            f"Set AGENT_ID={new_agent_id} as an environment variable in your hosting dashboard."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # DB first — other startup steps may need it
    await db.init_pool()
    await db.create_tables()
    await db.mark_failed_initiated_calls()  # clean up any stale 'initiated' from crashed run

    await ensure_agent()
    agent_id = os.getenv("AGENT_ID", "").strip().strip("'\"")
    if agent_id:
        await ensure_webhook(agent_id)
    yield

    await db.close_pool()


app = FastAPI(
    title="Venkanna Motors Voice Bot",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(call_router)
app.include_router(outbound_router)
app.include_router(logs_router)
app.include_router(webhook_router)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "agent_id": os.getenv("AGENT_ID", "not_set"),
        "agent_name": os.getenv("AGENT_NAME", "not_set"),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", 8000)),
        reload=False,
    )
