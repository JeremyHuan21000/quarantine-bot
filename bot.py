import os
import json
import sqlite3
import asyncio
import datetime
from dotenv import load_dotenv

load_dotenv()

import discord
from discord import app_commands
from discord.ext import commands, tasks

import football_api
import question_gen

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
QUARANTINE_ROLE_ID = int(os.getenv("QUARANTINE_ROLE_ID", "0"))
QUESTION_CHANNEL_ID = int(os.getenv("QUESTION_CHANNEL_ID", "0"))
ANNOUNCE_CHANNEL_ID = int(os.getenv("ANNOUNCE_CHANNEL_ID", "0"))
STREAK_TARGET = int(os.getenv("STREAK_TARGET", "7"))
MOD_ROLE_IDS = {
    int(r.strip()) for r in os.getenv("MOD_ROLE_IDS", "").split(",") if r.strip()
}
DAILY_POST_HOUR_UTC = int(os.getenv("DAILY_POST_HOUR_UTC", "10"))

DB_PATH = "quarantine.db"

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)


# ---------- DB setup ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS streaks (
            user_id INTEGER PRIMARY KEY,
            streak INTEGER DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS current_question (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            question TEXT,
            options TEXT,
            match_id INTEGER,
            qtype TEXT,
            extra TEXT,
            active INTEGER DEFAULT 0,
            auto INTEGER DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS picks (
            user_id INTEGER,
            choice TEXT,
            PRIMARY KEY (user_id)
        )
    """)
    conn.commit()
    conn.close()


def get_streak(user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT streak FROM streaks WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0


def set_streak(user_id: int, value: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO streaks (user_id, streak) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET streak = ?",
        (user_id, value, value),
    )
    conn.commit()
    conn.close()


def clear_picks():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM picks")
    conn.commit()
    conn.close()


def set_question(question_text, options, match_id=None, qtype=None, extra=None, auto=False):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM current_question")
    c.execute(
        "INSERT INTO current_question (id, question, options, match_id, qtype, extra, active, auto) "
        "VALUES (1, ?, ?, ?, ?, ?, 1, ?)",
        (question_text, json.dumps(options), match_id, qtype, json.dumps(extra or {}), int(auto)),
    )
    conn.commit()
    conn.close()
    clear_picks()


def get_question():
    """Returns a dict or None. Keys: question, options (list), match_id, qtype, extra (dict), active, auto"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT question, options, match_id, qtype, extra, active, auto FROM current_question WHERE id = 1"
    )
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "question": row[0],
        "options": json.loads(row[1]) if row[1] else [],
        "match_id": row[2],
        "qtype": row[3],
        "extra": json.loads(row[4]) if row[4] else {},
        "active": bool(row[5]),
        "auto": bool(row[6]),
    }


def close_question():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE current_question SET active = 0 WHERE id = 1")
    conn.commit()
    conn.close()


def record_pick(user_id: int, choice: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO picks (user_id, choice) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET choice = ?",
        (user_id, choice, choice),
    )
    conn.commit()
    conn.close()


def get_all_picks():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id, choice FROM picks")
    rows = c.fetchall()
    conn.close()
    return rows


