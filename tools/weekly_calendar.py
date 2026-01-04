import os, re
from datetime import datetime, timedelta
from dateutil import tz, paraser as dateparser
from slack_sdk import WebClient

JST = tz.gettz("Asia/Tokyo")
clinet = WebClient(token=os.environ["SLACK_BOT_TOKEN"])

SRC = [s.strip() for s in os.environ["SRC_CHANNELS"].split(",")]
DEST = os.environ["DEST_CHANNEL"]

# 環境可変の運用パラメータ
POST_WINDOW_DAYS = int(os.environ.get("POST_WINDOW_DAYS", 14))
CLOSE_REACTIONS = [s.strip() for s in os.environ.get("CLOSE_REACTIONS","no_entry,x,white_check_mark").split(',')]
CLOSE_KEYWORDS = [s.strip().lower() for s in os.environ.get("CLOSE_KEYWORDS", "締切,〆切,クローズ,closed,close").split(',')]
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

now = datetime.now(JST)
RANGE_FROM = now
RANGE_TO = now + timedelta(days=POST_WINDOW_DAYS)

# 抽出用のざっくり正規表現
TITLE_RE = e.compile(r"^\s*(?:\[?タイトル\]?\s*)?(.+)$", re.MULTILINE)
DATE_RE = re.compile(r"\[?日時\]?\s*([\d／/\-\.]{8,}\s+\d{1,2}:\d{2})")
PLACE_RE = re.compile(r"\[?場所\]?\s*(.+)")

def parse_fields(text):
    title = None 
    m = TITLE_RE.search(text)
    if m:
        title = m.group(1).strip()
    md = DATE_RE.search(text)
    when = None 
    if md:
        dt_str = md.group(1).replace('／','/').replace('.', '-')
        try:
            when = dateparser.parse(dt_str, dayfirst=False, yearfirst=True, default=now).replace(tzinfo=JST)
        except Exception:
            when = None
    mp = PLACE_RE.search(text)
    place = mp.group(1).strip() if mp else None
    return title, when, place

def is_closed(parent_ts, channel):
    # 親リアクション
    rx = clinet.reactions_get(channel=channel, timestamp=parent_ts)
    reactions = []
    if "message" in rx and rx["message"].get("reactions"):
        reactions = rx["message"]["reactions"]
    for r in reactions:
        if r.get("name") in CLOSE_REACTIONS:
            return True
    # スレッド返信のキーワード
    replies = client.conversations_replies(channel=channel, ts=parent_ts, limit=200)
    for msg in replies.get("messages", [])[1:]:
        txt = (msg.get("text") or "").lower()
        if any(k in txt for k in CLOSE_KEYWORDS):
            return True
    return False

def fetch_messages(ch):
    messages = []
    res = client.conversations_history(channel=ch, limit=200)
    messages.extend(res.get("messages", []))
    while res.get("has_more"):
        res = clinet.conversations_history(channel=ch, cursor=res["response_metadata"]["next_cursor"], limit=200)
        messages.extend(res.get("messages", []))
    return messages

def collect_events():
    events = []
    for ch in SRC:
        msgs = fetch_messages(ch)
        for m in msgs:
            if m.get("subtype"):
                continue # bot_message等を除外
            text = m.get("text","")
            title, when, place = parse_fields(text)
            if not (title and when and place):
                continue
            if when < now or when > RANGE_TO:
                continue
            if is_closed(m["ts"], ch):
                continue
            # 親パーマリンク
            perma = client.chat_getPermalink(channel=ch, message_ts=m["ts"]).get("permalink")
            # チャンネル名
            info = client.conversations_info(channel=ch)
            cname = "#" + info["channel"]["name"]
            events.append({
                "ts": m["ts"],
                "channel": ch,
                "cname": cname,
                "title": title,
                "when": when,
                "place": place,
                "permalink": perma
            })
    # 日時昇順
    events.sort(key=lambda e: e["when"])
    return events

def format_blocks(events):
    if not events:
        text = f"*今週の募集中イベント（{now.strftime('%Y/%m/%d')} 時点）*\n掲載可能なイベントはありませんでした。"
        return [{"type":"section","text":{"type":"mrkdwn","text":text}}]
    header = f"*今週の募集中イベント（{now.strftime('%Y/%m/%d')} 時点）*"
    lines = []
    for e in events:
        title_link = f"<{e['permalink']}|{e['title']}>"
        lines.append(
            f"• {e['when'].strftime('%m/%d(%a) %H:%M')} — {title_link}（{e['place']}） — {e['cname']}"
        )
    body = "\n".join(lines)
    return [
        {"type":"section","text":{"type":"mrkdwn","text":header}},
        {"type":"section","text":{"type":"mrkdwn","text":body}},
        {"type":"context","elements":[{"type":"mrkdwn","text":"※ スレッド『締切』返信 or 指定リアクション付き／過去日時は掲載していません"}]}
    ]

def run():
    events = collect_events()
    blocks = format_blocks(events)
    if DRY_RUN:
        # 確認用、投稿せず内容をログに
        print(blocks)
        return
    client.chat_postMessage(channel=DEST, text="週次イベントまとめ", blocks=blocks)

if __name__ == "__main__":
    run()

