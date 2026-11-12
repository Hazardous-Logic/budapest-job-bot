# LinkedIn Budapest jobs → Telegram

A small, dependency-free Python bot that checks LinkedIn job-alert emails, keeps new matching listings, and sends one daily Telegram digest. It does not sign in to or scrape LinkedIn. LinkedIn itself sends the job alerts; the bot reads those messages from your mailbox.

The default title filters are software engineering and cybersecurity. You can change them in Telegram with `/roles backend engineer, security analyst`.

## 1. Create LinkedIn job alerts

In LinkedIn Jobs, search for a role such as **Software Engineer** and set the location to **Budapest, Hungary**. Turn on the job alert and choose daily email notifications. Repeat for searches such as **Cybersecurity**, **Security Engineer**, or **SOC Analyst**. LinkedIn's daily alert email is the source for this bot.

## 2. Create the Telegram bot

In Telegram, open [@BotFather](https://t.me/BotFather), run `/newbot`, and copy the token it gives you. Keep the token private.

## 3. Connect the alert mailbox

Copy `.env.example` to `.env` and fill in the Telegram token and IMAP settings. For Gmail, use `imap.gmail.com`, enable IMAP in Gmail settings, and create a Google App Password for the bot. Use the mailbox that receives LinkedIn alerts. The bot only reads messages from `linkedin.com` senders, and selects the mailbox read-only.

PowerShell quick start:

```powershell
Copy-Item .env.example .env
notepad .env
py bot.py
```

Python 3.10 or newer is required. The bot uses only the Python standard library. Keep the process running for daily checks; for unattended use, run it on a machine or server that stays on.

## 4. Subscribe and customize

Open your bot in Telegram and send `/start`. The bot stores that chat as the digest recipient.

- `/roles` shows the current filters.
- `/roles Java developer, application security` replaces them with comma-separated title terms.
- `/digest` sends queued listings immediately.
- `/status` shows whether Telegram and mailbox settings are configured.
- `/stop` unsubscribes the chat from digests.

The default digest is sent once daily at `DAILY_HOUR` in `TIMEZONE` (09:00 Europe/Budapest by default). It includes new matching job links received in the last three days. Existing old mailbox messages are ignored; new LinkedIn alerts are collected while the bot runs.

## Notes

- LinkedIn alerts should be configured for Budapest in LinkedIn; the bot filters alert titles against your role terms but does not re-check the job's location from LinkedIn's email.
- Job titles that do not match your configured role terms are skipped. Keep terms broad enough to include title variants.
- The first run starts watching for new email. If an alert arrives before the bot starts, forward that LinkedIn alert to the connected mailbox after startup so it can be picked up.
- If a LinkedIn email template has no readable job links, the bot may not extract listings from that message.

