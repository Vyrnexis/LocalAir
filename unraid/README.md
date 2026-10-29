# LocalAIR on Unraid

LocalAIR is intended to run as an always-on Unraid Docker container.

For local source builds, this documentation assumes the repository is cloned to:

```text
/mnt/user/appdata/localAIR
```

That is the default folder name created by:

```sh
git clone https://github.com/Vyrnexis/LocalAir.git localAIR
```

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
Timezone: your local timezone, e.g. Australia/Perth
Data Directory: /data
Config File: /data/config.toml
Database File: /data/localair.db
```

Open:

```text
http://YOUR_UNRAID_IP:8787
```

## Templates

There are two templates:

```text
localair.xml        Normal install using ghcr.io/vyrnexis/localair:latest
localair-local.xml  Local install using an image already built as localair:latest
```

Use `localair-local.xml` only when testing an image you built yourself on the
Unraid server.

## Installing the published image

The GitHub Actions workflow in `.github/workflows/docker.yml` runs the tests and
publishes a multi-arch image to the GitHub Container Registry:

```text
ghcr.io/vyrnexis/localair:latest   every push to main
ghcr.io/vyrnexis/localair:1.2.3    every v1.2.3 tag (also :1.2)
ghcr.io/vyrnexis/localair:sha-abc  every build
```

On Unraid, install the template straight from GitHub:

```sh
mkdir -p /boot/config/plugins/dockerMan/templates-user
wget -O /boot/config/plugins/dockerMan/templates-user/my-localair.xml \
  https://raw.githubusercontent.com/Vyrnexis/LocalAir/main/unraid/localair.xml
```

Then use `Docker → Add Container → Template → LocalAIR → Apply`. No clone or
local build is needed, and the Docker tab shows when an update is available.
Updating is `Docker → LocalAIR → Update`.

To pin a release instead of following `main`, change the template's Repository
to a version tag, e.g. `ghcr.io/vyrnexis/localair:1.2`.

## Releasing

```sh
git tag v1.0.0
git push origin v1.0.0
```

The first time the workflow publishes, the GHCR package is private. Make it
public once under GitHub → Packages → localair → Package settings → Change
visibility, or Unraid will not be able to pull it.

The Unraid Docker page may show `Version: not available` when using
`localair-local.xml`. That is expected for a locally built image, because
Unraid can only check for updates against a registry image.
