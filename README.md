# HP t740 Telegram + OpenCode V2 operator

## Scope
Telegram polling gateway with private-chat Telegram user allowlist, read-only intent `/ask`, five-minute one-time `/propose` approvals, SQLite session state, RAM/disk alerts, systemd restart and OpenCode localhost Basic Auth. This is a starter deployment: it has **not** been tested against your particular OpenCode V2 build. OpenCode may execute tools beyond what the natural-language read-only instruction permits; `/ask` is NOT a hard security boundary. An approved prompt is not equivalent to per-tool approval. Use OpenCode's own deny/ask permissions and a separate low-privilege account for real enforcement. Do not expose this configuration to untrusted Telegram users.

## Preflight
1. Run `opencode --version` and `opencode serve --help` and confirm a V2 build that implements `/api/session` endpoints. V1 `/session` is incompatible.
2. Check `command -v opencode`; change systemd ExecStart if not `/home/trung/.opencode/bin/opencode`.
3. Configure Z.AI Coding Plan in the interactive `opencode` TUI via `/connect`, choose Z.AI Coding Plan and `/models`, and test one prompt. Do not put the API key in this bot's env file.
4. Create bot with @BotFather, start a private chat and obtain numeric user ID with @userinfobot. For a private chat, chat ID usually equals user ID. Never publish bot token.
5. If `/etc/sudoers.d/ai-agent` gives `trung` unrestricted NOPASSWD sudo, remove it before internet-facing bot operation, or recognize that a compromised OpenCode process has root access.

## Install (run as `trung` on Debian)
```bash
sudo apt update && sudo apt install -y python3-venv python3-pip
sudo mkdir -p /opt/telegram-opencode/app /etc/telegram-opencode
sudo cp app/bot.py /opt/telegram-opencode/app/bot.py
sudo cp requirements.txt /opt/telegram-opencode/requirements.txt
sudo chown -R trung:trung /opt/telegram-opencode
python3 -m venv /opt/telegram-opencode/.venv
/opt/telegram-opencode/.venv/bin/pip install -r /opt/telegram-opencode/requirements.txt
sudo cp secrets.env.example /etc/telegram-opencode/secrets.env
sudo nano /etc/telegram-opencode/secrets.env
sudo chown root:trung /etc/telegram-opencode/secrets.env
sudo chmod 640 /etc/telegram-opencode/secrets.env
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now opencode.service
sudo systemctl enable --now telegram-opencode.service
```

**Important:** `systemd StateDirectory=telegram-opencode` creates `/var/lib/telegram-opencode` owned by the service user. The app creates its SQLite database there. `opencode.service` reads the same env file; OpenCode uses `OPENCODE_SERVER_PASSWORD` for Basic Auth. If `opencode.service` already exists, back it up and reconcile its ExecStart instead of overwriting blindly.

## Verification
```bash
opencode --version
sudo systemctl status opencode telegram-opencode --no-pager
sudo journalctl -u telegram-opencode -n 100 --no-pager
ss -lntp | grep 4096
curl -u "opencode:$(sudo sed -n 's/^OPENCODE_SERVER_PASSWORD=//p' /etc/telegram-opencode/secrets.env)" http://127.0.0.1:4096/api/session
```

## Telegram
- `/status` CPU/RAM/disk and OpenCode reachability
- `/ask explain what is consuming RAM` sends a read-only-intent prompt
- `/propose install htop` queues a five-minute approval; tap Approve once
- `/new` resets Telegram's remembered OpenCode session

## Security and limitations
- Only one owner, one private chat, one active request at a time.
- Prompts can contain secrets; OpenCode session history and Telegram retain data. Avoid sensitive output.
- Approval is applied **before** submitting the request. It does not intercept individual shell commands, and LLM/tool execution may continue after a Telegram timeout.
- The message poller is a compatibility-oriented implementation; if your V2 build returns a different message shape, adjust `flatten_messages` and `extract_answer` after inspecting a redacted API response. It may return a response before all tools finish; production use should consume V2 events/session completion state instead.
- Alerts are basic RAM/disk thresholds and not a full monitoring suite; a host power outage requires external heartbeat monitoring.
- For production, move the bot and OpenCode to separate unprivileged users, remove unrestricted NOPASSWD sudo, use OS-enforced permission policy and per-tool approvals, rate limits, log redaction, backups and external uptime monitoring.
