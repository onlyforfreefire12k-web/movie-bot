# live.py
import importlib
import logging
import os
import threading

from flask import Flask
from telegram.ext import Application

import movie_chain

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("live")

# --------------------------------------------------------------------------
# Flask health server (health only)
# --------------------------------------------------------------------------
web = Flask(__name__)


@web.get("/")
def health():
    return "Movie Chain bot is running.", 200


@web.get("/healthz")
def healthz():
    return {"status": "ok"}, 200


def _serve_web() -> None:
    port = int(os.environ.get("PORT", 10000))
    logger.info("Health web server on 0.0.0.0:%d", port)
    try:
        from waitress import serve  # production WSGI, kills the dev-server warning
        serve(web, host="0.0.0.0", port=port, threads=4)
    except ImportError:
        web.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)


# --------------------------------------------------------------------------
# Bot lifecycle
# --------------------------------------------------------------------------
async def post_init(app: Application) -> None:
    me = await app.bot.get_me()
    logger.info("Bot online as @%s", me.username)


async def post_shutdown(app: Application) -> None:
    await movie_chain.cancel_all()
    logger.info("Shutdown complete; timers cancelled.")


def _find_register_word_chain():
    """Try common module/function names for the existing Word Chain registration."""
    candidates = [
        ("word_chain", "register_word_chain"),
        ("wordchain", "register_word_chain"),
        ("word_chain", "register"),
        ("bot", "register_word_chain"),
        ("handlers", "register_word_chain"),
        ("game", "register_word_chain"),
    ]
    for modname, funcname in candidates:
        try:
            mod = importlib.import_module(modname)
        except ImportError:
            continue
        fn = getattr(mod, funcname, None)
        if callable(fn):
            logger.info("Found Word Chain register: %s.%s", modname, funcname)
            return fn
    return None


def build_app() -> Application:
    token = os.environ["TELEGRAM_BOT_TOKEN"]  # rename if your var differs
    app = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    wc = _find_register_word_chain()
    if wc:
        wc(app)
    else:
        logger.warning(
            "Word Chain register not found — skipping. "
            "Set it manually in build_app() if needed."
        )

    movie_chain.register(app)
    return app


def main() -> None:
    threading.Thread(target=_serve_web, name="flask", daemon=True).start()
    app = build_app()
    try:
        app.run_polling(drop_pending_updates=True)
    except Exception:
        logger.exception("Fatal error in bot loop")
        raise


if __name__ == "__main__":
    main()
