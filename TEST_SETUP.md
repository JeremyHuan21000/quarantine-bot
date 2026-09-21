# Test Bot Setup

This test bot runs beside the live bot on the same Oracle VM. It uses a
different Discord application, Discord server and SQLite database.

## 1. Create the test application

1. Open <https://discord.com/developers/applications>.
2. Select **New Application** and name it `Quarantine Bot Test`.
3. Open **Bot**, create the bot if prompted, and copy/reset its token.
4. On the Bot page, enable **Server Members Intent**.

Keep the token private. Put it only in the test `.env` file on the VM.

## 2. Invite it to the test server

Under **OAuth2 > URL Generator** select these scopes:

- `bot`
- `applications.commands`

Select these bot permissions:

- Manage Roles
- Send Messages
- Embed Links
- Read Message History

Open the generated URL and choose your test Discord server.

Create a quarantine role and a moderator role in the test server. In the role
list, move the test bot's role above the quarantine role so it can release
members.

## 3. Configure the test copy

Enable Discord Developer Mode and copy the test server, roles and channel IDs.
On the Oracle VM, keep the test bot in its own directory:

```bash
cd /home/ubuntu/quarantine-bot-test
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.test.example .env
nano .env
```

Fill in the test token and test-server IDs. Leave these safeguards enabled:

```env
DB_PATH=quarantine-test.db
ENABLE_AUTOMATION=false
SYNC_GUILD_COMMANDS=true
```

Run `python bot.py` once in the terminal. The test-only slash commands should
appear immediately. Stop it with `Ctrl+C` after confirming it connects.

## 4. Install the test service

```bash
sudo cp quarantine-bot-test.service.example /etc/systemd/system/quarantine-bot-test.service
sudo systemctl daemon-reload
sudo systemctl enable --now quarantine-bot-test
sudo systemctl status quarantine-bot-test
```

Follow its logs with:

```bash
journalctl -u quarantine-bot-test -f
```

This does not restart, modify or share data with the live bot service.
