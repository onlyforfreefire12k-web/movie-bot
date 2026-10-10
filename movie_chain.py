"""
movie_chain.py — Multiplayer Telegram "Movie Chain" game (+ solo vs bots).

Drop-in for an existing python-telegram-bot v20+ (async) project.
- Per-chat isolated state, cancellable timers, race-safe.
- Bollywood + Hollywood validation from local .txt files. No API/keys.
- Solo mode vs AI bots with difficulty levels; bots never repeat used movies.

Usage:
    import movie_chain
    movie_chain.register(app)
    # on shutdown: await movie_chain.cancel_all()
"""
from __future__ import annotations

import asyncio
import html
import itertools
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

BOT_NAMES = [
    "Bot Raj", "Bot Simran", "Bot Vikram", "Bot Meera", "Bot Arjun",
    "Bot Nisha", "Bot Kabir", "Bot Ananya", "Bot Dev", "Bot Priya",
]
_BOT_IDS = itertools.count(-1, -1)

DIFFICULTIES = {
    "easy":   {"accuracy": 0.70, "min_delay": 3.0, "max_delay": 6.0, "smart": False},
    "medium": {"accuracy": 0.88, "min_delay": 2.0, "max_delay": 4.5, "smart": False},
    "hard":   {"accuracy": 0.98, "min_delay": 1.5, "max_delay": 3.5, "smart": True},
}
DEFAULT_DIFFICULTY = "medium"

# --------------------------------------------------------------------------
# Movie database
# --------------------------------------------------------------------------
_ARTICLE_RE = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)
_NONALNUM_RE = re.compile(r"[^a-z0-9]")


def _normalize(title: str) -> str:
    t = title.strip().lower()
    t = _ARTICLE_RE.sub("", t)
    return _NONALNUM_RE.sub("", t)


def _chain_letter(norm_title: str) -> Optional[str]:
    for ch in reversed(norm_title):
        if ch.isalpha():
            return ch
    return None


class MovieDB:
    def __init__(self) -> None:
        self.by_norm: dict[str, str] = {}
        self.by_letter: dict[str, list[str]] = {}

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
                if norm:
                    self.by_norm.setdefault(norm, line)
            logger.info("Loaded movies from %s", f.name)
        for norm in self.by_norm:
            self.by_letter.setdefault(norm[0], []).append(norm)
        logger.info("Movie DB ready: %d titles", len(self.by_norm))

    def exists(self, norm: str) -> bool:
        return norm in self.by_norm

    def display(self, norm: str) -> str:
        return self.by_norm.get(norm, norm)

    def _followups(self, norm: str, used: set[str]) -> int:
        letter = _chain_letter(norm)
        if not letter:
            return 0
        return sum(1 for m in self.by_letter.get(letter, []) if m not in used)

    def choose(self, letter: Optional[str], used: set[str], smart: bool) -> Optional[str]:
        """Pick an unused movie. Smart bots strand the opponent (fewest follow-ups)."""
        pool = self.by_letter.get(letter, []) if letter else list(self.by_norm)
        options = [n for n in pool if n not in used]
        if not options:
            return None
        if not smart:
            return random.choice(options)
        scored = [(self._followups(n, used | {n}), n) for n in options]
        best = min(s for s, _ in scored)
        return random.choice([n for s, n in scored if s == best])


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
    is_bot: bool = False


@dataclass
class MovieChainGame:
    chat_id: int
    host_id: int
    host_name: str
    status: str = "lobby"                 # lobby | running | finished
    difficulty: str = DEFAULT_DIFFICULTY
    players: dict[int, Player] = field(default_factory=dict)
    order: list[int] = field(default_factory=list)
    turn_index: int = 0
    required_letter: Optional[str] = None
    used: set[str] = field(default_factory=set)
    timer_task: Optional[asyncio.Task] = None
    bot_task: Optional[asyncio.Task] = None
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
        return await bot.edit_message_text(chat_id=chat_id, message_id=message_id,
                                           text=text, **kw)
    except BadRequest:
        return None
    except TelegramError as e:
        logger.warning("edit_message failed (chat=%s): %s", chat_id, e)
        return None


def _esc(name: str) -> str:
    return html.escape(name or "Player")

# --------------------------------------------------------------------------
# Texts
# --------------------------------------------------------------------------
WELCOME_TEXT = (
    "🎬 <b>Movie Chain — Welcome!</b>\n\n"
    "Each player names a movie. The next player must name a movie that starts "
    "with the <b>last letter</b> of the previous one.\n\n"
    "⏱ <b>20 seconds</b> per turn — time out means eliminated.\n"
    "🏆 Last player standing wins.\n"
    "🤖 Want to play alone? Tap <b>Play Solo</b>.\n\n"
    "Full guide: /mchelp"
)

