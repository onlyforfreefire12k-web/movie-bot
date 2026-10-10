"""
movie_chain.py — Multiplayer Telegram "Movie Chain" game.

Drop-in for an existing python-telegram-bot v20+ (async) project.
- Per-chat isolated state.
- Bollywood + Hollywood validation from local .txt files.
- No external API, no API keys.
- Cancellable turn timers, race-condition safe.

Usage:
    import movie_chain
    movie_chain.register(app)          # after app is built
    # on shutdown:
    # await movie_chain.cancel_all()
"""
from __future__ import annotations

import asyncio
import html
import logging
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
TURN_SECONDS = 20
MIN_PLAYERS = 2
MAX_PLAYERS = 10
POINTS_PER_MOVIE = 1

MOVIES_DIR = Path(__file__).resolve().parent / "movies"
MOVIE_FILES = ["bollywood.txt", "hollywood.txt"]

# --------------------------------------------------------------------------
# Movie database (local .txt files only)
# --------------------------------------------------------------------------
_ARTICLE_RE = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)
_NONALNUM_RE = re.compile(r"[^a-z0-9]")


def _normalize(title: str) -> str:
    """Lowercase, drop leading article, strip non-alphanumerics."""
    t = title.strip().lower()
    t = _ARTICLE_RE.sub("", t)
    return _NONALNUM_RE.sub("", t)


def _chain_letter(norm_title: str) -> Optional[str]:
    """Last alphabetic character of a normalized title."""
    for ch in reversed(norm_title):
        if ch.isalpha():
            return ch
    return None


class MovieDB:
    def __init__(self) -> None:
        self.by_norm: dict[str, str] = {}          # normalized -> display title
        self.by_letter: dict[str, list[str]] = {}  # first letter -> [normalized]

    def load(self, files: list[Path]) -> None:
        for f in files:
            try:
                raw = f.read_text(encoding="utf-8")
            except FileNotFoundError:
                logger.warning("Movie file not found: %s", f)
                continue
            except OSError as e:
                logger.warning("Could not read %s: %s", f, e)
                continue
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                norm = _normalize(line)
                if not norm:
                    continue
                self.by_norm.setdefault(norm, line)
            logger.info("Loaded movies from %s", f.name)
        for norm in self.by_norm:
            self.by_letter.setdefault(norm[0], []).append(norm)
        logger.info("Movie DB ready: %d titles", len(self.by_norm))

    def exists(self, norm: str) -> bool:
        return norm in self.by_norm

    def display(self, norm: str) -> str:
        return self.by_norm.get(norm, norm)


DB = MovieDB()

# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------
@dataclass
class Player:
    user_id: int
    name: str
    score: int = 0
    alive: bool = True
    last_movie: Optional[str] = None


@dataclass
class MovieChainGame:
    chat_id: int
    host_id: int
    host_name: str
    status: str = "lobby"                    # lobby | running | finished
    players: dict[int, Player] = field(default_factory=dict)
    order: list[int] = field(default_factory=list)
    turn_index: int = 0
    required_letter: Optional[str] = None
    used: set[str] = field(default_factory=set)
    timer_task: Optional[asyncio.Task] = None
    turn_token: int = 0
    join_message_id: Optional[int] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def current_uid(self) -> Optional[int]:
        if not self.order:
            return None
        return self.order[self.turn_index % len(self.order)]

    @property
    def current_player(self) -> Optional[Player]:
        uid = self.current_uid
        return self.players.get(uid) if uid is not None else None

    def alive_uids(self) -> list[int]:
        return [uid for uid in self.order if self.players[uid].alive]


GAMES: dict[int, MovieChainGame] = {}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
async def _safe_send(bot, chat_id: int, text: str, **kw):
    try:
        return await bot.send_message(chat_id, text, **kw)
    except (BadRequest, TelegramError) as e:
        logger.warning("send_message failed (chat=%s): %s", chat_id, e)
        return None


