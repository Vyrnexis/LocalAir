# LocalAIR

LocalAIR is an always-on Docker/Unraid web app for a local Tuya IR
temperature/humidity device.

It runs on your LAN and gives you a local dashboard for:

- temperature and humidity monitoring
- SQLite-backed history graphs
- learned AC IR controls
- Sensibo/remote IR observation
- assumed AC state tracking
- Sync Power state correction

LocalAIR does not need cloud access after you have the Tuya device details:

- device IP address
- device ID
- local key
- protocol version

## Supported device model

This first version targets a Tuya IR temperature/humidity remote like:

```text
Fish Temp / Tuya IR Temp Humidity Remote
```

Known DPS mapping:

```text
DP 1 = temperature, divide by 10
DP 2 = humidity percentage
```

Known IR format:

```text
NEC address 0x08E7
one-byte command values
```

Other Tuya IR devices may work later, but the current defaults are specific to
this device family.

## What you need before first start

You need these Tuya local-control values:

```toml
[device]
ip_address = "192.168.1.145"
device_id = "YOUR_DEVICE_ID"
local_key = "YOUR_LOCAL_KEY"
protocol_version = 3.5
```

The web UI can edit settings after the app is running, but the first launch
needs a valid `config.toml` mounted into the container.

## Getting Tuya device details

There are several ways to get the device ID, local key, IP address, and protocol
version.

Common options:

1. Use TinyTuya's wizard on a normal PC.
2. Use the Tuya IoT Platform with the Smart Life/Tuya app account.
3. Check your router/DHCP leases for the device IP address.
4. Use an existing known working TinyTuya config if you already have one.

TinyTuya wizard example:

```sh
python3 -m venv .venv
.venv/bin/pip install tinytuya
.venv/bin/python -m tinytuya wizard
```

The wizard normally helps discover devices and produce values like:

```text
Device ID
Local Key
IP Address
Version
```

For this app, put those values into `config.toml`.

Notes:

- Do not commit or publish your real local key.
- The local key is not printed by LocalAIR.
- The web Settings page leaves the local-key field blank; submitting a blank key
  keeps the existing key.

## Recommended Unraid layout

Use two separate folders:

```text
/mnt/user/appdata/localair      runtime data: config, database, logs
/mnt/user/appdata/localair-src  cloned source repo and Docker build files
```

Do not delete `/mnt/user/appdata/localair` unless you intentionally want to
remove your config and history database.

## Install on Unraid from a cloned repo

Clone the repo:

```sh
cd /mnt/user/appdata
git clone https://codeberg.org/Vyrnexis/localAIR.git localair-src
cd /mnt/user/appdata/localair-src
```

Create the runtime appdata folder:

```sh
mkdir -p /mnt/user/appdata/localair
cp -n config.example.toml /mnt/user/appdata/localair/config.toml
chmod 600 /mnt/user/appdata/localair/config.toml
```

Edit the config:

```sh
nano /mnt/user/appdata/localair/config.toml
```

At minimum, set:

```toml
[device]
ip_address = "YOUR_DEVICE_IP"
device_id = "YOUR_DEVICE_ID"
local_key = "YOUR_LOCAL_KEY"
protocol_version = 3.5
```

Build the local Docker image:

```sh
docker build -t localair:latest .
```

Install the local Unraid template:

```sh
mkdir -p /boot/config/plugins/dockerMan/templates-user

cp /mnt/user/appdata/localair-src/unraid/localair-local.xml \
  /boot/config/plugins/dockerMan/templates-user/my-localair.xml
```

Then in the Unraid web UI:

```text
Docker → Add Container → Template dropdown → LocalAIR
```

Confirm these template values:

```text
Repository: localair:latest
Network Type: Host
Appdata: /mnt/user/appdata/localair -> /data
Web UI Port: 8787
Timezone: your local timezone, e.g. Australia/Perth
```

Apply the template, then open:

```text
http://YOUR_UNRAID_IP:8787
```

## Manual Docker run

If you do not want to use the Unraid template yet:

```sh
docker build -t localair:latest .

docker run -d \
  --name localair \
  --restart unless-stopped \
  --network host \
  -e LOCALAIR_DATA=/data \
  -e LOCALAIR_PORT=8787 \
  -e TZ=Australia/Perth \
  -v /mnt/user/appdata/localair:/data \
  localair:latest
```

Useful commands:

```sh
docker logs -f localair
docker restart localair
docker stop localair
docker rm localair
```

Raw `docker run` works, but Unraid will not know the icon/WebUI/template
metadata. Use the Unraid template if you want proper Unraid UI integration.

## Updating on Unraid

From the source folder:

```sh
cd /mnt/user/appdata/localair-src
git pull
docker build -t localair:latest .
```

If the container already exists, restarting may not use the newly built image.
Docker containers keep using the image ID they were created from. To guarantee
the new image is used, remove and recreate the container from the Unraid
template:

```sh
docker stop LocalAIR || docker stop localair
docker rm LocalAIR || docker rm localair
```

Then use:

```text
Docker → Add Container → Template dropdown → LocalAIR → Apply
```

If the container was created manually with `docker run`:

```sh
docker stop localair
docker rm localair

docker run -d \
  --name localair \
  --restart unless-stopped \
  --network host \
  -e LOCALAIR_DATA=/data \
  -e LOCALAIR_PORT=8787 \
  -e TZ=Australia/Perth \
  -v /mnt/user/appdata/localair:/data \
  localair:latest
```

## Web Settings page

After LocalAIR is running, open:

```text
http://YOUR_UNRAID_IP:8787/settings
```

The Settings page can update:

- IP address
- device ID
- local key
- protocol version
- poll interval
- web host
- web port
- history retention days

Important:

- If the local-key field is left blank, LocalAIR keeps the existing saved key.
- Changing the web port may require restarting the container.
- The app stores changes back to `/data/config.toml`.
- Timezone is a Docker/Unraid environment variable named `TZ`, not a web setting.
  Set it in the Unraid template. Example: `Australia/Perth`.

## Configuration file

Docker uses:

```text
/data/config.toml
```

On Unraid this is:

```text
/mnt/user/appdata/localair/config.toml
```

Example:

```toml
[device]
ip_address = "192.168.1.145"
device_id = "bfc626075b28194ecfhor5"
local_key = "REPLACE_WITH_YOUR_LOCAL_KEY"
protocol_version = 3.5
poll_interval_seconds = 30

[server]
host = "0.0.0.0"
port = 8787

[history]
retention_days = 31

[ir]
address = 0x08E7

[ir.commands]
power = 0x00
timer = 0x80
unit_cf = 0x20
swing = 0x60
mode_fan = 0x10
mode_cool = 0x28
mode_dry = 0x30
mode_heat = 0x90
temp_up = 0xA8
temp_down = 0xB0
fan_low = 0x50
fan_mid = 0x70
fan_high = 0x68

[ac_state]
power = "on"
mode = "heat"
temperature = "25"
fan = "mid"
swing = "off"
```

## Data storage

The appdata folder contains:

```text
config.toml       device/settings config
localair.db       SQLite history database
unknown-ir.log    unknown observed IR signals
```

SQLite stores raw sensor readings for 31 days by default. Larger graph ranges
are down-sampled before being sent to the browser.

## Web dashboard

The dashboard includes:

- connection and last-update status
- large temperature and humidity cards
- AC assumed state card
- IR control buttons
- Sync Power button
- temperature/humidity history graph
- recent IR event list
- settings page

The Sync Power button only changes LocalAIR's saved power state. It does not
send an IR command.

## IR and AC state behavior

Learned commands are stored as NEC bytes in `config.toml`. LocalAIR converts
those bytes into Tuya IR payloads when sending commands.

Power, Swing, Timer, and C/F are toggle commands. Because IR is one-way,
LocalAIR tracks an assumed state. It updates that state when:

- LocalAIR sends a known IR command
- LocalAIR observes a matching Sensibo/remote command
- you press Sync Power

Unknown observed IR signals are logged to:

```text
/data/unknown-ir.log
```

## Testing the device from inside the container

If the web UI loads but the device does not connect:

```sh
docker exec -it localair python scripts/read_device.py
```

If your Unraid template created a container named `LocalAIR`, use:

```sh
docker exec -it LocalAIR python scripts/read_device.py
```

Also confirm Unraid can reach the device:

```sh
ping -c 3 YOUR_DEVICE_IP
```

Common causes of connection failure:

- missing or wrong local key
- wrong device IP
- wrong device ID
- wrong protocol version
- device moved to another Wi-Fi network/VLAN
- another client holding the Tuya connection open

## Local development

Install dependencies:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Run the web app:

```sh
LOCALAIR_DATA=/tmp/localair .venv/bin/python -m src.web_main
```

Run tests:

```sh
python3 -m unittest
```

## Project structure

```text
src/config.py      TOML config, data paths, secure saving
src/device.py      Tuya reads, IR send/listen logic, state updates
src/storage.py     SQLite history and event storage
src/web.py         FastAPI dashboard and JSON API
src/web_main.py    web server entry point
scripts/           helper scripts
tests/             core tests
unraid/            Unraid templates and icon
```