HELP_TEXT = (
    "<b>🎬 Movie Chain — How to Play</b>\n\n"
    "<b>Setup (in a group)</b>\n"
    "• /mc — create a lobby (you become host)\n"
    "• Others tap <b>Join</b> (2–10 players)\n"
    "• Host can tap <b>Add Bot</b> to add AI players\n"
    "• Host taps <b>Start</b> — turn order is randomized\n\n"
    "<b>Solo (alone)</b>\n"
    "• /solo — you vs 3 bots\n"
    "• /solo easy | medium | hard\n\n"
    "<b>Turns</b>\n"
    "• On your turn, send a movie title — plain text or /m &lt;movie&gt;\n"
    "• The movie must start with the <b>LAST letter</b> of the previous one\n"
    "• A movie cannot be used twice\n"
    "• Bollywood and Hollywood are both valid\n\n"
    "<b>Timer</b>\n"
    f"• Send within {TURN_SECONDS}s or you're out\n"
    "• Last player standing wins 🏆\n\n"
    "<b>Other</b>\n"
    "• /mcscore — show the scoreboard\n"
    "• /mcstop — host stops the game (no winner)\n"
)


def _lobby_text(game: MovieChainGame) -> str:
    lines = [
        "<b>🎬 Movie Chain — Lobby</b>",
        f"Host: {_esc(game.host_name)}",
        f"Bot difficulty: {game.difficulty}",
        f"Players ({len(game.players)}/{MAX_PLAYERS}):",
    ]
    for p in game.players.values():
        lines.append(f"  • {_esc(p.name)}" + (" 🤖" if p.is_bot else ""))
    lines.append("")
    lines.append(f"Need at least {MIN_PLAYERS} players. Host taps Start.")
    return "\n".join(lines)


def _lobby_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Join", callback_data="mc:join"),
         InlineKeyboardButton("Add Bot", callback_data="mc:addbot")],
        [InlineKeyboardButton("Start", callback_data="mc:start"),
         InlineKeyboardButton("Cancel", callback_data="mc:cancel")],
    ])


def _start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 Play Solo", callback_data="mc:solo"),
         InlineKeyboardButton("👥 Create Game", callback_data="mc:new")],
        [InlineKeyboardButton("📖 How to Play", callback_data="mc:help")],
    ])


def _scoreboard_text(game: MovieChainGame, title: str = "Scoreboard") -> str:
    rows = sorted(game.players.values(), key=lambda p: (-p.score, p.name.lower()))
    lines = [f"<b>{_esc(title)}</b>"]
    for p in rows:
        mark = "" if p.alive else " (out)"
        bot = " 🤖" if p.is_bot else ""
        lines.append(f"  • {_esc(p.name)}{bot} — {p.score}{mark}")
    return "\n".join(lines)

# --------------------------------------------------------------------------
# Timer + bot helpers
# --------------------------------------------------------------------------
def _cancel_timer(game: MovieChainGame) -> None:
    if game.timer_task and not game.timer_task.done():
        game.timer_task.cancel()
    game.timer_task = None


def _cancel_bot(game: MovieChainGame) -> None:
    if game.bot_task and not game.bot_task.done():
        game.bot_task.cancel()
    game.bot_task = None


def _schedule_timer(app: Application, game: MovieChainGame) -> None:
    _cancel_timer(game)
    game.turn_token += 1
    token = game.turn_token
    game.timer_task = asyncio.create_task(_turn_timer(app, game.chat_id, token))


def _add_bot(game: MovieChainGame) -> int:
    bid = next(_BOT_IDS)
    base = random.choice(BOT_NAMES)
    name, n, existing = base, 2, {p.name for p in game.players.values()}
    while name in existing:
        name = f"{base} {n}"
        n += 1
    game.players[bid] = Player(bid, name, is_bot=True)
    return bid


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


async def _bot_turn(app: Application, chat_id: int, token: int) -> None:
    game = GAMES.get(chat_id)
    if not game:
        return
    cfg = DIFFICULTIES.get(game.difficulty, DIFFICULTIES[DEFAULT_DIFFICULTY])
    try:
        await asyncio.sleep(random.uniform(cfg["min_delay"], cfg["max_delay"]))
    except asyncio.CancelledError:
        return
    game = GAMES.get(chat_id)
    if not game or game.status != "running":
        return
    async with game.lock:
        if game.turn_token != token or game.status != "running":
            return
        player = game.current_player
        if not player or not player.is_bot:
            return
        if random.random() > cfg["accuracy"]:
            return  # bot "miss" -> the timer will eliminate it
        movie = DB.choose(game.required_letter, game.used, smart=cfg["smart"])
        if movie is None:
            return
        await _accept_movie(app, game, player, movie)

