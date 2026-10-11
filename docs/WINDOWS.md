# Running mcf-apply on Windows

Everything below is typed in **PowerShell**: press Start, type `powershell`, press Enter.
Lines that start with `#` are notes, not commands.

## 1. Install Python and Git (once)

```powershell
winget install Python.Python.3.12
winget install Git.Git
```

Close PowerShell and open a new one so it finds them. If `winget` isn't available, install
Python from [python.org](https://www.python.org/downloads/) and tick **"Add python.exe to PATH"**,
and Git from [git-scm.com](https://git-scm.com/download/win).

## 2. Download and install mcf-apply

```powershell
cd $HOME
git clone -b claude/mycareersfuture-job-automation-gpp9xj https://github.com/Minemine-afk/applicationapp.git
cd applicationapp
py -3.12 -m venv .venv
.venv\Scripts\pip install -e .
.venv\Scripts\playwright install chromium
copy config.example.yml config.yml
copy answers.example.yml answers.yml
```

The project is now in `C:\Users\<you>\applicationapp`. Every `mcf-apply` command below is run
from this folder as `.venv\Scripts\mcf-apply ...`.

## 3. Connect Telegram

1. In Telegram, message **@BotFather**, send `/newbot`, and copy the token it gives you.
2. Save the token for your Windows user. It's stored outside the project, so it never reaches GitHub,
   and mcf-apply finds it even in the window you're in:
   ```powershell
   setx TELEGRAM_BOT_TOKEN "123456789:ABCdef..."
   ```
3. Send your new bot any message in Telegram, then run:
   ```powershell
   .venv\Scripts\mcf-apply telegram-setup
   ```
   You should get "✅ Linked to mcf-apply" in Telegram.
4. Open the config with `notepad config.yml`, change `enabled: false` under `telegram:` to
   `enabled: true`, and save.

## 4. Log in to MyCareersFuture

```powershell
.venv\Scripts\mcf-apply login
```

A browser window opens and goes straight to the Singpass login. At the same time, the Singpass
QR code is sent to your Telegram with an **Open in Singpass app** button:

- **On your phone:** tap **Open in Singpass app** and approve the login.
- **Or:** scan the QR on the PC screen with the Singpass app.

Each QR lasts about a minute. When one expires, a fresh one is sent automatically, for up to
10 minutes. Once you approve, the window closes, you get "✅ Logged in" in Telegram, and the
login is saved for later runs.

## 5. First run: try it without submitting

```powershell
.venv\Scripts\mcf-apply discover            # find jobs
.venv\Scripts\mcf-apply list --fresh        # fresh-grad jobs, best first
.venv\Scripts\mcf-apply approve 8c7d83be 9dfbf2a6   # IDs from the list
.venv\Scripts\mcf-apply questions           # employer questions waiting for you
.venv\Scripts\mcf-apply answer 1 yes        # answer truthfully: this goes to employers
.venv\Scripts\mcf-apply apply --dry-run --show-browser
```

The dry run fills in each application up to the review page **without submitting**. Look at
the screenshots in `data\screenshots`. If a step fails, send the screenshot and the `.html`
file next to it so the automation can be fixed.

## 6. Turn on real applying

Open `notepad config.yml` and under `apply:` change `mode: dry_run` to:

- `mode: manual`: submit only jobs you approve (on the command line or with Telegram's Apply button), or
- `mode: auto`: also approve jobs scoring 70+ by itself.

## 7. Keep it running

Double-click **`scripts\start-serve-windows.cmd`** in the project folder. A window titled
"mcf-apply serve" opens. Leave it open (you can minimise it): closing it stops mcf-apply.

While it runs, it searches and applies every hour between 8am and 10pm, and responds to Telegram:

- **Apply / Skip** buttons on job cards, and **Yes / No** on screening questions
- **/status** shows the queue and today's count
- **/run** runs now
- **/login** starts a new login when the old one expires. The Singpass QR and the
  **Open in Singpass app** button arrive in Telegram, so you can log in from anywhere.

### Stop the PC from sleeping

Settings → System → **Power & battery** → **Screen and sleep** → set "When plugged in, put my
device to sleep after" to **Never**. The screen may turn off; that's fine.

On a laptop, also open Control Panel → Power Options → **Choose what closing the lid does** →
"When I close the lid" (plugged in) → **Do nothing**.

### Start automatically when you log in (optional)

1. Start → type **Task Scheduler** → open it → **Create Task…** (right-hand panel).
2. **General:** name it `mcf-apply`. Keep **"Run only when user is logged on"** selected, so the
   login browser can appear on your screen.
3. **Triggers → New…:** Begin the task **At log on** → OK.
4. **Actions → New…:** Program/script: browse to
   `C:\Users\<you>\applicationapp\scripts\start-serve-windows.cmd` → OK.
5. **Conditions:** untick **"Start the task only if the computer is on AC power"**.
6. **Settings:** "If the task is already running" → **Do not start a new instance** → OK.

## Day to day

- Most things happen in Telegram.
- When you get **"MCF login expired"**, send **/login** and tap **Open in Singpass app** on the
  QR that arrives.
- To see everything: `.venv\Scripts\mcf-apply queue`.
- To update to the latest version: stop mcf-apply (close its window), then in PowerShell:
  ```powershell
  cd $HOME\applicationapp
  git pull
  .venv\Scripts\pip install -e .
  ```
  and start `scripts\start-serve-windows.cmd` again.