def get_user_pick(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT choice FROM picks WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None


def get_leaderboard():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id, streak FROM streaks ORDER BY streak DESC")
    rows = c.fetchall()
    conn.close()
    return rows


def is_mod(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    if MOD_ROLE_IDS:
        return any(r.id in MOD_ROLE_IDS for r in interaction.user.roles)
    return False


def is_quarantined(member: discord.Member) -> bool:
    return any(r.id == QUARANTINE_ROLE_ID for r in member.roles)


# ---------- UI: pick buttons ----------
BUTTON_STYLES = [
    discord.ButtonStyle.primary,
    discord.ButtonStyle.secondary,
    discord.ButtonStyle.success,
]


class PickView(discord.ui.View):
    def __init__(self, options: list):
        super().__init__(timeout=None)
        for i, opt in enumerate(options):
            style = BUTTON_STYLES[i % len(BUTTON_STYLES)]
            self.add_item(PickButton(opt, style))


LOCK_GRACE_MINUTES = 5  # allow a short grace window after kickoff before locking


class PickButton(discord.ui.Button):
    def __init__(self, label: str, style: discord.ButtonStyle):
        super().__init__(label=label, style=style, custom_id=f"pick_{label}")

    async def callback(self, interaction: discord.Interaction):
        member = interaction.user
        if not is_quarantined(member):
            await interaction.response.send_message(
                "Only quarantined members can pick.", ephemeral=True
            )
            return

        q = get_question()
        if not q or not q["active"]:
            await interaction.response.send_message(
                "No active question right now.", ephemeral=True
            )
            return

        kickoff_str = q["extra"].get("kickoff_utc")
        if kickoff_str:
            kickoff = datetime.datetime.fromisoformat(kickoff_str.replace("Z", "+00:00"))
            lock_time = kickoff + datetime.timedelta(minutes=LOCK_GRACE_MINUTES)
            now = datetime.datetime.now(datetime.timezone.utc)
            if now >= lock_time:
                existing_pick = get_user_pick(member.id)
                if existing_pick:
                    msg = f"Picks are locked - kickoff has passed. Your locked-in pick: **{existing_pick}**"
                else:
                    msg = "Picks are locked - kickoff has passed. You didn't lock in a pick, so this one counts as missed."
                await interaction.response.send_message(msg, ephemeral=True)
                return

        record_pick(member.id, self.label)
        await interaction.response.send_message(
            f"Locked in: **{self.label}**. Good luck.", ephemeral=True
        )


async def grade_and_announce(guild: discord.Guild, correct_option: str, title: str):
    """Shared grading logic used by both manual /resolve and the auto-resolve loop."""
    role = guild.get_role(QUARANTINE_ROLE_ID)
    quarantined_members = [m for m in guild.members if role in m.roles]
    picks = dict(get_all_picks())

    released = []
    results_lines = []

    for member in quarantined_members:
        user_pick = picks.get(member.id)
        current = get_streak(member.id)

        if user_pick and user_pick.strip().lower() == correct_option.strip().lower():
            new_streak = current + 1
            results_lines.append(f"✅ {member.display_name}: {new_streak}/{STREAK_TARGET}")
        else:
            new_streak = 0
            reason = "missed" if not user_pick else "wrong"
            results_lines.append(f"❌ {member.display_name}: reset ({reason})")

        set_streak(member.id, new_streak)

        if new_streak >= STREAK_TARGET:
            try:
                await member.remove_roles(role)
                set_streak(member.id, 0)
                released.append(member)
            except discord.Forbidden:
                pass

    close_question()

    channel = guild.get_channel(ANNOUNCE_CHANNEL_ID) or guild.get_channel(QUESTION_CHANNEL_ID)
    if channel:
        embed = discord.Embed(
            title=title,
            description="\n".join(results_lines) or "No picks were made.",
            color=discord.Color.green(),
        )
        await channel.send(embed=embed)
        for member in released:
            await channel.send(
                f"🎉 {member.mention} hit a {STREAK_TARGET}-day streak and has been released from quarantine!"
            )


async def post_question_embed(channel, question_text, options, kickoff_utc=None):
    embed = discord.Embed(
        title="Today's Quarantine Prediction",
        description=question_text,
        color=discord.Color.red(),
    )
    footer = f"Get to {STREAK_TARGET} in a row to earn release."
    if kickoff_utc:
        kickoff = datetime.datetime.fromisoformat(kickoff_utc.replace("Z", "+00:00"))
        lock_time = kickoff + datetime.timedelta(minutes=LOCK_GRACE_MINUTES)
        footer += f" Picks lock at {lock_time.strftime('%H:%M UTC')}."
    embed.set_footer(text=footer)
    view = PickView(options)
    await channel.send(embed=embed, view=view)


_last_auto_post_date = None


# ---------- Scheduled tasks ----------
@tasks.loop(minutes=60)
async def daily_question_task():
    """Auto-post a new hard question once a day, picking a real fixture."""
    global _last_auto_post_date
    now = datetime.datetime.utcnow()
    today = now.date()

    if now.hour != DAILY_POST_HOUR_UTC or _last_auto_post_date == today:
        return

    guild = bot.get_guild(GUILD_ID)
    channel = guild.get_channel(QUESTION_CHANNEL_ID) if guild else None
    if not guild or not channel:
        return

    q = get_question()
    if q and q["active"]:
        return  # don't overwrite an unresolved question

    try:
        fixtures = await asyncio.to_thread(football_api.get_todays_fixtures)
    except Exception as e:
        print(f"Failed to fetch fixtures: {e}")
        return

    if not fixtures:
        await channel.send("No fixtures available today from the free competitions — skipping today's question.")
        _last_auto_post_date = today
        return

    import random
    match = random.choice(fixtures)
    qtype, text, options, extra = question_gen.generate_question(match)
    extra["kickoff_utc"] = match["utcDate"]
    set_question(text, options, match_id=match["id"], qtype=qtype, extra=extra, auto=True)
    await post_question_embed(channel, text, options, kickoff_utc=match["utcDate"])
    _last_auto_post_date = today


@tasks.loop(minutes=30)
async def auto_resolve_task():
    """Check if the active auto-generated question's match has finished, and grade it."""
    q = get_question()
    if not q or not q["active"] or not q["auto"] or not q["match_id"]:
        print(f"[auto_resolve] skipping: no active auto question (q={q})")
        return

    try:
        match = await asyncio.to_thread(football_api.get_match, q["match_id"])
    except Exception as e:
        print(f"[auto_resolve] Failed to fetch match {q['match_id']}: {e}")
        return

    result = football_api.extract_result(match)
    print(f"[auto_resolve] match {q['match_id']} result: {result}")
    if not result["finished"] or result["home_goals"] is None:
        print(f"[auto_resolve] not finished yet, skipping")
        return

    correct_option = question_gen.resolve_answer(
        q["qtype"], q["extra"], result["home_goals"], result["away_goals"],
        result["home_name"], result["away_name"],
    )

    guild = bot.get_guild(GUILD_ID)
    print(f"[auto_resolve] guild lookup for {GUILD_ID}: {guild}")
    if guild:
        title = f"Results: {correct_option} ({result['home_name']} {result['home_goals']}-{result['away_goals']} {result['away_name']})"
        await grade_and_announce(guild, correct_option, title)
    else:
        print(f"[auto_resolve] ERROR: could not find guild {GUILD_ID}, cached guilds: {[g.id for g in bot.guilds]}")


# ---------- Slash commands ----------
_views_registered = False


@bot.event
async def on_ready():
    global _views_registered
    init_db()
    await bot.tree.sync()

    if not _views_registered:
        q = get_question()
        if q and q["active"]:
            # Re-attach a live view to the current question so buttons on
            # the existing Discord message keep working after a restart -
            # without this, old button clicks silently fail/time out since
            # discord.py has no in-memory handler for them anymore.
            bot.add_view(PickView(q["options"]))
        _views_registered = True

    if not daily_question_task.is_running():
        daily_question_task.start()
    if not auto_resolve_task.is_running():
        auto_resolve_task.start()
    print(f"Logged in as {bot.user}")


@bot.tree.command(description="Manually post a prediction question (overrides auto-generation for today)")
@app_commands.describe(
    question="e.g. 'Arsenal vs Chelsea: BTTS?'",
    option_a="First choice, e.g. 'Yes'",
    option_b="Second choice, e.g. 'No'",
    option_c="Optional third choice, e.g. 'Draw'",
)
async def question(interaction: discord.Interaction, question: str, option_a: str, option_b: str, option_c: str = None):
    if not is_mod(interaction):
        await interaction.response.send_message("Mods only.", ephemeral=True)
        return

    options = [option_a, option_b] + ([option_c] if option_c else [])
    set_question(question, options, auto=False)

    embed = discord.Embed(
        title="Today's Quarantine Prediction",
        description=question,
        color=discord.Color.red(),
    )
    embed.set_footer(text=f"Get to {STREAK_TARGET} in a row to earn release.")
    view = PickView(options)
    await interaction.response.send_message(embed=embed, view=view)


async def resolve_option_autocomplete(interaction: discord.Interaction, current: str):
    q = get_question()
    if not q or not q["active"]:
        return []
    options = q["options"]
    return [
        app_commands.Choice(name=opt, value=opt)
        for opt in options
        if current.lower() in opt.lower()
    ][:25]


@bot.tree.command(description="Manually resolve today's question and update streaks")
@app_commands.describe(correct_option="Pick the correct answer from the list (matches the current question's buttons)")
@app_commands.autocomplete(correct_option=resolve_option_autocomplete)
async def resolve(interaction: discord.Interaction, correct_option: str):
    if not is_mod(interaction):
        await interaction.response.send_message("Mods only.", ephemeral=True)
        return

    q = get_question()
    if not q or not q["active"]:
        await interaction.response.send_message("No active question to resolve.", ephemeral=True)
        return

    if correct_option not in q["options"]:
        options_list = ", ".join(q["options"])
        await interaction.response.send_message(
            f"'{correct_option}' doesn't match any option for the current question. "
            f"Valid options are: {options_list}",
            ephemeral=True,
        )
        return

    await interaction.response.send_message("Resolving...", ephemeral=True)
    await grade_and_announce(interaction.guild, correct_option, f"Results: {correct_option}")


@bot.tree.command(description="Check your current streak, or someone else's")
async def streak(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    s = get_streak(target.id)
    await interaction.response.send_message(
        f"{target.display_name}'s streak: **{s}/{STREAK_TARGET}**"
    )


@bot.tree.command(description="Show the quarantine leaderboard")
async def leaderboard(interaction: discord.Interaction):
    guild = interaction.guild
    role = guild.get_role(QUARANTINE_ROLE_ID)
    quarantined_ids = {m.id for m in guild.members if role in m.roles}

    rows = [r for r in get_leaderboard() if r[0] in quarantined_ids]

    if not rows:
        await interaction.response.send_message("Nobody's in quarantine right now.")
        return

    lines = []
    for i, (user_id, s) in enumerate(rows, start=1):
        member = guild.get_member(user_id)
        name = member.display_name if member else f"User {user_id}"
        lines.append(f"{i}. {name} — {s}/{STREAK_TARGET}")

    embed = discord.Embed(
        title="Quarantine Streak Leaderboard",
        description="\n".join(lines),
        color=discord.Color.orange(),
    )
    await interaction.response.send_message(embed=embed)


bot.run(TOKEN)