# --------------------------------------------------------------------------
# Turn flow (hold game.lock)
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
    hint = (f"Required letter: <b>{game.required_letter.upper()}</b>"
            if game.required_letter else "First turn — any valid movie.")
    who = _esc(player.name) + (" 🤖" if player.is_bot else "")
    await _safe_send(
        app.bot, game.chat_id,
        f"{who}, it's your turn!\n{hint}\nYou have {TURN_SECONDS}s.",
        parse_mode=ParseMode.HTML,
    )
    _schedule_timer(app, game)
    if player.is_bot:
        game.bot_task = asyncio.create_task(
            _bot_turn(app, game.chat_id, game.turn_token)
        )


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
    _cancel_bot(game)
    await _safe_send(app.bot, game.chat_id,
                     f"{_esc(player.name)} is eliminated ({reason}).",
                     parse_mode=ParseMode.HTML)
    if len(game.alive_uids()) <= 1:
        await _finish(app, game, "winner")
        return
    game.turn_index = _next_alive_index(game, game.turn_index)
    await _begin_turn(app, game)


async def _accept_movie(app: Application, game: MovieChainGame,
                        player: Player, norm: str) -> None:
    game.used.add(norm)
    player.score += POINTS_PER_MOVIE
    player.last_movie = norm
    letter = _chain_letter(norm)
    game.required_letter = letter
    _cancel_timer(game)
    _cancel_bot(game)
    await _safe_send(
        app.bot, game.chat_id,
        f"✅ {_esc(player.name)}: {_esc(DB.display(norm))} (+{POINTS_PER_MOVIE}) "
        f"→ next letter <b>{(letter or '?').upper()}</b>",
        parse_mode=ParseMode.HTML,
    )
    await _advance_turn(app, game)


async def _finish(app: Application, game: MovieChainGame, reason: str) -> None:
    game.status = "finished"
    _cancel_timer(game)
    _cancel_bot(game)

    if reason == "winner":
        alive = game.alive_uids()
        if not alive:
            await _safe_send(app.bot, game.chat_id, "No eligible players remain — no winner.")
        else:
            winner = game.players[alive[0]]
            await _safe_send(app.bot, game.chat_id,
                             f"🏆 <b>{_esc(winner.name)} wins Movie Chain!</b>",
                             parse_mode=ParseMode.HTML)
        await _safe_send(app.bot, game.chat_id,
                         _scoreboard_text(game, "Final Scoreboard"),
                         parse_mode=ParseMode.HTML)
    elif reason == "stopped":
        await _safe_send(app.bot, game.chat_id,
                         "Movie Chain stopped by the host (no winner declared).")
        await _safe_send(app.bot, game.chat_id,
                         _scoreboard_text(game, "Final Scoreboard"),
                         parse_mode=ParseMode.HTML)
    else:
        await _safe_send(app.bot, game.chat_id, "No eligible players remain — no winner.")

    GAMES.pop(game.chat_id, None)

# --------------------------------------------------------------------------
# Submission
# --------------------------------------------------------------------------
async def _handle_submission(app: Application, game: MovieChainGame, user, text: str) -> None:
    async with game.lock:
        if game.status != "running":
            return
        if user.id != game.current_uid:
            return

        norm = _normalize(text)
        player = game.current_player
        if not norm:
            await _safe_send(app.bot, game.chat_id, "Send a movie title (text).")
            return
        if norm in game.used:
            await _safe_send(app.bot, game.chat_id, f"“{_esc(text)}” was already used.")
            return
        if game.required_letter and not norm.startswith(game.required_letter):
            await _safe_send(app.bot, game.chat_id,
                             f"“{_esc(text)}” must start with "
                             f"<b>{game.required_letter.upper()}</b>.",
                             parse_mode=ParseMode.HTML)
            return
        if not DB.exists(norm):
            await _safe_send(app.bot, game.chat_id,
                             f"“{_esc(text)}” not found in the movie database.",
                             parse_mode=ParseMode.HTML)
            return
        await _accept_movie(app, game, player, norm)

# --------------------------------------------------------------------------
# Game-start helpers (used by commands AND callback buttons)
# --------------------------------------------------------------------------
async def _create_lobby(app: Application, chat, user) -> None:
    existing = GAMES.get(chat.id)
    if existing and existing.status in ("lobby", "running"):
        await _safe_send(app.bot, chat.id, "A Movie Chain game is already active in this chat.")
        return
    game = MovieChainGame(chat.id, user.id, user.first_name or "Host")
    game.players[user.id] = Player(user.id, user.first_name or "Host")
    GAMES[chat.id] = game
    msg = await _safe_send(app.bot, chat.id, _lobby_text(game),
                           reply_markup=_lobby_keyboard(), parse_mode=ParseMode.HTML)
    if msg:
        game.join_message_id = msg.message_id


