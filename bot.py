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
ENABLE_AUTOMATION = os.getenv("ENABLE_AUTOMATION", "true").lower() == "true"
SYNC_GUILD_COMMANDS = os.getenv("SYNC_GUILD_COMMANDS", "false").lower() == "true"

DB_PATH = os.getenv("DB_PATH", "quarantine.db")

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
    c.execute("""
        CREATE TABLE IF NOT EXISTS release_blacklist (
            user_id INTEGER PRIMARY KEY
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_stats (
            user_id INTEGER PRIMARY KEY,
            total_picks INTEGER DEFAULT 0,
            correct_picks INTEGER DEFAULT 0,
            best_streak INTEGER DEFAULT 0,
            exact_score_attempts INTEGER DEFAULT 0,
            exact_score_hits INTEGER DEFAULT 0
        )
    """)
    c.execute("""
        INSERT OR IGNORE INTO user_stats (user_id, best_streak)
        SELECT user_id, streak FROM streaks
    """)
    c.execute("""
        UPDATE user_stats
        SET best_streak = MAX(
            best_streak,
            COALESCE((SELECT streak FROM streaks WHERE streaks.user_id = user_stats.user_id), 0)
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


def update_question_extra(extra: dict):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE current_question SET extra = ? WHERE id = 1",
        (json.dumps(extra),),
    )
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


def is_blacklisted(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM release_blacklist WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row is not None


def add_to_blacklist(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO release_blacklist (user_id) VALUES (?)",
        (user_id,),
    )
    conn.commit()
    conn.close()


def remove_from_blacklist(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM release_blacklist WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def get_blacklist():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id FROM release_blacklist ORDER BY user_id")
    rows = c.fetchall()
    conn.close()
    return [row[0] for row in rows]


def update_stats(
    user_id: int,
    *,
    picked: bool = False,
    correct: bool = False,
    streak: int = 0,
    exact_attempt: bool = False,
    exact_hit: bool = False,
):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO user_stats (user_id) VALUES (?)",
        (user_id,),
    )
    c.execute(
        """
        UPDATE user_stats
        SET total_picks = total_picks + ?,
            correct_picks = correct_picks + ?,
            best_streak = MAX(best_streak, ?),
            exact_score_attempts = exact_score_attempts + ?,
            exact_score_hits = exact_score_hits + ?
        WHERE user_id = ?
        """,
        (
            int(picked),
            int(correct),
            streak,
            int(exact_attempt),
            int(exact_hit),
            user_id,
        ),
    )
    conn.commit()
    conn.close()


def get_stats(user_id: int) -> dict:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        SELECT total_picks, correct_picks, best_streak,
               exact_score_attempts, exact_score_hits
        FROM user_stats WHERE user_id = ?
        """,
        (user_id,),
    )
    row = c.fetchone()
    conn.close()
    if not row:
        return {
            "total_picks": 0,
            "correct_picks": 0,
            "best_streak": 0,
            "exact_score_attempts": 0,
            "exact_score_hits": 0,
        }
    return dict(
        zip(
            (
                "total_picks",
                "correct_picks",
                "best_streak",
                "exact_score_attempts",
                "exact_score_hits",
            ),
            row,
        )
    )


