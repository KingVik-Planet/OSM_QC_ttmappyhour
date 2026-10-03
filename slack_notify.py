"""
Slack posting for the hourly summary.

INTENTIONALLY NOT WIRED IN YET: main.py imports and calls
post_summary() every run, but it's a silent no-op until
config.SLACK_ENABLED is turned on (env var QC_SLACK_ENABLED=1) and
SLACK_BOT_TOKEN / SLACK_CHANNEL_ID are set. That way, turning Slack on
later is a one-line config change, not a code change.
"""
from collections import Counter

import requests

import config

# Slack messages have a hard length limit -- past this many rows, list the
# most recent N and point to the full CSV instead of trying to cram
# everything in (and risk the message failing to send at all).
MAX_ROWS_IN_MESSAGE = 25


def build_summary_text(window_start, window_end, issues, csv_path=None):
    counts = Counter(i["error_type"] for i in issues)
    lines = [
        f"*OSM #{config.HASHTAG} quality check* — "
        f"{window_start:%Y-%m-%d %H:%M} to {window_end:%H:%M} UTC",
        f"Total issues found: *{len(issues)}*",
    ]

    if not issues:
        lines.append("No issues found in this window. ✅")
        return "\n".join(lines)

    for error_type, n in counts.most_common():
        lines.append(f"• {error_type}: {n}")

    shown = issues[:MAX_ROWS_IN_MESSAGE]
    # Fixed-width columns inside a code block -- the closest thing to a
    # real table Slack's plain message text supports.
    user_w = max(4, min(20, max(len(i["username"]) for i in shown)))
    type_w = max(4, min(30, max(len(i["error_type"]) for i in shown)))

    table_lines = [f"{'User':<{user_w}}  {'Date (UTC)':<19}  {'Issue type':<{type_w}}"]
    table_lines.append("-" * (user_w + type_w + 25))
    for row in shown:
        user = row["username"][:user_w].ljust(user_w)
        date = (row.get("time_utc") or "")[:19].ljust(19)
        etype = row["error_type"][:type_w].ljust(type_w)
        table_lines.append(f"{user}  {date}  {etype}")

    lines.append("```")
    lines.extend(table_lines)
    lines.append("```")

    if len(issues) > MAX_ROWS_IN_MESSAGE:
        lines.append(f"_...showing {MAX_ROWS_IN_MESSAGE} of {len(issues)} issues. Full list in the CSV below._")

    if csv_path:
        lines.append(f"Full detail: `{csv_path}`")

    return "\n".join(lines)


def post_summary(window_start, window_end, issues, csv_path=None):
    if not config.SLACK_ENABLED:
        return
    if not config.SLACK_BOT_TOKEN or not config.SLACK_CHANNEL_ID:
        return

    text = build_summary_text(window_start, window_end, issues, csv_path)

    requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {config.SLACK_BOT_TOKEN}"},
        json={"channel": config.SLACK_CHANNEL_ID, "text": text},
        timeout=30,
    )
