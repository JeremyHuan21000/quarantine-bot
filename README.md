# Quarantine Prediction Gauntlet Bot

Daily sports prediction game for a Discord #quarantine channel. Members need
a 7-day correct streak (configurable) to be auto-released.

**Fully automated by default**: each day the bot pulls a real fixture from
football-data.org's free tier and generates a genuinely hard question —
biased toward 3-way match result and extreme over/under lines rather than
coinflip markets like BTTS — then auto-resolves it once the match finishes.
Mods can still post a manual question with `/question` any day they want
to override that.

**Free-tier data limits**: football-data.org's free plan covers 12 top
competitions with delayed scores and does not include corners, cards, or
other match-stats data. Questions are therefore limited to match result,
total goals (over/under), and BTTS — all derivable from the final score.

The test instance can instead set `SPORTS_DATA_PROVIDER=thesportsdb` to try a
curated multi-sport pool containing major soccer leagues and UEFA competitions,
NHL, NBA, NFL and MLB. The live default remains `football_data` until testing
is complete. Major soccer fixtures always take priority; the other sports are
used only when no eligible soccer fixture is available that day.

## Setup

1. **Create a bot** at https://discord.com/developers/applications
   - New Application -> Bot -> Reset Token, copy it
   - Under "Privileged Gateway Intents", enable **Server Members Intent**
   - OAuth2 -> URL Generator: check `bot` and `applications.commands` scopes,
     and these bot permissions: Manage Roles, Send Messages, Embed Links,
     Read Message History. Use the generated URL to invite it to your server.
   - **Important**: the bot's role must sit *above* the quarantine role in
     Server Settings -> Roles, or it won't be able to remove it.

2. **Get IDs** (enable Developer Mode in Discord: Settings -> Advanced):
   - Right-click your server -> Copy Server ID -> `GUILD_ID`
   - Right-click the quarantine role in Server Settings -> Roles -> Copy ID -> `QUARANTINE_ROLE_ID`
   - Right-click each mod role -> Copy ID -> combine them comma-separated into `MOD_ROLE_IDS` (e.g. `MOD_ROLE_IDS=123456,789012`) if your server has more than one mod role
   - Right-click the channel where release announcements should post -> Copy ID -> `ANNOUNCE_CHANNEL_ID`

3. **Get a free football-data.org key**: register at
   football-data.org/client/register (no card needed) and copy the API
   token into `FOOTBALL_DATA_API_KEY`.

4. **Configure**: copy `.env.example` to `.env` and fill in the values,
   including `QUESTION_CHANNEL_ID` (where the daily question gets posted)
   and `DAILY_POST_HOUR_UTC` (what hour, in UTC, the bot checks fixtures
   and posts — pick a time when most of that day's matches are already
   scheduled and known).

5. **Install & run locally**:
   ```
   pip install -r requirements.txt
   python bot.py
   ```

## Deploying for free (Oracle Cloud Free Tier)

Oracle Cloud's "Always Free" tier includes a small ARM VM that stays free
indefinitely (no trial clock, no credit card charge). It's more setup than
a PaaS like Railway, but nothing runs out.

1. **Sign up** at oracle.com/cloud/free and create an account (a card is
   required for identity verification but the Always Free resources are
   never billed).
2. **Create a VM instance**: Compute -> Instances -> Create Instance.
   - Choose an "Always Free eligible" shape (an Ampere A1 or VM.Standard.E2.1.Micro shape)
   - Image: Ubuntu (latest LTS)
   - Add your SSH key (or let Oracle generate one for you to download)
3. **Open a port isn't needed** — this bot only makes outbound connections
   to Discord, so no inbound firewall rules are required.
4. **SSH into the VM**:
   ```
   ssh -i /path/to/key ubuntu@<your-vm-public-ip>
   ```
5. **Install Python and clone/copy the bot**:
   ```
   sudo apt update && sudo apt install -y python3-pip python3-venv git
   git clone <your-repo-url> quarantine-bot
   cd quarantine-bot
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env
   nano .env   # fill in your real values
   ```
6. **Run it persistently** with a systemd service so it survives reboots
   and SSH disconnects. Create `/etc/systemd/system/quarantine-bot.service`:
   ```
   [Unit]
   Description=Quarantine Prediction Bot
   After=network.target

   [Service]
   Type=simple
   User=ubuntu
   WorkingDirectory=/home/ubuntu/quarantine-bot
   ExecStart=/home/ubuntu/quarantine-bot/venv/bin/python bot.py
   Restart=always
   RestartSec=5

   [Install]
   WantedBy=multi-user.target
   ```
   Then:
   ```
   sudo systemctl daemon-reload
   sudo systemctl enable quarantine-bot
   sudo systemctl start quarantine-bot
   sudo systemctl status quarantine-bot   # check it's running
   ```
7. **Updating later**: `git pull`, `pip install -r requirements.txt` if
   deps changed, then `sudo systemctl restart quarantine-bot`.

