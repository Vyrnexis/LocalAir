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

## Templates

There are two templates:

```text
localair.xml        Public install using vyrnexis/localair:latest
localair-local.xml  Local install using an image already built as localair:latest
```

Use `localair-local.xml` if you are testing on your own Unraid server before a
public image exists.

## Publishing the public template

The public template points to the Codeberg source repository:

```text
https://codeberg.org/Vyrnexis/localAIR
```

The Docker image should exist at:

```text
vyrnexis/localair:latest
```

Publish it to Docker Hub with:

```sh
docker login
docker build -t vyrnexis/localair:latest .
docker push vyrnexis/localair:latest
```

If Unraid Community Apps does not accept the SVG icon, export `localair.svg` to
`localair.png` and update the `<Icon>` URL.
