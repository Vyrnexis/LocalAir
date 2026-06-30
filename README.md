# LocalAIR

LocalAIR is a local climate monitor and IR controller for a Tuya
temperature/humidity IR device. It reads temperature (DP 1, divided by 10) and
humidity (DP 2) over the LAN using TinyTuya, transmits learned AC IR commands,
and can observe matching IR commands from Sensibo or the physical remote.

Python is used because GTK4 and TinyTuya both have mature Python support on
Linux. The project now includes both:

- a GTK4 desktop test/control app
- a Docker-friendly web app for always-on Unraid use

The web app stores graph history in SQLite and keeps raw readings for 31 days by
default.

## Requirements

- Linux with GTK 4 and PyGObject
- Python 3.11 or newer
- TinyTuya
- FastAPI/Uvicorn for the web UI

On Debian/Ubuntu:

```sh
sudo apt install python3-gi gir1.2-gtk-4.0
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
```

On Arch Linux:

```sh
sudo pacman -S gtk4 python-gobject
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
```

## Configure and run

You can enter device settings in the GTK application's Settings dialog or the
web app's Settings page. By default, LocalAIR stores config at:

```text
~/.config/localair/config.toml
```

Alternatively, copy the example and edit it:

```sh
mkdir -p ~/.config/localair
cp config.example.toml ~/.config/localair/config.toml
chmod 600 ~/.config/localair/config.toml
python3 -m src.main
```

The local key is never logged or printed by LocalAir. The configuration file is
created with user-only permissions where supported.

### Run the web app locally

```sh
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m src.web_main
```

Open:

```text
http://localhost:8787
```

The web server reads `[server]` from `config.toml`. You can override the port:

```sh
LOCALAIR_PORT=8790 .venv/bin/python -m src.web_main
```

### Run with Docker / Unraid

The container is designed to use one mounted data directory:

```text
/data/config.toml
/data/localair.db
/data/unknown-ir.log
```

Recommended Unraid appdata path:

```text
/mnt/user/appdata/localair
```

Create the config:

```sh
mkdir -p /mnt/user/appdata/localair
cp config.example.toml /mnt/user/appdata/localair/config.toml
chmod 600 /mnt/user/appdata/localair/config.toml
```

Edit `/mnt/user/appdata/localair/config.toml` and set the real local key.

Run with Compose:

```sh
docker compose up -d --build
```

The included `docker-compose.yml` uses `network_mode: host` because Tuya LAN
devices are simplest and most reliable from host networking. With host
networking, Docker port mappings are ignored, so configure the app port with:

```yaml
environment:
  LOCALAIR_PORT: 8787
```

Then open:

```text
http://YOUR_UNRAID_IP:8787
```

### Easy Unraid install plan

For a proper Unraid Community Apps-style install, LocalAIR needs a published
Docker image. This repo includes:

```text
Dockerfile
unraid/localair.xml
unraid/localair-local.xml
unraid/localair.svg
unraid/README.md
```

The public Unraid template points at this Docker image:

```text
vyrnexis/localair:latest
```

Publish it to Docker Hub with:

```sh
docker login
docker build -t vyrnexis/localair:latest .
docker push vyrnexis/localair:latest
```

After the image is published, Unraid users can install using the XML template.
The important template settings are:

```text
Network: host
WebUI: http://[IP]:[PORT:8787]/
Appdata: /mnt/user/appdata/localair -> /data
LOCALAIR_PORT: 8787
```

See `unraid/README.md` for the short Unraid-focused install notes. If Community
Apps requires a PNG icon, export `unraid/localair.svg` to `unraid/localair.png`
and update the template icon URL.

For your own Unraid server before publishing a public image, either use
`unraid/localair-local.xml` after building `localair:latest` locally, or run:

```sh
git clone YOUR_REPO_URL /mnt/user/appdata/localair-src
cd /mnt/user/appdata/localair-src
docker compose up -d --build
```

Then edit:

```text
/mnt/user/appdata/localair/config.toml
```

and open:

```text
http://YOUR_UNRAID_IP:8787
```

## Web UI

The LocalAIR web UI includes:

- connection and last-update status
- large temperature and humidity cards
- AC assumed power/state card
- IR control buttons
- a separate Sync Power button that does not send IR
- temperature/humidity history graph
- recent IR event list
- settings page

The Sync Power button only corrects LocalAIR's saved belief of whether the AC is
on or off. It does not transmit anything.

## History storage

Web history is stored in SQLite at:

```text
~/.local/state/localair/localair.db
```

or, in Docker:

```text
/data/localair.db
```

Raw sensor readings are kept for 31 days by default. The graph API down-samples
large ranges before sending data to the browser.

## Learned IR controls

Learned commands are stored as readable NEC bytes in the same `config.toml`
file. LocalAIR generates the pulse sequence and temporary Base64 payload when a
button is clicked.

```toml
[ir]
address = 0x08E7

[ir.commands]
power = 0x00
mode_cool = 0x28
```

IR transmission is asynchronous and does not block the dashboard. The Power,
Swing, Timer, and C/F commands are toggles: LocalAIR cannot know the AC's actual
state if another remote is also used.

While LocalAIR is running, it keeps the Tuya device in learning mode, observes
matching commands from Sensibo or the physical remote, and updates an explicitly
labelled assumed AC state. Unknown signals are written to:

```text
~/.local/state/localair/unknown-ir.log
```

For a one-shot JSON device check:

```sh
python3 scripts/read_device.py
```

The helper prints one JSON object and never includes the local key.

Run the dependency-free core tests with:

```sh
python3 -m unittest
```

## Project structure

```text
src/main.py       Application entry point
src/config.py     Settings, IR codes, and secure TOML persistence
src/device.py     Sensor reads, data models, and IR transmission
src/storage.py    SQLite history and event persistence
src/ui.py         GTK4 dashboard, settings, history, and graph
src/web.py        FastAPI web dashboard and JSON API
src/web_main.py   Web server entry point
scripts/          One-shot device helper
tests/            Core configuration and device tests
```

## Device defaults

- IP: `192.168.1.145`
- Device ID: `bfc626075b28194ecfhor5`
- Protocol: `3.5`
- Temperature: DP 1, value / 10
- Humidity: DP 2, percent

No local key is supplied in the repository.
