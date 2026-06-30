# LocalAIR on Unraid

LocalAIR is intended to run as an always-on Unraid Docker container.

## Appdata

Create:

```sh
mkdir -p /mnt/user/appdata/localair
```

Copy the example config:

```sh
cp config.example.toml /mnt/user/appdata/localair/config.toml
chmod 600 /mnt/user/appdata/localair/config.toml
```

Edit:

```sh
nano /mnt/user/appdata/localair/config.toml
```

At minimum, set:

```toml
[device]
ip_address = "192.168.1.145"
device_id = "bfc626075b28194ecfhor5"
local_key = "YOUR_REAL_LOCAL_KEY"
protocol_version = 3.5
```

## Template settings

Recommended defaults:

```text
Network: host
Appdata: /mnt/user/appdata/localair -> /data
Web UI Port: 8787
Data Directory: /data
Config File: /data/config.toml
Database File: /data/localair.db
```

Open:

```text
http://YOUR_UNRAID_IP:8787
```

## Publishing the template

Before sharing the template publicly, replace every `your-github-user`
placeholder in `localair.xml` with the real GitHub username or organisation.

The Docker image should exist at:

```text
ghcr.io/YOUR_GITHUB_USER/localair:latest
```

The included GitHub Actions workflow publishes that image automatically from the
`main` branch.

If Unraid Community Apps does not accept the SVG icon, export `localair.svg` to
`localair.png` and update the `<Icon>` URL.