def is_mod(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    if MOD_ROLE_IDS:
        return any(r.id in MOD_ROLE_IDS for r in interaction.user.roles)
    return False


def is_quarantined(member: discord.Member) -> bool:
    return any(r.id == QUARANTINE_ROLE_ID for r in member.roles)


def parse_utc(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def question_lock_time(q: dict):
    lock_utc = q["extra"].get("lock_utc")
    if lock_utc:
        return parse_utc(lock_utc)
    kickoff_utc = q["extra"].get("kickoff_utc")
    if kickoff_utc:
        return parse_utc(kickoff_utc) + datetime.timedelta(minutes=LOCK_GRACE_MINUTES)
    return None


def question_is_locked(q: dict) -> bool:
    if q["extra"].get("locked"):
        return True
    lock_time = question_lock_time(q)
    return bool(lock_time and datetime.datetime.now(datetime.timezone.utc) >= lock_time)


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
            self.add_item(PickButton(opt, style, i))


LOCK_GRACE_MINUTES = 5  # allow a short grace window after kickoff before locking


class PickButton(discord.ui.Button):
    def __init__(self, label: str, style: discord.ButtonStyle, index: int):
        # Discord requires every component on a message to have a unique ID.
        # Use the option position rather than its label so repeated labels do
        # not make the entire question fail to post.
        super().__init__(label=label, style=style, custom_id=f"pick_{index}")

    async def callback(self, interaction: discord.Interaction):
        member = interaction.user
        playing_for_release = is_quarantined(member)

        q = get_question()
        if not q or not q["active"]:
            await interaction.response.send_message(
                "No active question right now.", ephemeral=True
            )
            return

        if q["qtype"] == "exact_score":
            await interaction.response.send_message(
                "This is an exact-score challenge. Use its Submit score button.",
                ephemeral=True,
            )
            return

        if question_is_locked(q):
            existing_pick = get_user_pick(member.id)
            if existing_pick:
                msg = f"Picks are locked. Your locked-in pick: **{existing_pick}**"
            else:
                msg = "Picks are locked. You didn't submit a pick."
            await interaction.response.send_message(msg, ephemeral=True)
            return

        record_pick(member.id, self.label)
        if playing_for_release:
            msg = f"Locked in: **{self.label}**. Good luck."
        else:
            msg = f"Locked in: **{self.label}**. (Just for fun - you're not in quarantine, so this doesn't count toward a streak.)"
        await interaction.response.send_message(msg, ephemeral=True)


class ExactScoreModal(discord.ui.Modal):
    def __init__(self, home_team: str, away_team: str):
        super().__init__(title="Full-time score prediction")
        home_suffix = " goals (home)"
        away_suffix = " goals (away)"
        self.home_goals = discord.ui.TextInput(
            label=f"{home_team[:45 - len(home_suffix)]}{home_suffix}",
            placeholder="0",
            min_length=1,
            max_length=2,
        )
        self.away_goals = discord.ui.TextInput(
            label=f"{away_team[:45 - len(away_suffix)]}{away_suffix}",
            placeholder="0",
            min_length=1,
            max_length=2,
        )
        self.add_item(self.home_goals)
        self.add_item(self.away_goals)

    async def on_submit(self, interaction: discord.Interaction):
        q = get_question()
        if not q or not q["active"] or q["qtype"] != "exact_score":
            await interaction.response.send_message(
                "There is no active exact-score challenge.", ephemeral=True
            )
            return
        if question_is_locked(q):
            await interaction.response.send_message(
                "Score predictions are locked.", ephemeral=True
            )
            return

        try:
            home = int(self.home_goals.value.strip())
            away = int(self.away_goals.value.strip())
        except ValueError:
            await interaction.response.send_message(
                "Enter whole numbers only, such as 2 and 1.", ephemeral=True
            )
            return

        if not (0 <= home <= 99 and 0 <= away <= 99):
            await interaction.response.send_message(
                "Goals must be whole numbers from 0 to 99.", ephemeral=True
            )
            return

        prediction = f"{home}-{away}"
        record_pick(interaction.user.id, prediction)
        await interaction.response.send_message(
            f"Locked in privately: **{q['extra']['home_team']} {prediction} "
            f"{q['extra']['away_team']}**. You may resubmit before the deadline.",
            ephemeral=True,
        )


class ExactScoreView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Submit score",
        style=discord.ButtonStyle.primary,
        custom_id="exact_score_submit",
    )
    async def submit_score(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        q = get_question()
        if not q or not q["active"] or q["qtype"] != "exact_score":
            await interaction.response.send_message(
                "There is no active exact-score challenge.", ephemeral=True
            )
            return
        if question_is_locked(q):
            existing = get_user_pick(interaction.user.id)
            suffix = f" Your prediction: **{existing}**." if existing else ""
            await interaction.response.send_message(
                f"Score predictions are locked.{suffix}", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            ExactScoreModal(q["extra"]["home_team"], q["extra"]["away_team"])
        )


async def grade_and_announce(guild: discord.Guild, correct_option: str, title: str):
    """Shared grading logic used by both manual /resolve and the auto-resolve loop."""
    role = guild.get_role(QUARANTINE_ROLE_ID)
    quarantined_ids = {m.id for m in guild.members if role in m.roles}
    picks = dict(get_all_picks())

    # Everyone who currently has a streak or made a pick this round gets
    # graded - not just quarantined members - so non-quarantined players
    # can build their own streak for fun. Only quarantined members are
    # ever checked for release below.
    participant_ids = set(picks.keys()) | {
        uid for uid in quarantined_ids
    }

    released = []
    results_lines = []

    for user_id in participant_ids:
        member = guild.get_member(user_id)
        if member is None:
            continue  # left the server, or otherwise unresolvable

        user_pick = picks.get(user_id)
        current = get_streak(user_id)
        is_quarantined_now = user_id in quarantined_ids

        # Only worth showing if they actually did something this round
        # (picked) or had a real streak on the line that's now being reset -
        # a permanently-inactive quarantined member sitting at 0 shouldn't
        # clutter the results every single day.
        show_line = user_pick is not None or (is_quarantined_now and current > 0)

        was_correct = bool(
            user_pick
            and user_pick.strip().lower() == correct_option.strip().lower()
        )
        if was_correct:
            new_streak = current + 1
            if show_line:
                results_lines.append(f"✅ {member.display_name}: {new_streak}/{STREAK_TARGET}")
        else:
            new_streak = 0
            if show_line:
                reason = "missed" if not user_pick else "wrong"
                results_lines.append(f"❌ {member.display_name}: reset ({reason})")

        set_streak(user_id, new_streak)
        update_stats(
            user_id,
            picked=user_pick is not None,
            correct=was_correct,
            streak=new_streak,
        )

        if (
            is_quarantined_now
            and new_streak >= STREAK_TARGET
            and not is_blacklisted(user_id)
        ):
            try:
                await member.remove_roles(role)
                set_streak(user_id, 0)
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
    q = get_question()
    lock_time = question_lock_time(q) if q else None
    if lock_time:
        footer += f" Picks lock at {lock_time.strftime('%Y-%m-%d %H:%M UTC')}."
    embed.set_footer(text=footer)
    view = PickView(options)
    await channel.send(embed=embed, view=view)


async def void_question(guild: discord.Guild, reason: str):
    close_question()
    clear_picks()
    channel = guild.get_channel(ANNOUNCE_CHANNEL_ID) or guild.get_channel(
        QUESTION_CHANNEL_ID
    )
    if channel:
        await channel.send(
            f"↩️ The current prediction has been voided ({reason}). "
            "No streaks or statistics were changed."
        )


async def resolve_exact_score(
    guild: discord.Guild, home_goals: int, away_goals: int
):
    q = get_question()
    correct_score = f"{home_goals}-{away_goals}"
    picks = dict(get_all_picks())
    role = guild.get_role(QUARANTINE_ROLE_ID)
    released = []
    blocked = []
    winners = []

    for user_id, prediction in picks.items():
        member = guild.get_member(user_id)
        if member is None:
            continue
        hit = prediction == correct_score
        update_stats(
            user_id,
            exact_attempt=True,
            exact_hit=hit,
            streak=get_streak(user_id),
        )
        if not hit:
            continue
        winners.append(member)
        if is_quarantined(member):
            if is_blacklisted(user_id):
                blocked.append(member)
            else:
                try:
                    await member.remove_roles(role)
                    set_streak(user_id, 0)
                    released.append(member)
                except discord.Forbidden:
                    pass

    close_question()
    channel = guild.get_channel(ANNOUNCE_CHANNEL_ID) or guild.get_channel(
        QUESTION_CHANNEL_ID
    )
    if channel:
        home = q["extra"]["home_team"]
        away = q["extra"]["away_team"]
        if winners:
            winner_text = ", ".join(m.mention for m in winners)
            description = f"Exact-score winners: {winner_text}"
        else:
            description = "Nobody predicted the exact score."
        embed = discord.Embed(
            title=f"Full-time result: {home} {correct_score} {away}",
            description=description,
            color=discord.Color.gold(),
        )
        await channel.send(embed=embed)
        for member in released:
            await channel.send(
                f"🎯 {member.mention} predicted the exact full-time score and has been "
                "released from quarantine!"
            )
        for member in blocked:
            await channel.send(
                f"🔒 {member.mention} predicted the exact score, but is not eligible "
                "for automatic release."
            )


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
    if result["voided"]:
        guild = bot.get_guild(GUILD_ID)
        if guild:
            await void_question(guild, result["status"].lower())
        return
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
    if SYNC_GUILD_COMMANDS and GUILD_ID:
        guild_object = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild_object)
        await bot.tree.sync(guild=guild_object)
    else:
        await bot.tree.sync()

    if not _views_registered:
        q = get_question()
        if q and q["active"]:
            # Re-attach a live view to the current question so buttons on
            # the existing Discord message keep working after a restart -
            # without this, old button clicks silently fail/time out since
            # discord.py has no in-memory handler for them anymore.
            if q["qtype"] == "exact_score":
                bot.add_view(ExactScoreView())
            else:
                bot.add_view(PickView(q["options"]))
        _views_registered = True

    if ENABLE_AUTOMATION:
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
    lock_minutes="Minutes from now until submissions close",
    option_c="Optional third choice, e.g. 'Draw'",
)
async def question(
    interaction: discord.Interaction,
    question: str,
    option_a: str,
    option_b: str,
    lock_minutes: app_commands.Range[int, 1, 10080],
    option_c: str = None,
):
    if not is_mod(interaction):
        await interaction.response.send_message("Mods only.", ephemeral=True)
        return

    options = [option_a.strip(), option_b.strip()] + (
        [option_c.strip()] if option_c else []
    )
    if len({option.casefold() for option in options}) != len(options):
        await interaction.response.send_message(
            "Each answer option must be different.", ephemeral=True
        )
        return
    lock_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        minutes=lock_minutes
    )
    extra = {"lock_utc": lock_time.isoformat()}
    set_question(question, options, qtype="manual", extra=extra, auto=False)

    embed = discord.Embed(
        title="Today's Quarantine Prediction",
        description=question,
        color=discord.Color.red(),
    )
    embed.set_footer(
        text=f"Get to {STREAK_TARGET} in a row to earn release. "
        f"Picks lock at {lock_time.strftime('%Y-%m-%d %H:%M UTC')}."
    )
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

    if q["qtype"] == "exact_score":
        await interaction.response.send_message(
            "Use /scoreresolve for an exact-score challenge.", ephemeral=True
        )
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


