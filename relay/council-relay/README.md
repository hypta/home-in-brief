# council-relay

A small service for a Raspberry Pi in New Zealand. Auckland Council answers HTTP 406 to cloud hosts outside New
Zealand, so property-api (Cloud Run, Australia) cannot read the council property page directly. From a New Zealand
address, with the project's honest User-Agent (`home-in-brief/0.1 (+https://github.com/hypta/home-in-brief;
non-commercial)`), the page answers 200. This relay fetches that one page for property-api.

```
property-api (Cloud Run) --https + X-Relay-Token--> Tailscale Funnel --> 127.0.0.1:8787 (Docker, this service)
                                                                          --> www.aucklandcouncil.govt.nz
```

## API

| Request | Answer |
|---|---|
| `GET /healthz` (no token, never calls the council) | `200 {"status":"ok"}` |
| `GET /v1/property-page?key=<rates key>` with header `X-Relay-Token` | `200` and the council page HTML (`text/html`) |

Errors are JSON `{"error": ...}`: `401` bad or missing token, `400` key is not 5-15 digits (or missing/repeated),
`404` any other path, `429` rate limit, `502` the council answered something other than 200 (adds
`"upstream_status"`), `504` the council timed out. Only successful pages are cached.

## Security design

- **Not a general proxy.** The only input is a rates key matching `[0-9]{5,15}`. The service builds the council URL
  itself; no URL, host or path is ever taken from the caller. Redirects from the council are never followed.
- **Token.** `X-Relay-Token` is compared in constant time with `RELAY_TOKEN`. The service refuses to start if the token
  is unset or shorter than 16 characters. Tokens and page contents are never logged (the log has the key, cache
  hit or miss, and status).
- **Low frequency.** At most 20 council fetches per minute in total (`RATE_LIMIT_PER_MINUTE`), then 429. Pages are
  cached in memory for 24 hours (`CACHE_TTL_SECONDS`, at most `CACHE_MAX_ENTRIES`=300, least recently used out), so a
  repeated address does not reach the council again. One fetch at a time, 8 second timeout, GET only.
- **Isolation from the other services on the Pi.** Docker container, non-root user (uid 10001), read-only root
  filesystem with a tmpfs `/tmp`, 128 MB memory limit, half a CPU, 64 processes, all capabilities dropped,
  `no-new-privileges`, published only on `127.0.0.1:8787`. No router port is opened; Tailscale Funnel provides the
  public https address. Standard library only, so there is nothing to patch besides the base image.
- **Council terms.** The council's terms of use allow non-commercial, single-page use with acknowledgement and do not
  allow bulk copying (https://www.aucklandcouncil.govt.nz/en/terms-and-conditions.html, checked 2026-10-14). The
  rate limit, the cache and the one-key-per-request design keep this project to that.

## Set up on a clean Raspberry Pi

Start from Raspberry Pi OS Lite (64-bit), flashed with Raspberry Pi Imager with SSH enabled and your Wi-Fi or a
cable. Steps 1-4 install the platform, 5-6 prepare Tailscale, 7-8 the relay, 9-10 the public address.

**1. Check the OS and architecture.**

```bash
uname -m                      # must print aarch64 (64-bit); armv7l means the 32-bit image: reflash 64-bit
. /etc/os-release && echo "$PRETTY_NAME"      # Debian GNU/Linux 12 (bookworm) or 13 (trixie)
```

**2. Update the system.**

```bash
sudo apt update && sudo apt full-upgrade -y && sudo reboot
```

**3. Install Docker Engine.** Docker's page for Raspberry Pi OS covers only 32-bit and sends 64-bit users to the Debian
page (https://docs.docker.com/engine/install/raspberry-pi-os/); Debian 12 and 13 on arm64 are supported
(https://docs.docker.com/engine/install/debian/). Official apt repository:

```bash
sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: $(. /etc/os-release && echo "$VERSION_CODENAME")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable docker.service containerd.service     # start on boot (usually already the default)
sudo docker run --rm hello-world
```

(Docker's convenience script, `curl -fsSL https://get.docker.com -o get-docker.sh && sudo sh get-docker.sh`, also
works, but Docker recommends it for development and testing only.)

