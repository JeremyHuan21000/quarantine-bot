"""Manual UFC main-card challenges. Independent of daily predictions."""
import datetime
import json
import sqlite3

import discord
from discord import app_commands

UFC_EMOJI = '<:UFC:1563314986569306185>'
HOLD_EMOJI = '<:HOLD:1563314902758596688>'
OOOO_EMOJI = '<a:OOOO:1563314940918374400>'

def install(bot, db_path, question_channel_id, announce_channel_id, quarantine_role_id,
            is_mod, is_quarantined, is_blacklisted, set_streak, set_freeze):
    def connect():
        conn = sqlite3.connect(db_path, timeout=20)
        conn.row_factory = sqlite3.Row
        return conn

    def init():
        with connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS ufc_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, fights TEXT NOT NULL, lock_utc TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open', results TEXT,
                message_id INTEGER, channel_id INTEGER
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS ufc_picks (
                event_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                picks TEXT NOT NULL, submitted_utc TEXT NOT NULL,
                correct_count INTEGER, perfect INTEGER,
                PRIMARY KEY(event_id, user_id)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS ufc_stats (
                user_id INTEGER PRIMARY KEY, cards_entered INTEGER NOT NULL DEFAULT 0,
                cards_graded INTEGER NOT NULL DEFAULT 0,
                perfect_cards INTEGER NOT NULL DEFAULT 0,
                fight_picks_correct INTEGER NOT NULL DEFAULT 0,
                fight_picks_graded INTEGER NOT NULL DEFAULT 0
            )""")

    def event_by_id(event_id):
        with connect() as conn:
            row = conn.execute("SELECT * FROM ufc_events WHERE id=?", (event_id,)).fetchone()
            return dict(row) if row else None

    def open_events():
        with connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM ufc_events WHERE status='open'")]

    def unlocked(event):
        return event["status"] == "open" and datetime.datetime.now(datetime.timezone.utc) < datetime.datetime.fromisoformat(event["lock_utc"])

    def embed_for(event):
        fights = json.loads(event["fights"])
        embed = discord.Embed(title=event["name"], description=(
            UFC_EMOJI + " **UFC MAIN CARD CHALLENGE**\n"
            "Predict every fight winner. **Perfect card = instant quarantine release!**\n\n"
            + "\n\n".join(
                "**" + ("MAIN EVENT" if i == len(fights)-1 else "FIGHT " + str(i+1)) + "**\n"
                + "🔴 " + a + "  **VS**  🔵 " + b
                for i, (a,b) in enumerate(fights)
            ) + "\n\n🔒 Picks lock <t:" + str(int(datetime.datetime.fromisoformat(event["lock_utc"]).timestamp())) + ":F>"
        ), color=discord.Color.red())
        embed.set_footer(text="Event #" + str(event["id"]) + " • Picks editable until main-card kickoff")
        return embed

    class FightSelect(discord.ui.Select):
        def __init__(self, idx, pair, chosen):
            self.idx = idx
            options = [discord.SelectOption(label=name[:100], value=str(side), default=chosen == side)
                       for side, name in enumerate(pair)]
            super().__init__(placeholder="Fight " + str(idx+1) + ": choose winner", options=options,
                             min_values=1, max_values=1, row=idx)
        async def callback(self, interaction):
            self.view.choices[self.idx] = int(self.values[0])
            for option in self.options:
                option.default = option.value == self.values[0]
            await self.view.persist_selection(interaction)

    class CardView(discord.ui.View):
        def __init__(self, event, user_id, existing):
            super().__init__(timeout=900)
            self.event_id = event["id"]
            self.user_id = user_id
            self.fights = json.loads(event["fights"])
            self.choices = list(existing) if existing is not None else [None] * len(self.fights)
            for i, pair in enumerate(self.fights):
                self.add_item(FightSelect(i, pair, self.choices[i]))

        def summary(self):
            selected = sum(x is not None for x in self.choices)
            return (UFC_EMOJI + " **Your UFC fight card** — " + str(selected) + "/" + str(len(self.fights)) +
                    " selected\nChoose one winner per fight. Your complete card saves automatically. "
                    "You may edit until the first main-card fight.")

        async def persist_selection(self, interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("This is someone else's card.", ephemeral=True)
                return
            if any(choice is None for choice in self.choices):
                await interaction.response.edit_message(content=self.summary(), view=self)
                return
            event = event_by_id(self.event_id)
            if not event or not unlocked(event):
                await interaction.response.send_message("Picks are locked. No changes saved.", ephemeral=True)
                return
            # BEGIN IMMEDIATE serializes writes; recheck the clock while holding the write lock.
            with connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                event = conn.execute("SELECT * FROM ufc_events WHERE id=?", (self.event_id,)).fetchone()
                if not event or not unlocked(dict(event)):
                    await interaction.response.send_message("Picks are locked.", ephemeral=True)
                    return
                conn.execute("""INSERT INTO ufc_picks(event_id,user_id,picks,submitted_utc)
                    VALUES(?,?,?,?) ON CONFLICT(event_id,user_id)
                    DO UPDATE SET picks=excluded.picks,submitted_utc=excluded.submitted_utc""",
                    (self.event_id,self.user_id,json.dumps(self.choices),
                     datetime.datetime.now(datetime.timezone.utc).isoformat()))
            await interaction.response.edit_message(content="✅ **Card saved automatically!** You can edit it until kickoff.\n" +
                "\n".join("Fight " + str(i+1) + ": **" + self.fights[i][side] + "**"
                          for i,side in enumerate(self.choices)), view=self)

    class OpenCardView(discord.ui.View):
        def __init__(self, event_id):
            super().__init__(timeout=None)
            self.event_id = event_id
            button = discord.ui.Button(label="Make / Edit Picks", style=discord.ButtonStyle.primary,
                                       emoji=discord.PartialEmoji(name="UFC", id=1563314986569306185), custom_id="ufc:open:" + str(event_id))
            button.callback = self.open_card
            self.add_item(button)

        async def open_card(self, interaction):
            event = event_by_id(self.event_id)
            if not event or not unlocked(event):
                await interaction.response.send_message("This UFC card is locked or closed.", ephemeral=True)
                return
            with connect() as conn:
                row = conn.execute("SELECT picks FROM ufc_picks WHERE event_id=? AND user_id=?",
                                   (self.event_id,interaction.user.id)).fetchone()
            existing = json.loads(row["picks"]) if row else None
            view = CardView(event, interaction.user.id, existing)
            await interaction.response.send_message(view.summary(), view=view, ephemeral=True)

    group = app_commands.Group(name="ufc", description="Moderator-managed UFC main-card challenges")

    @group.command(name="create", description="Create and post a five-fight UFC challenge")
    @app_commands.describe(name="Event name, e.g. UFC 333",
        lock_utc="First main-card fight kickoff, UTC: YYYY-MM-DD HH:MM",
        fight1="First bout: Fighter A | Fighter B", fight2="Second bout: Fighter A | Fighter B",
        fight3="Third bout: Fighter A | Fighter B", fight4="Co-main: Fighter A | Fighter B",
        fight5="Main event: Fighter A | Fighter B")
    async def create(interaction: discord.Interaction, name: str, lock_utc: str,
                     fight1: str, fight2: str, fight3: str, fight4: str, fight5: str):
        if not is_mod(interaction):
            await interaction.response.send_message("Mods only.", ephemeral=True)
            return
        try:
            lock = datetime.datetime.strptime(lock_utc.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=datetime.timezone.utc)
            if lock <= datetime.datetime.now(datetime.timezone.utc):
                raise ValueError("kickoff is in the past")
            fights = []
            for raw in (fight1,fight2,fight3,fight4,fight5):
                pair = [part.strip() for part in raw.split("|")]
                if len(pair) != 2 or not all(pair) or pair[0].casefold() == pair[1].casefold():
                    raise ValueError("each fight must have two different fighters separated by |")
                fights.append(pair)
            if len(name) > 90 or any(len(f) > 100 for pair in fights for f in pair):
                raise ValueError("event name or fighter name too long")
        except ValueError as exc:
            await interaction.response.send_message("Invalid card: " + str(exc), ephemeral=True)
            return
        channel = interaction.guild.get_channel(question_channel_id)
        if channel is None:
            await interaction.response.send_message("Question channel not found.", ephemeral=True)
            return
        with connect() as conn:
            cur = conn.execute("INSERT INTO ufc_events(name,fights,lock_utc) VALUES(?,?,?)",
                               (name,json.dumps(fights),lock.isoformat()))
            event_id = cur.lastrowid
        event = event_by_id(event_id)
        view = OpenCardView(event_id)
        try:
            msg = await channel.send(embed=embed_for(event), view=view)
        except discord.HTTPException:
            with connect() as conn:
                conn.execute("UPDATE ufc_events SET status='cancelled' WHERE id=?", (event_id,))
            await interaction.response.send_message("Could not post UFC card.", ephemeral=True)
            return
        with connect() as conn:
            conn.execute("UPDATE ufc_events SET message_id=?,channel_id=? WHERE id=?",
                         (msg.id,channel.id,event_id))
        bot.add_view(OpenCardView(event_id))
        await interaction.response.send_message("🥊 UFC event #" + str(event_id) +
            " posted in " + channel.mention + ". Lock: <t:" + str(int(lock.timestamp())) + ":F>", ephemeral=True)

    @group.command(name="results", description="Grade a UFC card; 1=red corner, 2=blue corner")
    @app_commands.describe(event_id="UFC event number", winners="Five winners, e.g. 1,2,1,1,2")
    async def results(interaction: discord.Interaction, event_id: int, winners: str):
        if not is_mod(interaction):
            await interaction.response.send_message("Mods only.", ephemeral=True)
            return
        event = event_by_id(event_id)
        if not event or event["status"] != "open":
            await interaction.response.send_message("No open UFC event with that ID.", ephemeral=True)
            return
        if unlocked(event):
            await interaction.response.send_message("Cannot grade before main-card kickoff.", ephemeral=True)
            return
        try:
            answers = [int(x.strip()) - 1 for x in winners.split(",")]
            if len(answers) != 5 or any(x not in (0,1) for x in answers):
                raise ValueError
        except ValueError:
            await interaction.response.send_message("Enter five winners as 1 or 2, e.g. 1,2,1,1,2.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute("UPDATE ufc_events SET status='graded',results=? WHERE id=? AND status='open'",
                                   (json.dumps(answers),event_id)).rowcount
            if not changed:
                await interaction.followup.send("Already graded or cancelled.", ephemeral=True)
                return
            rows = conn.execute("SELECT user_id,picks FROM ufc_picks WHERE event_id=?", (event_id,)).fetchall()
            perfect_ids = []
            for row in rows:
                picks = json.loads(row["picks"])
                correct = sum(a==b for a,b in zip(picks,answers))
                perfect = correct == len(answers)
                conn.execute("UPDATE ufc_picks SET correct_count=?,perfect=? WHERE event_id=? AND user_id=?",
                             (correct,int(perfect),event_id,row["user_id"]))
                conn.execute("""INSERT INTO ufc_stats(user_id,cards_entered,cards_graded,perfect_cards,
                    fight_picks_correct,fight_picks_graded) VALUES(?,1,1,?,?,5)
                    ON CONFLICT(user_id) DO UPDATE SET
                    cards_graded=cards_graded+1, perfect_cards=perfect_cards+excluded.perfect_cards,
                    fight_picks_correct=fight_picks_correct+excluded.fight_picks_correct,
                    fight_picks_graded=fight_picks_graded+5""",
                    (row["user_id"],int(perfect),correct))
                if perfect:
                    perfect_ids.append(row["user_id"])
        role = interaction.guild.get_role(quarantine_role_id)
        released, blocked, winners_names = [], [], []
        for user_id in perfect_ids:
            member = interaction.guild.get_member(user_id)
            if not member:
                continue
            winners_names.append(member.mention)
            if is_quarantined(member):
                if is_blacklisted(user_id):
                    blocked.append(member.mention)
                elif role:
                    try:
                        await member.remove_roles(role, reason="Perfect UFC main card #" + str(event_id))
                        set_streak(user_id,0)
                        set_freeze(user_id,False)
                        released.append(member.mention)
                    except discord.HTTPException:
                        blocked.append(member.mention)
        channel = interaction.guild.get_channel(announce_channel_id) or interaction.guild.get_channel(question_channel_id)
        if channel:
            fights = json.loads(event["fights"])
            result_embed = discord.Embed(
                title=event["name"] + " — Official Main Card Results",
                description=UFC_EMOJI + " **MAIN CARD RESULTS**\\n" +
                    "\\n".join(
                        ("**MAIN EVENT**" if i == 4 else "**CO-MAIN EVENT**" if i == 3
                         else "**Fight " + str(i+1) + "**") +
                        "\\n🏆 **" + fights[i][side] + "** defeated " + fights[i][1-side]
                        for i, side in enumerate(answers)
                    ),
                color=discord.Color.gold() if perfect_ids else discord.Color.red(),
            )
            result_embed.add_field(name="Cards graded", value=str(len(rows)))
            result_embed.add_field(name="Perfect cards", value=str(len(perfect_ids)))
            result_embed.add_field(name="Released", value=str(len(released)))
            if perfect_ids:
                result_embed.add_field(
                    name=OOOO_EMOJI + " PERFECT CARDS",
                    value=", ".join(winners_names) if winners_names else
                        str(len(perfect_ids)) + " perfect card(s)",
                    inline=False,
                )
                result_embed.add_field(
                    name="🔓 Instant releases",
                    value=", ".join(released) if released else "No quarantined winners released",
                    inline=False,
                )
            else:
                result_embed.add_field(
                    name=HOLD_EMOJI + " NO PERFECT CARDS",
                    value="The quarantine continues.",
                    inline=False,
                )
            ranked = sorted(
                ((sum(a == b for a, b in zip(json.loads(row["picks"]), answers)),
                  row["user_id"]) for row in rows),
                key=lambda item: (-item[0], item[1]),
            )
            if ranked:
                result_embed.add_field(
                    name="Closest predictions",
                    value="\\n".join(
                        str(rank) + ". <@" + str(uid) + "> — **" + str(correct) + "/5**"
                        for rank, (correct, uid) in enumerate(ranked[:5], start=1)
                    ),
                    inline=False,
                )
            if blocked:
                result_embed.add_field(
                    name="⚠️ Not released",
                    value=", ".join(blocked), inline=False,
                )
            result_embed.set_footer(text="UFC Event #" + str(event_id) + " • Official moderator-entered results")
            await channel.send(embed=result_embed)
        await interaction.followup.send("Graded " + str(len(rows)) + " cards. Perfect: " +
                                        str(len(perfect_ids)) + ". Released: " + str(len(released)) + ".", ephemeral=True)

    @group.command(name="cancel", description="Cancel an ungraded UFC challenge without affecting stats")
    async def cancel(interaction: discord.Interaction, event_id: int):
        if not is_mod(interaction):
            await interaction.response.send_message("Mods only.", ephemeral=True)
            return
        with connect() as conn:
            changed = conn.execute("UPDATE ufc_events SET status='cancelled' WHERE id=? AND status='open'",
                                   (event_id,)).rowcount
        await interaction.response.send_message(
            ("Cancelled UFC event #" + str(event_id) + ". No stats or streaks changed.")
            if changed else "Event not found, already graded, or already cancelled.", ephemeral=True)

    @group.command(name="list", description="List active UFC challenges")
    async def list_events(interaction: discord.Interaction):
        events = open_events()
        await interaction.response.send_message(
            "\\n".join("#" + str(e["id"]) + " — " + e["name"] + " | Lock: " + e["lock_utc"]
                      for e in events) if events else "No active UFC challenges.", ephemeral=True)

    @group.command(name="stats", description="Show UFC prediction performance")
    async def stats(interaction: discord.Interaction, user: discord.Member = None):
        target = user or interaction.user
        with connect() as conn:
            row = conn.execute("SELECT * FROM ufc_stats WHERE user_id=?", (target.id,)).fetchone()
            entered = conn.execute("SELECT COUNT(*) FROM ufc_picks WHERE user_id=?", (target.id,)).fetchone()[0]
        if row:
            await interaction.response.send_message(
                "🥊 **" + target.display_name + " — UFC Stats**\n"
                "Cards entered: " + str(entered) + "\nCards graded: " + str(row["cards_graded"]) +
                "\nPerfect cards: " + str(row["perfect_cards"]) +
                "\nCorrect fight picks: " + str(row["fight_picks_correct"]) + "/" +
                str(row["fight_picks_graded"]), ephemeral=False)
        else:
            await interaction.response.send_message("🥊 " + target.display_name +
                " has entered " + str(entered) + " UFC cards (none graded yet).")

    bot.tree.add_command(group)
    return init, OpenCardView, open_events