async def _safe_edit(bot, chat_id: int, message_id: int, text: str, **kw):
    try:
        return await bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=text, **kw
        )
    except BadRequest:
        return None
    except TelegramError as e:
        logger.warning("edit_message failed (chat=%s): %s", chat_id, e)
        return None


def _esc(name: str) -> str:
    return html.escape(name or "Player")


def _lobby_text(game: MovieChainGame) -> str:
    lines = [
        "<b>Movie Chain — Lobby</b>",
        f"Host: {_esc(game.host_name)}",
        f"Players ({len(game.players)}/{MAX_PLAYERS}):",
    ]
    for p in game.players.values():
        lines.append(f"  • {_esc(p.name)}")
    lines.append("")
    lines.append(f"Need at least {MIN_PLAYERS} players. Host presses Start.")
    return "\n".join(lines)


def _lobby_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Join", callback_data="mc:join"),
            InlineKeyboardButton("Start", callback_data="mc:start"),
            InlineKeyboardButton("Cancel", callback_data="mc:cancel"),
        ]]
    )


def _scoreboard_text(game: MovieChainGame, title: str = "Scoreboard") -> str:
    rows = sorted(game.players.values(), key=lambda p: (-p.score, p.name.lower()))
    lines = [f"<b>{_esc(title)}</b>"]
    for p in rows:
        status = "" if p.alive else " (out)"
        lines.append(f"  • {_esc(p.name)} — {p.score}{status}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Timer management
# --------------------------------------------------------------------------
def _cancel_timer(game: MovieChainGame) -> None:
    if game.timer_task and not game.timer_task.done():
        game.timer_task.cancel()
    game.timer_task = None


def _schedule_timer(app: Application, game: MovieChainGame) -> None:
    _cancel_timer(game)
    game.turn_token += 1
    token = game.turn_token
    game.timer_task = asyncio.create_task(_turn_timer(app, game.chat_id, token))


async def _turn_timer(app: Application, chat_id: int, token: int) -> None:
    try:
        await asyncio.sleep(TURN_SECONDS)
    except asyncio.CancelledError:
        return
    game = GAMES.get(chat_id)
    if not game or game.status != "running":
        return
    async with game.lock:
        if game.turn_token != token or game.status != "running":
            return
        await _eliminate_current(app, game, "ran out of time")


# --------------------------------------------------------------------------
# Turn flow  (call only while holding game.lock)
# --------------------------------------------------------------------------
def _next_alive_index(game: MovieChainGame, start: int) -> int:
    n = len(game.order)
    for step in range(1, n + 1):
        idx = (start + step) % n
        if game.players[game.order[idx]].alive:
            return idx
    return start


async def _begin_turn(app: Application, game: MovieChainGame) -> None:
    player = game.current_player
    if player is None:
        await _finish(app, game, "nowinner")
        return
    if game.required_letter:
        hint = f"Required letter: <b>{game.required_letter.upper()}</b>"
    else:
        hint = "First turn — any valid movie."
    await _safe_send(
        app.bot,
        game.chat_id,
        f"{_esc(player.name)}, it's your turn!\n{hint}\n"
        f"You have {TURN_SECONDS}s. Send the movie title.",
        parse_mode=ParseMode.HTML,
    )
    _schedule_timer(app, game)


async def _advance_turn(app: Application, game: MovieChainGame) -> None:
    if len(game.alive_uids()) <= 1:
        await _finish(app, game, "winner")
        return
    game.turn_index = _next_alive_index(game, game.turn_index)
    await _begin_turn(app, game)


async def _eliminate_current(app: Application, game: MovieChainGame, reason: str) -> None:
    player = game.current_player
    if player is None:
        await _finish(app, game, "nowinner")
        return
    player.alive = False
    _cancel_timer(game)
    await _safe_send(
        app.bot,
        game.chat_id,
        f"{_esc(player.name)} is eliminated ({reason}).",
        parse_mode=ParseMode.HTML,
    )
    if len(game.alive_uids()) <= 1:
        await _finish(app, game, "winner")
        return
    game.turn_index = _next_alive_index(game, game.turn_index)
    await _begin_turn(app, game)


async def _finish(app: Application, game: MovieChainGame, reason: str) -> None:
    game.status = "finished"
    _cancel_timer(game)

    if reason == "winner":
        alive = game.alive_uids()
        if not alive:
            await _safe_send(app.bot, game.chat_id, "No eligible players remain — no winner.")
        else:
            winner = game.players[alive[0]]
            await _safe_send(
                app.bot,
                game.chat_id,
                f"🏆 <b>{_esc(winner.name)} wins Movie Chain!</b>",
                parse_mode=ParseMode.HTML,
            )
        await _safe_send(
            app.bot,
            game.chat_id,
            _scoreboard_text(game, "Final Scoreboard"),
            parse_mode=ParseMode.HTML,
        )
    elif reason == "stopped":
        await _safe_send(
            app.bot,
            game.chat_id,
            "Movie Chain stopped by the host (no winner declared).",
        )
        await _safe_send(
            app.bot,
            game.chat_id,
            _scoreboard_text(game, "Final Scoreboard"),
            parse_mode=ParseMode.HTML,
        )
    else:  # nowinner
        await _safe_send(app.bot, game.chat_id, "No eligible players remain — no winner.")

    # Clean up so a new game can start.
    GAMES.pop(game.chat_id, None)


# --------------------------------------------------------------------------
# Submission
# --------------------------------------------------------------------------
async def _handle_submission(app: Application, game: MovieChainGame, user, text: str) -> None:
    async with game.lock:
        if game.status != "running":
            return
        if user.id != game.current_uid:
            return  # not their turn

        norm = _normalize(text)
        player = game.current_player

        if not norm:
            await _safe_send(app.bot, game.chat_id, "Send a movie title (text).")
            return
        if norm in game.used:
            await _safe_send(app.bot, game.chat_id, f"“{_esc(text)}” was already used.")
            return
        if game.required_letter and not norm.startswith(game.required_letter):
            await _safe_send(
                app.bot, game.chat_id,
                f"“{_esc(text)}” must start with "
                f"<b>{game.required_letter.upper()}</b>.",
                parse_mode=ParseMode.HTML,
            )
            return
        if not DB.exists(norm):
            await _safe_send(
                app.bot, game.chat_id,
                f"“{_esc(text)}” not found in the movie database.",
                parse_mode=ParseMode.HTML,
            )
            return

        # Accept
        game.used.add(norm)
        player.score += POINTS_PER_MOVIE
        player.last_movie = norm
        letter = _chain_letter(norm)
        game.required_letter = letter
        _cancel_timer(game)

        await _safe_send(
            app.bot, game.chat_id,
            f"✅ {_esc(player.name)}: {_esc(DB.display(norm))} "
            f"(+{POINTS_PER_MOVIE}) → next letter "
            f"<b>{(letter or '?').upper()}</b>",
            parse_mode=ParseMode.HTML,
        )
        await _advance_turn(app, game)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
async def cmd_mc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None or update.message is None:
        return

    existing = GAMES.get(chat.id)
    if existing and existing.status in ("lobby", "running"):
        await update.message.reply_text("A Movie Chain game is already active in this chat.")
        return

    game = MovieChainGame(chat_id=chat.id, host_id=user.id, host_name=user.first_name or "Host")
    game.players[user.id] = Player(user.id, user.first_name or "Host")
    GAMES[chat.id] = game

    msg = await _safe_send(
        context.bot, chat.id, _lobby_text(game),
        reply_markup=_lobby_keyboard(), parse_mode=ParseMode.HTML,
    )
    if msg:
        game.join_message_id = msg.message_id


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None or update.message is None:
        return
    game = GAMES.get(chat.id)
    if not game:
        await update.message.reply_text("No active Movie Chain game.")
        return
    if user.id != game.host_id:
        await update.message.reply_text("Only the host can stop the game.")
        return
    async with game.lock:
        await _finish(context.application, game, "stopped")


async def cmd_score(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None or update.message is None:
        return
    game = GAMES.get(chat.id)
    if not game:
        await update.message.reply_text("No active Movie Chain game.")
        return
    await update.message.reply_text(
        _scoreboard_text(game), parse_mode=ParseMode.HTML
    )


async def cmd_submit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Explicit submission: /m <movie title>"""
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None or update.message is None:
        return
    game = GAMES.get(chat.id)
    if not game or game.status != "running":
        await update.message.reply_text("No running Movie Chain game.")
        return
    text = " ".join(context.args).strip() if context.args else ""
    if not text:
        await update.message.reply_text("Usage: /m <movie title>")
        return
    await _handle_submission(context.application, game, user, text)


# --------------------------------------------------------------------------
# Callback queries
# --------------------------------------------------------------------------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    await query.answer()
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return

    action = query.data.split(":", 1)[1] if ":" in query.data else ""
    game = GAMES.get(chat.id)

    if action == "join":
        if not game or game.status != "lobby":
            await _safe_edit(context.bot, chat.id, query.message.message_id,
                             "Lobby closed.")
            return
        if user.id in game.players:
            await query.answer("Already joined.", show_alert=False)
            return
        if len(game.players) >= MAX_PLAYERS:
            await query.answer("Lobby is full.", show_alert=True)
            return
        game.players[user.id] = Player(user.id, user.first_name or "Player")
        await _safe_edit(context.bot, chat.id, query.message.message_id,
                         _lobby_text(game), reply_markup=_lobby_keyboard(),
                         parse_mode=ParseMode.HTML)
        return

    if action == "start":
        if not game or game.status != "lobby":
            return
        if user.id != game.host_id:
            await query.answer("Only the host can start.", show_alert=True)
            return
        if len(game.players) < MIN_PLAYERS:
            await query.answer(f"Need at least {MIN_PLAYERS} players.", show_alert=True)
            return
        async with game.lock:
            game.order = list(game.players.keys())
            random.shuffle(game.order)
            game.turn_index = 0
            game.status = "running"
            game.required_letter = None
        await _safe_edit(context.bot, chat.id, query.message.message_id,
                         "<b>Movie Chain started!</b>", parse_mode=ParseMode.HTML)
        await _safe_send(context.bot, chat.id, "Game on. Turns are randomized.")
        async with game.lock:
            await _begin_turn(context.application, game)
        return

    if action == "cancel":
        if not game or game.status != "lobby":
            return
        if user.id != game.host_id:
            await query.answer("Only the host can cancel.", show_alert=True)
            return
        GAMES.pop(chat.id, None)
        await _safe_edit(context.bot, chat.id, query.message.message_id,
                         "Lobby cancelled.")


# --------------------------------------------------------------------------
# Plain-text submissions (Word-Chain-safe)
# --------------------------------------------------------------------------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None or update.message is None or not update.message.text:
        return
    game = GAMES.get(chat.id)
    # Only act when a Movie Chain game is running and it's this user's turn.
    if not game or game.status != "running":
        return
    if user.id != game.current_uid:
        return
    await _handle_submission(context.application, game, user, update.message.text)


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------
def register(app: Application, group: int = 0) -> None:
    """Wire handlers into the existing dispatcher/application."""
    DB.load([MOVIES_DIR / f for f in MOVIE_FILES])

    app.add_handler(CommandHandler(["mc", "moviechain"], cmd_mc), group=group)
    app.add_handler(CommandHandler("mcstop", cmd_stop), group=group)
    app.add_handler(CommandHandler("mcscore", cmd_score), group=group)
    app.add_handler(CommandHandler("m", cmd_submit), group=group)
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^mc:"), group=group)
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, on_text), group=group
    )
    logger.info("Movie Chain registered.")


async def cancel_all() -> None:
    """Call on shutdown to kill every live timer."""
    for game in list(GAMES.values()):
        _cancel_timer(game)
    GAMES.clear()