**4. Let your user run Docker** (https://docs.docker.com/engine/install/linux-postinstall/). The `docker` group is
root-equivalent on this machine; acceptable for a single-purpose Pi.

```bash
sudo usermod -aG docker $USER && newgrp docker      # or log out and in again
docker run --rm hello-world
```

**5. Install Tailscale and log in** (https://tailscale.com/kb/1031/install-linux).

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up            # prints a URL: open it on any device and sign in; the Pi appears in the admin console
sudo systemctl enable --now tailscaled     # make sure it starts on boot
```

**6. Enable HTTPS and Funnel for the tailnet** (https://tailscale.com/kb/1223/funnel). Funnel needs MagicDNS and HTTPS
certificates enabled (admin console, DNS page: https://login.tailscale.com/admin/dns) and a `funnel` node attribute
in the tailnet policy file. By default every tailnet member already has it; the entry looks like this (Access
controls page, https://login.tailscale.com/admin/acls):

```json
"nodeAttrs": [{"target": ["autogroup:member"], "attr": ["funnel"]}]
```

Running `sudo tailscale funnel --bg 8787` (step 9) opens an approval link that does the HTTPS and policy steps for you
if they are missing; only an Owner, Admin or Network admin can approve it.

**7. Get the relay onto the Pi** (only this folder is needed). From your computer:

```bash
scp -r relay/council-relay <user>@<pi>:~/council-relay          # or: git clone the repo and cd relay/council-relay
```

**8. Create `.env`, start, and test locally** (on the Pi, in `~/council-relay`).

```bash
echo "RELAY_TOKEN=$(openssl rand -hex 32)" > .env     # keep this value: Cloud Run needs the same one
chmod 600 .env
docker compose up -d --build
curl -s localhost:8787/healthz                               # {"status":"ok"}
T=$(grep '^RELAY_TOKEN=' .env | cut -d= -f2-)
curl -s -o /dev/null -w '%{http_code}\n' "localhost:8787/v1/property-page?key=12345450368"                 # 401
curl -s -o /dev/null -w '%{http_code}\n' -H "X-Relay-Token: $T" "localhost:8787/v1/property-page?key=abc"  # 400
curl -s -H "X-Relay-Token: $T" "localhost:8787/v1/property-page?key=<a real rates key>" | head -c 300      # one real page
docker compose logs --tail 5          # key, cache=miss, status=200; no token, no HTML
```

Do not paste the token into chat, issues or commits. `restart: unless-stopped` plus the enabled Docker service bring
the relay back after a reboot.

**9. Publish it with Tailscale Funnel.**

```bash
sudo tailscale funnel --bg 8787
tailscale funnel status                                      # shows https://<device>.<tailnet>.ts.net
```

`--bg` keeps Funnel running across reboots and restarts of Tailscale (https://tailscale.com/kb/1311/tailscale-funnel).
Funnel's public port is 443 by default (443, 8443 and 10000 are allowed). The bare `8787` target form comes from the
tailscale CLI help; check it on your client version. The public DNS name can take up to 10 minutes to appear.

**10. Check from outside.** From another network (a phone on mobile data, or any other computer):

```bash
curl -s https://<device>.<tailnet>.ts.net/healthz            # {"status":"ok"}
```

That address is `COUNCIL_RELAY_URL`; the token is `COUNCIL_RELAY_TOKEN` (see `DEPLOY.md`, section 8e).

## Day-to-day

```bash
docker compose logs -f --tail 50          # watch requests (no tokens or HTML in the log)
docker compose ps                         # state and health
docker compose up -d --build              # after copying a newer folder over: rebuild and restart
sudo apt update && sudo apt full-upgrade  # OS updates; reboot when asked
```

Rotate the token: put a new `RELAY_TOKEN` in `.env`, `docker compose up -d`, add the same value as a new version of
the `COUNCIL_RELAY_TOKEN` secret, and redeploy property-api.

### Turn it off

```bash
sudo tailscale funnel --https=443 off      # or: sudo tailscale funnel reset
docker compose down
```

Without the relay, property-api still answers: the four council-page fields are `unavailable` and the user enters
the year built and cladding.

## Run and test without Docker

```bash
RELAY_TOKEN=local-test-token-1234 python relay.py        # port 8787
make test-relay                                           # from the repository root; no network
```
