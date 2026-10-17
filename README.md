# Live Quiz Arena — Streamlit + SQLite

A runnable starter app with **HOST QUIZ**, **JOIN QUIZ**, room codes, QR join links, a question bank, timed multiple-choice questions, scoring, and a leaderboard.

## 1. Requirements

- Python 3.10 or newer
- A terminal / PowerShell
- For multiple devices: all devices must be able to reach the computer/server running Streamlit.

## 2. Install and run locally

Unzip the project, open a terminal in this folder, then run:

### Windows PowerShell
```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:QUIZ_ADMIN_USER="admin"
$env:QUIZ_ADMIN_PASSWORD="Set-A-Strong-Password-Here"
streamlit run app.py
```

### macOS / Linux
```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export QUIZ_ADMIN_USER="admin"
export QUIZ_ADMIN_PASSWORD="Set-A-Strong-Password-Here"
streamlit run app.py
```

Open the URL printed by Streamlit, usually `http://localhost:8501`.

**Change the default admin password** before use. If you do not set environment variables, the development defaults are username `admin` and password `ChangeMe123!`. Do not expose the app publicly with those defaults.

The SQLite database `quiz_app.db` is created automatically in the current working directory and starter questions are added on first launch.

## 3. Let phones / other computers join on the same Wi-Fi

1. Connect the host computer and participants' phones to the same trusted Wi-Fi/LAN.
2. Find the host computer's private IPv4 address:
   - Windows: `ipconfig` and look for the Wi-Fi adapter's IPv4 Address.
   - macOS/Linux: `ip addr` or `ifconfig`.
3. Start Streamlit listening on the network:
   ```bash
   streamlit run app.py --server.address 0.0.0.0 --server.port 8501
   ```
4. On a phone, open `http://HOST_PRIVATE_IP:8501` (replace `HOST_PRIVATE_IP` with the host's actual address).
5. In the app, the host creates a room. The displayed QR code uses the current app URL if it can be detected. For a reliable QR URL on a LAN, set `QUIZ_BASE_URL` to the reachable URL **before starting**:
   - PowerShell: `$env:QUIZ_BASE_URL="http://192.168.1.25:8501"`
   - macOS/Linux: `export QUIZ_BASE_URL="http://192.168.1.25:8501"`
   Replace the example IP with the actual host address.
6. Scan the QR code or enter the six-character room code.

If Windows Firewall asks, allow Python/Streamlit on **Private networks** only. Do not port-forward the app to the public internet.

## 4. Hosting over the internet

For participants outside your LAN, deploy the app to a server/platform that supports Streamlit and set `QUIZ_BASE_URL` to the deployed HTTPS URL. Set `QUIZ_ADMIN_USER` and `QUIZ_ADMIN_PASSWORD` using the platform's secrets/environment-variable settings. Keep the database on persistent storage.

Important: local SQLite is suitable for a small single-server quiz. For multiple server replicas or larger concurrent events, use a managed PostgreSQL database and a shared session store. Do not run multiple replicas that each have their own local SQLite file.

## 5. How to use

1. Open **Admin** and sign in.
2. Go to **Question bank** to add questions (four options per question).
3. Go to **HOST QUIZ** → **Create quiz**, choose questions and timer, then create a room.
4. Open **Manage live quiz**. Share the room code / QR code.
5. Click **Start / Next question**. Players submit one answer per question.
6. Click **Close answers** or let the timer expire; players see the answer and explanation after the host closes the question.
7. Continue through the questions. The final leaderboard appears when the quiz is finished.

## 6. Notes / limitations

- Active host/player sessions poll every 2 seconds so the timer, room state, and leaderboard update automatically. The app uses the shared SQLite database as the source of truth; this is polling, not a dedicated WebSocket system.
- The app is a functional starter, not a hardened exam platform. Add HTTPS, rate limits, stronger admin authentication, audit logging, backup/retention policies, and server-side authorization before production use.
- A QR code must contain a URL that participant devices can reach. `localhost` only points to the device itself; use the host's LAN IP or public deployment URL.


### Import questions from Excel

1. Open **Question bank** (from Host Quiz or Admin).
2. Click **Download Excel template**.
3. Fill one question per row and save as `.xlsx`. Required columns are `question`, `option_a`, `option_b`, `option_c`, `option_d`, and `correct_answer`. Optional columns are `category` and `explanation`.
4. In `correct_answer`, enter `A`, `B`, `C`, or `D`, or the exact text of one option.
5. Upload the workbook and click **Import questions**. The import validates the whole sheet before saving; if any row has an error, fix the sheet and upload it again.
