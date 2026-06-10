# Deploying Yeske Trade on AWS — beginner step-by-step

This guide takes you from zero to a live, always-on Yeske Trade dashboard on
AWS, behind a password. No prior server experience assumed.

> **Recommended:** AWS **Lightsail** — it's the simplest AWS product (one-click
> Ubuntu, built-in browser SSH, simple firewall). The free **EC2** alternative
> is noted at the end.

---

## What you'll have at the end
- The app running 24/7 on a small Ubuntu server (auto-restarts on crash/reboot).
- A URL like `http://<your-server-ip>/`, protected by a login.
- Scheduled auto-entry that fires even when your laptop is off.

## What you need
1. An **AWS account** (credit/debit card for verification).
2. Your **Kite API key + secret** (you have these).
3. ~20 minutes.
4. ⚠️ An active **Kite Connect API subscription** from Zerodha (paid) for live orders.

---

## Step 0 — Code is already on GitHub ✅
The redesign lives at **`https://github.com/sanketkadu13/algoTrade.git`**, branch
**`redesign`** — already pushed. Your `.env` is **git-ignored**, so your keys were
NOT uploaded. Nothing to do here; the server clones it in Step 3.

---

## Step 1 — Create the server (Lightsail)
1. Go to **https://lightsail.aws.amazon.com** → sign in.
2. Click **Create instance**.
3. **Region:** pick the one nearest you (e.g. *Mumbai, ap-south-1*).
4. **Platform:** Linux/Unix → **Blueprint:** *OS Only* → **Ubuntu 22.04 LTS**.
5. **Instance plan:** the **$5/mo** (or $7) plan is plenty (1 GB RAM).
   *(Lightsail currently gives the first 3 months free.)*
6. **Name it** `yeske-trade` → **Create instance**.
7. Wait until it shows **Running** (~1 min).

### Give it a fixed IP (so the address never changes)
8. Open the instance → **Networking** tab → **Create static IP** → attach it to
   `yeske-trade` → note the IP (e.g. `13.x.x.x`). This is your dashboard address.

### Open the firewall
9. Same **Networking** tab → **IPv4 Firewall**. Make sure these rules exist:
   - **SSH** TCP 22 (already there)
   - **HTTP** TCP 80 — add it if missing
   - (later, if you add HTTPS) **HTTPS** TCP 443

---

## Step 2 — Connect to the server
On the instance page, click **Connect using SSH**. A black terminal opens in your
browser — no keys or passwords to set up. You're now "inside" the server.

---

## Step 3 — Get the code and run the installer
Paste these one block at a time (right-click to paste in the browser SSH):
```bash
sudo apt update
cd ~
git clone -b redesign https://github.com/sanketkadu13/algoTrade.git
cd algoTrade
bash deploy/install.sh
```
The first run installs packages, creates `/opt/kite`, copies files — then **stops
on purpose** with:
> `!! No .env found at /opt/kite/.env`

That's expected. Next step creates it.

---

## Step 4 — Add your credentials
Create the secrets file:
```bash
nano /opt/kite/.env
```
Type (or paste) this, using **your** keys:
```
API_KEY=woicdj3h53q3m89i
API_SECRET=fwsxe1agkdv4if9ss8cj1ncgixax1uht
ACCESS_TOKEN=
```
Save and exit nano: **Ctrl+O**, **Enter**, then **Ctrl+X**.
*(Leave `ACCESS_TOKEN` blank — you'll fill it from the website in Step 6.)*

---

## Step 5 — Finish the install
Run the installer again — this time it completes:
```bash
cd ~/algoTrade
bash deploy/install.sh
```
Partway through it asks you to **set a login password** (the username is `admin`).
Type a strong password (twice). This is what protects your dashboard.

When it prints `==> Done.`, the app is live.

---

## Step 6 — First login + Kite token
1. In your browser go to **`http://<your-static-ip>/`**.
2. Browser asks for a login → username **`admin`**, your password from Step 5.
3. The Yeske Trade dashboard loads. Click **🔑 Refresh Token**:
   - Click **Open Kite Login** → log in with your Zerodha PIN + TOTP.
   - Your browser shows a "site can't be reached" page at `127.0.0.1?request_token=…` — **that's expected**. Copy the **full URL** from the address bar.
   - Paste it into the box → **Submit & Save**.
4. You now have live data. 🎉

---

## Step 7 — The one daily chore (important)
Kite access tokens **expire every morning (~6 AM IST)**. Each trading day you must
refresh it. Two ways:

**A) Manual (30 seconds):** open the site → **🔑 Refresh Token** → log in → paste URL.

**B) Automatic (set once):** add your Kite web-login + TOTP secret so the app
refreshes itself at 8:30 AM IST:
```bash
nano /opt/kite/.env
```
add:
```
KITE_USER_ID=YOUR_KITE_ID
KITE_PASSWORD=YOUR_KITE_PASSWORD
KITE_TOTP_SECRET=YOUR_TOTP_SECRET
```
*(Get `KITE_TOTP_SECRET` from Zerodha's external-2FA/TOTP setup — it's the secret
key behind the QR code.)* Then:
```bash
sudo systemctl restart kite-monitor
```

---

## Step 8 (optional) — A real domain + HTTPS
If you own a domain (e.g. from Namecheap/GoDaddy):
1. Point an **A record** to your static IP.
2. On the server:
```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d yourdomain.com
```
Follow the prompts — it auto-configures HTTPS and renews itself. Then use
`https://yourdomain.com/`.

---

## Day-to-day commands (browser SSH)
| Task | Command |
|---|---|
| See live logs | `tail -f /var/log/kite-monitor.log` |
| Service status | `sudo systemctl status kite-monitor` |
| Restart app | `sudo systemctl restart kite-monitor` |
| **Update to latest code** | `cd ~/algoTrade && git pull && bash deploy/install.sh` |

---

## Safety checklist (please read)
- [ ] **This places REAL orders with REAL money.** Start with tiny quantities.
- [ ] Use a **strong** dashboard password.
- [ ] In Lightsail firewall, consider restricting **SSH (22)** to *your IP only*.
- [ ] Add **HTTPS** (Step 8) before using it on public Wi-Fi.
- [ ] Never share or commit your `.env` / API secret.
- [ ] Refresh the Kite token each trading day (Step 7).

---

## Free alternative: EC2 (free for 12 months)
Prefer $0 for the first year? Use **EC2** instead of Lightsail:
1. EC2 → **Launch instance** → **Ubuntu 22.04**, type **t3.micro** (or t2.micro) — "Free tier eligible".
2. **Create a key pair** (download the `.pem`) — keep it safe.
3. **Security group:** allow **SSH 22** (your IP) and **HTTP 80** (anywhere).
4. Launch → allocate an **Elastic IP** and associate it (so the IP is fixed).
5. Connect from Windows PowerShell:
   ```powershell
   ssh -i path\to\key.pem ubuntu@<elastic-ip>
   ```
6. From here, **Steps 3–8 are identical**.

> Trade-off: EC2 is free for 12 months but fiddlier (keys, security groups).
> Lightsail costs a few dollars but is far simpler. Both run the same app.
