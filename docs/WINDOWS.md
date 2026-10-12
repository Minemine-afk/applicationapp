# Running mcf-apply on Windows

You type commands in **PowerShell** once, to install. After that everything happens in
**Telegram**.

## 1. Install (once)

Open PowerShell (Start → type `powershell` → Enter) and run:

```powershell
winget install Python.Python.3.12
winget install Git.Git
```

Close PowerShell, open a new one, then:

```powershell
cd $HOME
git clone -b claude/mycareersfuture-job-automation-gpp9xj https://github.com/Minemine-afk/applicationapp.git
cd applicationapp
py -3.12 -m venv .venv
.venv\Scripts\pip install -e .
.venv\Scripts\mcf-apply setup
```

If you already have the folder, replace the `git clone` line with `git pull` (inside
`applicationapp`), then run the last two lines.

`mcf-apply setup` walks you through the rest. It:

1. Creates `config.yml` and `answers.yml` if they don't exist.
2. Asks for your Telegram bot token (create a bot by messaging **@BotFather** with `/newbot`),
   checks it works and saves it for your Windows user. If you already saved one with `setx`,
   it uses that.
3. Turns Telegram on in `config.yml`.
4. Installs the browser it uses to apply.
5. Asks you to press **Start** on your bot in Telegram, which links your chat. Only your chat
   can control it.
6. Makes mcf-apply start (minimised) every time you log in to Windows, and starts it now.
7. Asks whether to stop the PC sleeping while plugged in. Say yes: hourly runs need it awake.

## 2. Everything else: Telegram

Open your bot in Telegram. Type `/` to see the commands.

| Command | What it does |
|---|---|
| `/login` | Logs in to MyCareersFuture. A Singpass QR arrives with an **Open in Singpass app** button: tap it and approve. Fresh QRs follow every minute until you're in. |
| `/mode` | Choose how it applies: **🧪 Dry run** (fills forms, sends you screenshots, never submits), **✋ Manual** (only jobs you tap Apply on), **🤖 Auto** (also applies to high-scoring jobs by itself). Starts in dry run. |
| `/jobs` | The best jobs you haven't applied to, with **Apply** and **Skip** buttons. `/jobs 10` shows 10. |
| `/questions` | Employers' screening questions waiting for you: tap Yes/No or an option, or reply to the card with your answer. Answer truthfully: they go to employers. |
| `/jobstreet on` | Turns on JobStreet as a second site (read the warning it shows). `/jobstreet off` turns it off. |
| `/login jobstreet` | Opens JobStreet's sign-in in a window on the PC for you to log in. |
| `/update` | Installs the latest version and restarts. |
| `/run` | Search and apply now, instead of waiting for the hourly run. |
| `/apply` | Apply to the queue now, without searching first (faster). |
| `/limits` | Show or set how many to apply per hour and per day, e.g. `/limits 6 30`. |
| `/queue` | What's still to be submitted, and what was applied to today. |
| `/status` | Mode, today's count, and whether anything is paused. |
| `/pause`, `/resume` | Stop or restart submissions. Searching continues. |
| `/help` | All commands. |

What arrives without asking:

- **Job cards** for good matches, after each hourly search
- **Screening questions** when a job you approved has one
- **Run summaries** with screenshots of dry runs and failures
- **Alerts** when the login expires (send `/login`) or submissions pause

Before each applying run it reads your **Applied Jobs** list from MyCareersFuture, so jobs you've
already applied to (even by hand), and reposts of them, are skipped automatically.

### A good first week

1. `/login` and approve in the Singpass app.
2. `/jobs` and tap **Apply** on a few. Answer any `/questions`.
3. `/run` (still in dry run) and check the screenshots it sends.
4. When they look right, `/mode` → **✋ Manual** or **🤖 Auto**.

## If something's wrong

- **No replies in Telegram:** mcf-apply isn't running. Log out and back in to Windows, or
  double-click `scripts\start-serve-windows.cmd` in the project folder. A minimised window
  called **mcf-apply** should appear in the taskbar.
- **Stop it for good:** close the mcf-apply window, and delete `mcf-apply.cmd` from the Startup
  folder (Win+R → `shell:startup`).
- **Update to the latest version:** send **/update** to the bot. It pulls the new code and
  restarts; the start script reinstalls it before starting, and "🟢 mcf-apply is running" means
  it's back. If the bot isn't answering at all, do it in PowerShell instead:
  ```powershell
  cd $HOME\applicationapp
  git pull
  .venv\Scripts\pip install -e .
  .venv\Scripts\mcf-apply setup
  ```
  Setup is safe to re-run: it keeps your settings and answers.
- **The mcf-apply window keeps printing `ModuleNotFoundError: No module named 'mcf_autoapply'`:**
  the install was removed while the app was running (older versions of /update did this). Close
  that window, then in PowerShell run the four lines above; the new start script reinstalls
  before every start so it can't happen again.
- **Send the developer a failure:** the screenshot and page file are in `data\screenshots`.
- **JobStreet:** off by default (its rules forbid automation; see README → JobStreet). Send
  **/jobstreet on**, then **/login jobstreet** and sign in in the window that opens on the PC.
