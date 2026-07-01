# MideaHavara

Availability tracker for the Midea/Comfee PortaSplit 12.000 BTU, adapted for
Graz, Austria and local Raspberry Pi operation.

The tracker checks configured shop/product pages, filters for the right product
and max price, avoids repeated alerts via `state.json`, and sends Telegram
notifications when a new buyable offer appears.

## Current Setup

- Location: Graz, `8010`, 25 km radius.
- Product: Midea PortaSplit 12.000 BTU, EAN `4048164116478`.
- Price limit: `1000.0` EUR.
- Enabled sources: Geizhals AT, MediaMarkt AT, OBI AT, BAUHAUS AT.
- Disabled sources: Idealo, Saturn, Hornbach, Amazon, Greensun.
- GitHub Actions: removed.

The main settings are in `config.yaml`. Store IDs for Graz-area MediaMarkt
stores are in `stores.yaml`.

## Local Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install pytest
python -m playwright install chromium
python -m pytest -q
python -m tracker.run --dry-run -v
```

On Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install pytest
python -m playwright install chromium
python -m pytest -q
python -m tracker.run --dry-run -v
```

To test Telegram locally, set the environment variables and run:

```bash
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
python -m tracker.notify --test
```

## Raspberry Pi Setup

Suggested install location on the Pi:

```bash
sudo mkdir -p /opt/mideahavara
sudo chown -R joe:joe /opt/mideahavara
git clone <your-repo-or-copy> /opt/mideahavara
cd /opt/mideahavara
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

If Playwright's bundled Chromium is not available on the Pi, install system
Chromium and set `PLAYWRIGHT_CHROMIUM_EXECUTABLE` in `.env`:

```bash
sudo apt update
sudo apt install -y chromium
cp .env.example .env
nano .env
```

Example `.env`:

```bash
TELEGRAM_BOT_TOKEN=123456:real-token
TELEGRAM_CHAT_ID=123456789
PLAYWRIGHT_CHROMIUM_EXECUTABLE=/usr/bin/chromium
```

Install the local timer:

```bash
sudo cp systemd/mideahavara.service /etc/systemd/system/
sudo cp systemd/mideahavara.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mideahavara.timer
```

Useful commands:

```bash
systemctl list-timers mideahavara.timer
sudo systemctl start mideahavara.service
journalctl -u mideahavara.service -n 100 --no-pager
```

## Notes

- `state.json` stores already-alerted offers and daily heartbeat markers.
- Run `python -m tracker.run --dry-run -v` before enabling the timer.
- Some shops may block automated checks. The tracker falls back to Playwright
  where possible, but each source is best effort.