@bot.tree.command(description="Immediately close submissions for the active prediction")
async def lock(interaction: discord.Interaction):
    if not is_mod(interaction):
        await interaction.response.send_message("Mods only.", ephemeral=True)
        return
    q = get_question()
    if not q or not q["active"]:
        await interaction.response.send_message(
            "No active question to lock.", ephemeral=True
        )
        return
    if question_is_locked(q):
        await interaction.response.send_message(
            "The active question is already locked.", ephemeral=True
        )
        return
    q["extra"]["locked"] = True
    update_question_extra(q["extra"])
    await interaction.response.send_message("🔒 Submissions are now locked.")


@bot.tree.command(description="Void the active prediction without changing streaks")
@app_commands.describe(reason="Why the prediction is being cancelled")
async def cancel(interaction: discord.Interaction, reason: str = "cancelled by a moderator"):
    if not is_mod(interaction):
        await interaction.response.send_message("Mods only.", ephemeral=True)
        return
    q = get_question()
    if not q or not q["active"]:
        await interaction.response.send_message(
            "No active question to cancel.", ephemeral=True
        )
        return
    await interaction.response.send_message("Voiding prediction...", ephemeral=True)
    await void_question(interaction.guild, reason)


@bot.tree.command(description="Post a private full-time exact-score challenge")
@app_commands.describe(
    home_team="Home team name",
    away_team="Away team name",
    lock_minutes="Minutes from now until score submissions close",
)
async def scorequestion(
    interaction: discord.Interaction,
    home_team: str,
    away_team: str,
    lock_minutes: app_commands.Range[int, 1, 10080],
):
    if not is_mod(interaction):
        await interaction.response.send_message("Mods only.", ephemeral=True)
        return

    lock_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        minutes=lock_minutes
    )
    text = f"{home_team} vs {away_team} — predict the exact full-time score."
    extra = {
        "home_team": home_team,
        "away_team": away_team,
        "lock_utc": lock_time.isoformat(),
    }
    set_question(text, [], qtype="exact_score", extra=extra, auto=False)
    embed = discord.Embed(
        title="Instant Release: Exact Score",
        description=text,
        color=discord.Color.gold(),
    )
    embed.set_footer(
        text=f"An exact FT score earns instant release. "
        f"Entries lock at {lock_time.strftime('%Y-%m-%d %H:%M UTC')}."
    )
    await interaction.response.send_message(embed=embed, view=ExactScoreView())