`quarantine.db` (SQLite) lives directly on the VM's disk, so streaks
persist across restarts and reboots with no extra setup — no volumes or
external database needed.

## Commands (all slash commands)

- `/question <question> <option_a> <option_b> <lock_minutes> [option_c]` — **mods only**.
  Manually posts a question, overriding auto-generation for that day.
  The deadline is calculated from `lock_minutes` and displayed in UTC.
- `/lock` — **mods only**. Immediately closes submissions without resolving.
- `/cancel [reason]` — **mods only**. Voids the active prediction without
  changing streaks or statistics.
- `/resolve <correct_option>` — **mods only**. Manually grade the current
  question (needed if you used `/question`, or if you want to override
  an auto-generated one). Type the answer exactly as it appears as a
  button label. Updates every quarantined member's streak, auto-releases
  anyone who hits the target, and posts an announcement.
- `/streak [@user]` — anyone can check their own or someone else's streak.
- `/stats [@user]` — shows regular-pick accuracy, current and best streaks,
  and exact-score performance.
- `/leaderboard` — shows all currently quarantined members ranked by streak.
- `/scorequestion <home_team> <away_team> <lock_minutes> [score_unit]` — **mods only**.
  Posts a private full-time exact-score challenge. Users submit both scores
  through a modal; an exact hit earns instant release. Choose goals for soccer
  or hockey, points for football or basketball, and runs for baseball. This
  challenge runs independently and does not replace the daily streak question.
- `/scoreresolve <home_score> <away_score>` — **mods only**. Resolves the
  active exact-score challenge using the final full-time score.
- `/blacklist add <@user>` — **mods only**. Prevents the bot from automatically
  removing that user's quarantine role. Their picks and streak are still tracked.
- `/blacklist remove <@user>` — **mods only**. Makes a blacklisted user eligible
  for automatic release again.
- `/blacklist list` — **mods only**. Shows the current release blacklist.

## How a day works (automated)

1. Once an hour, the bot checks whether it's `DAILY_POST_HOUR_UTC` and
   whether a question has already been posted today. If not, it pulls
   today's fixtures from football-data.org, picks one at random, and
   generates a question — weighted toward 3-way match result and extreme
   over/under lines (0.5, 1.5, 4.5, 5.5) rather than near-coinflip markets,
   so it's a genuinely hard pick, not a layup.
2. Quarantined members click a button to lock in their pick (one pick per
   person — clicking again overwrites the previous pick until resolved).
3. Every 30 minutes, the bot checks whether that match has finished. Once
   it has, it grades every pick automatically from the final score,
   updates streaks, and posts the results.
4. Anyone reaching the streak target (default 7) has the quarantine role
   removed automatically and gets a public shoutout, unless a moderator has
   placed them on the release blacklist.

If an automatically tracked fixture is cancelled or postponed, the round is
voided without changing streaks or statistics.

Mods can skip all of this on a given day with `/question` + `/resolve`.

## Notes / things you may want to tweak

- Only one question can be active at a time — a new `/question` (manual)
  overwrites the current one and clears picks; the auto-post won't
  overwrite an unresolved question, it'll just wait.
- One separate correct-score challenge may run alongside the daily question.
  Posting another `/scorequestion` replaces only the previous score challenge
  and never clears or replaces the daily prediction or its picks.
- `STREAK_TARGET` in `.env` controls the streak needed to be released.
- `DB_PATH` selects the SQLite database. Use `quarantine-test.db` for a test
  instance so it cannot touch live streaks or the live blacklist.
- `SPORTS_DATA_PROVIDER` selects `football_data` (the live default) or
  `thesportsdb` (the multi-sport test provider). `SPORTSDB_LEAGUE_IDS` can
  override the curated TheSportsDB league IDs without a code change.
- The release blacklist is stored in `quarantine.db`, so it survives bot and
  server restarts. Removing someone from it does not immediately release them;
  they become eligible the next time a question is resolved while their streak
  is at or above the target.
- Question difficulty is weighted in `question_gen.py`
  (`WEIGHTED_TYPES` and `OVER_UNDER_LINES`) — adjust the weights or the
  over/under lines there if picks feel too easy or too hard.
- football-data.org's free tier only covers 12 competitions (the big
  European leagues, World Cup, Euros, Champions League, etc. — see
  `FREE_COMPETITIONS` in `football_api.py`). If none of those play on a
  given day, the bot skips posting rather than guessing.
- Corners, cards, and other match-stats markets need a paid add-on on
  football-data.org (or a different provider like API-Football) — not
  wired up here, but `question_gen.py` is where you'd add new question
  types if you upgrade later.

## Running a separate test bot

Use a second Discord application and a separate checkout or directory. Give
the test copy its own `.env` values, especially a test token, test server IDs,
and `DB_PATH=quarantine-test.db`. It can run alongside the live service on the
same Oracle VM.
