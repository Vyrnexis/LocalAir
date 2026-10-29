# Unraid templates

For installing LocalAIR on Unraid, follow
[Install on Unraid](../README.md#install-on-unraid) in the main README. This
folder holds the templates it uses.

| File | Image | Use it when |
| --- | --- | --- |
| `localair.xml` | `ghcr.io/vyrnexis/localair:latest` | Normal installs. Unraid shows updates in the Docker tab. |
| `localair-local.xml` | `localair:latest` | Testing an image you built on the server with `docker build -t localair:latest .` Unraid can't check it for updates. |
| `localair.png` | | Icon shown in the Docker tab. `localair.svg` is its source. |

## Template fields

| Field | Default | Notes |
| --- | --- | --- |
| Appdata | `/mnt/user/appdata/localair` → `/data` | Settings, local key and history. Kept across updates. |
| Web UI Port | `8787` | Host networking is used, so this is the port LocalAIR listens on. |
| Timezone | `Etc/UTC` | Set to your own, for example `Australia/Perth`. The schedule follows it. |
| Data Directory, Config File, Database File, Host Bind | `/data`, `/data/config.toml`, `/data/localair.db`, `0.0.0.0` | Advanced. Leave as they are. |
| Access Logs | `false` | Set to `true` only while debugging HTTP requests. |

## Pinning a version

`latest` follows every change on `main`. To stay on a release, edit the
container and set Repository to a version tag, for example
`ghcr.io/vyrnexis/localair:1.2`.

## Releasing

Tagging a commit publishes a versioned image alongside `latest`:

```sh
git tag v1.0.0
git push origin v1.0.0
```

This publishes `ghcr.io/vyrnexis/localair:1.0.0` and `:1.0`. The workflow is
`.github/workflows/docker.yml`.