async def _start_solo(app: Application, chat, user, difficulty: str) -> None:
    if (g := GAMES.get(chat.id)) and g.status in ("lobby", "running"):
        await _safe_send(app.bot, chat.id, "A game is already running in this chat.")
        return
    if difficulty not in DIFFICULTIES:
        difficulty = DEFAULT_DIFFICULTY
    game = MovieChainGame(chat.id, user.id, user.first_name or "You", difficulty=difficulty)
    game.players[user.id] = Player(user.id, user.first_name or "You")
    for _ in range(3):
        _add_bot(game)
    game.order = list(game.players.keys())
    random.shuffle(game.order)
    game.turn_index = 0
    game.status = "running"
    GAMES[chat.id] = game
    await _safe_send(app.bot, chat.id, f"Solo mode ({difficulty}): you vs 3 bots. Go!")
    async with game.lock:
        await _begin_turn(app, game)

# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text(
        WELCOME_TEXT, parse_mode=ParseMode.HTML, reply_markup=_start_keyboard()
    )


async def cmd_mc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat, user = update.effective_chat, update.effective_user
    if chat is None or user is None or update.message is None:
        return
    await _create_lobby(context.application, chat, user)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def cmd_solo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat, user = update.effective_chat, update.effective_user
    if chat is None or user is None or update.message is None:
        return
    difficulty = (context.args[0].lower() if context.args else DEFAULT_DIFFICULTY)
    await _start_solo(context.application, chat, user, difficulty)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat, user = update.effective_chat, update.effective_user
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
    await update.message.reply_text(_scoreboard_text(game), parse_mode=ParseMode.HTML)


async def cmd_submit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat, user = update.effective_chat, update.effective_user
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
# Callbacks
# --------------------------------------------------------------------------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    await query.answer()
    chat, user = update.effective_chat, update.effective_user
    if chat is None or user is None:
        return
    action = query.data.split(":", 1)[1] if ":" in query.data else ""
    game = GAMES.get(chat.id)

    if action == "solo":
        await _start_solo(context.application, chat, user, DEFAULT_DIFFICULTY)
        return
    if action == "new":
        await _create_lobby(context.application, chat, user)
        return
    if action == "help":
        await _safe_send(context.bot, chat.id, HELP_TEXT, parse_mode=ParseMode.HTML)
        return

    if action == "join":
        if not game or game.status != "lobby":
            await _safe_edit(context.bot, chat.id, query.message.message_id, "Lobby closed.")
            return
        if user.id in game.players:
            await query.answer("Already joined.")
            return
        if len(game.players) >= MAX_PLAYERS:
            await query.answer("Lobby is full.", show_alert=True)
            return
        game.players[user.id] = Player(user.id, user.first_name or "Player")
        await _safe_edit(context.bot, chat.id, query.message.message_id,
                         _lobby_text(game), reply_markup=_lobby_keyboard(),
                         parse_mode=ParseMode.HTML)
        return

    if action == "addbot":
        if not game or game.status != "lobby":
            return
        if user.id != game.host_id:
            await query.answer("Only the host can add bots.", show_alert=True)
            return
        if len(game.players) >= MAX_PLAYERS:
            await query.answer("Lobby is full.", show_alert=True)
            return
        _add_bot(game)
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
        while len(game.players) < MIN_PLAYERS:
            _add_bot(game)
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
        await _safe_edit(context.bot, chat.id, query.message.message_id, "Lobby cancelled.")

# --------------------------------------------------------------------------
# Text handler (Word-Chain-safe)
# --------------------------------------------------------------------------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat, user = update.effective_chat, update.effective_user
    if chat is None or user is None or update.message is None or not update.message.text:
        return
    game = GAMES.get(chat.id)
    if not game or game.status != "running":
        return
    if user.id != game.current_uid:
        return
    await _handle_submission(context.application, game, user, update.message.text)

# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------
def register(app: Application, group: int = 0) -> None:
    DB.load([MOVIES_DIR / f for f in MOVIE_FILES])
    app.add_handler(CommandHandler(["start"], cmd_start), group=group)
    app.add_handler(CommandHandler(["mc", "moviechain"], cmd_mc), group=group)
    app.add_handler(CommandHandler(["mchelp", "help"], cmd_help), group=group)
    app.add_handler(CommandHandler("solo", cmd_solo), group=group)
    app.add_handler(CommandHandler("mcstop", cmd_stop), group=group)
    app.add_handler(CommandHandler("mcscore", cmd_score), group=group)
    app.add_handler(CommandHandler("m", cmd_submit), group=group)
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^mc:"), group=group)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text), group=group)
    logger.info("Movie Chain registered.")


async def cancel_all() -> None:
    for game in list(GAMES.values()):
        _cancel_timer(game)
        _cancel_bot(game)
    GAMES.clear()