@bot.tree.command(description="Resolve the active full-time exact-score challenge")
@app_commands.describe(
    home_goals="Home team's full-time goals",
    away_goals="Away team's full-time goals",
)
async def scoreresolve(
    interaction: discord.Interaction,
    home_goals: app_commands.Range[int, 0, 99],
    away_goals: app_commands.Range[int, 0, 99],
):
    if not is_mod(interaction):
        await interaction.response.send_message("Mods only.", ephemeral=True)
        return
    q = get_question()
    if not q or not q["active"] or q["qtype"] != "exact_score":
        await interaction.response.send_message(
            "There is no active exact-score challenge.", ephemeral=True
        )
        return
    await interaction.response.send_message("Resolving exact score...", ephemeral=True)
    await resolve_exact_score(interaction.guild, home_goals, away_goals)


@bot.tree.command(description="Check your current streak, or someone else's")
async def streak(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    s = get_streak(target.id)
    await interaction.response.send_message(
        f"{target.display_name}'s streak: **{s}/{STREAK_TARGET}**"
    )


@bot.tree.command(description="Show prediction performance for yourself or another user")
async def stats(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    data = get_stats(target.id)
    total = data["total_picks"]
    accuracy = (data["correct_picks"] / total * 100) if total else 0
    embed = discord.Embed(
        title=f"Prediction Stats — {target.display_name}",
        color=discord.Color.blue(),
    )
    embed.add_field(name="Current streak", value=str(get_streak(target.id)))
    embed.add_field(name="Best streak", value=str(data["best_streak"]))
    embed.add_field(name="Regular picks", value=str(total))
    embed.add_field(name="Correct", value=str(data["correct_picks"]))
    embed.add_field(name="Accuracy", value=f"{accuracy:.1f}%")
    embed.add_field(
        name="Exact scores",
        value=f"{data['exact_score_hits']}/{data['exact_score_attempts']}",
    )
    await interaction.response.send_message(embed=embed)


blacklist_group = app_commands.Group(
    name="blacklist",
    description="Manage users who cannot earn automatic quarantine release",
)


@blacklist_group.command(name="add", description="Block a user from automatic quarantine release")
@app_commands.describe(user="User to block from automatic release")
async def blacklist_add(interaction: discord.Interaction, user: discord.Member):
    if not is_mod(interaction):
        await interaction.response.send_message("Mods only.", ephemeral=True)
        return

    if is_blacklisted(user.id):
        await interaction.response.send_message(
            f"{user.mention} is already on the release blacklist.",
            ephemeral=True,
        )
        return

    add_to_blacklist(user.id)
    await interaction.response.send_message(
        f"Added {user.mention} to the release blacklist. Their streak will still be tracked, "
        "but the bot will not remove their quarantine role.",
        ephemeral=True,
    )


@blacklist_group.command(name="remove", description="Allow a user to earn automatic quarantine release again")
@app_commands.describe(user="User to remove from the release blacklist")
async def blacklist_remove(interaction: discord.Interaction, user: discord.Member):
    if not is_mod(interaction):
        await interaction.response.send_message("Mods only.", ephemeral=True)
        return

    if not is_blacklisted(user.id):
        await interaction.response.send_message(
            f"{user.mention} is not on the release blacklist.",
            ephemeral=True,
        )
        return

    remove_from_blacklist(user.id)
    await interaction.response.send_message(
        f"Removed {user.mention} from the release blacklist. They can now earn automatic release.",
        ephemeral=True,
    )


@blacklist_group.command(name="list", description="Show everyone blocked from automatic quarantine release")
async def blacklist_list(interaction: discord.Interaction):
    if not is_mod(interaction):
        await interaction.response.send_message("Mods only.", ephemeral=True)
        return

    user_ids = get_blacklist()
    if not user_ids:
        await interaction.response.send_message(
            "The release blacklist is empty.",
            ephemeral=True,
        )
        return

    lines = []
    for user_id in user_ids:
        member = interaction.guild.get_member(user_id)
        lines.append(member.mention if member else f"User ID `{user_id}` (not in server)")

    await interaction.response.send_message(
        "**Release blacklist**\n" + "\n".join(lines),
        ephemeral=True,
    )


bot.tree.add_command(blacklist_group)


@bot.tree.command(description="Show the quarantine leaderboard")
async def leaderboard(interaction: discord.Interaction):
    guild = interaction.guild
    role = guild.get_role(QUARANTINE_ROLE_ID)
    quarantined_ids = {m.id for m in guild.members if role in m.roles}

    rows = [r for r in get_leaderboard() if r[1] > 0 or r[0] in quarantined_ids]

    if not rows:
        await interaction.response.send_message("Nobody's picked yet.")
        return

    lines = []
    for i, (user_id, s) in enumerate(rows, start=1):
        member = guild.get_member(user_id)
        name = member.display_name if member else f"User {user_id}"
        tag = "" if user_id in quarantined_ids else " (not quarantined)"
        lines.append(f"{i}. {name}{tag} — {s}/{STREAK_TARGET}")

    embed = discord.Embed(
        title="Streak Leaderboard",
        description="\n".join(lines),
        color=discord.Color.orange(),
    )
    await interaction.response.send_message(embed=embed)


bot.run(TOKEN)
