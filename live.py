# live.py
import logging
import os
import threading

from flask import Flask
from telegram.ext import Application

# --- your existing imports ---
# from your_project import register_word_chain, build_app, ... 
import movie_chain

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("live")

# --------------------------------------------------------------------------
# Flask health server (ADDED — health only, no bot logic here)
# --------------------------------------------------------------------------
web = Flask(__name__)


@web.get("/")
def health():
    # Simple 200 so Render's health check passes.
    return "Movie Chain bot is running.", 200


@web.get("/healthz")
def healthz():
    return {"status": "ok"}, 200


def _serve_web() -> None:
    port = int(os.environ.get("PORT", 10000))
    logger.info("Health web server on 0.0.0.0:%d", port)
    # use_reloader=False -> no second process, no duplicate polling.
    web.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)


# --------------------------------------------------------------------------
# Bot lifecycle (ADDED hooks; reuse your existing build/register code)
# --------------------------------------------------------------------------
async def post_init(app: Application) -> None:
    me = await app.bot.get_me()
    logger.info("Bot online as @%s", me.username)


async def post_shutdown(app: Application) -> None:
    # Kill every live Movie Chain timer before exit (Render sends SIGTERM).
    await movie_chain.cancel_all()
    logger.info("Shutdown complete; timers cancelled.")


def build_app() -> Application:
    token = os.environ["TELEGRAM_BOT_TOKEN"]  # reuse existing var name
    app = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # --- your existing registrations (Word Chain etc.) stay here ---
    register_word_chain(app)

    # --- new game, fully isolated per chat ---
    movie_chain.register(app)
    return app


def main() -> None:
    # Start web server FIRST so Render health checks succeed immediately.
    threading.Thread(target=_serve_web, name="flask", daemon=True).start()

    app = build_app()
    try:
        app.run_polling(drop_pending_updates=True)
    except Exception:
        logger.exception("Fatal error in bot loop")
        raise


if __name__ == "__main__":
    main()
