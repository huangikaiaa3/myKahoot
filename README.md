# Spark Quiz

A one-room, real-time quiz for up to 150 players. The app is a FastAPI server with a WebSocket connection per player and an original mobile-first interface.

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` for the player screen. Select **Host controls** in a separate browser tab, then share the displayed PIN.

The host dashboard also shows a QR code that opens the player entry page. For local phone testing, run the server with `--host 0.0.0.0` and open it on your computer using its local network IP, such as `http://192.168.1.25:8000`; QR codes containing `127.0.0.1` only work on the computer itself. On EC2, the QR code uses your public domain automatically.

The first launch uses the three default questions in `app.py`. After you save changes in the question editor, the editable quiz is stored separately in `data/questions.json`.

## Question editor

The unlinked `/questions` page lets you add, edit, and remove questions without changing code. Its changes are saved to `data/questions.json` and survive server restarts. The editor is protected with an HTTP Basic password; it is not exposed from the host or player pages.

Set a strong password before starting the server:

```bash
export QUESTIONS_ADMIN_PASSWORD='replace-this-with-a-long-unique-password'
python3 -m uvicorn app:app --host 0.0.0.0 --port 8000
```

On EC2, replace the `QUESTIONS_ADMIN_PASSWORD` value in `deploy/spark-quiz.service`, then run `sudo systemctl daemon-reload` and `sudo systemctl restart spark-quiz`. Visit `https://your-domain/questions` and enter any username plus that password when prompted.

## Load test

With the server already running, simulate 150 players joining, answering, and receiving the reveal:

```bash
python3 tests/load_test.py
```

The test resets the active game first. To test the deployed EC2 instance, replace the endpoint with your public WebSocket URL, for example `python3 tests/load_test.py --url wss://quiz.example.com/ws`.

## EC2 deployment

Use one Ubuntu EC2 instance. A `t3.small` is ample for one 150-player game; choose a region near the venue. Attach a security group that allows TCP 22 only from your IP and TCP 80/443 from the internet. Do not expose port 8000 publicly.

Install Python, create the virtual environment, and install the requirements:

```bash
sudo mkdir -p /opt/spark-quiz
sudo chown ubuntu:ubuntu /opt/spark-quiz
# Copy this project into /opt/spark-quiz first.
cd /opt/spark-quiz
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Copy `deploy/spark-quiz.service` to `/etc/systemd/system/`, then start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now spark-quiz
sudo systemctl status spark-quiz
```

Install Nginx and use `deploy/nginx.conf` as the site configuration, replacing `quiz.example.com` with your domain. It proxies both HTTP and WebSocket traffic to `127.0.0.1:8000`. Obtain and renew the HTTPS certificate with Certbot. Run Uvicorn as one worker: quiz state is intentionally in memory, so multiple workers would split players across separate games.

Before the event, test with several devices on both Wi-Fi and cellular data. Keep the process running for the duration of the quiz: restarting it clears the lobby and scores.
