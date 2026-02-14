import os, re
from datetime import datetime, timedelta
from dateutil import tz, parser as dateparser
from slack_sdk import WebClient
import unicodedata

DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

if DRY_RUN:
    print("=== DRY RUN MODE ===")
    os.environ["SLACK_BOT_TOKEN"] = os.environ.get("SLACK_BOT_TOKEN_DEBUG")
    os.environ["SRC_CHANNELS"] = os.environ.get("SRC_CHANNELS_DEBUG")
    os.environ["DEST_CHANNEL"] = os.environ.get("DEST_CHANNEL_DEBUG")
    os.environ["FREE_CHANNELS"] = os.environ.get("FREE_CHANNELS_DEBUG")
    os.environ["PAID_CHANNELS"] = os.environ.get("PAID_CHANNELS_DEBUG")
    os.environ["OTHER_CHANNELS"] = os.environ.get("OTHER_CHANNELS_DEBUG")

JST = tz.gettz("Asia/Tokyo")
client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])

SRC = [s.strip() for s in os.environ["SRC_CHANNELS"].split(",")]
DEST = os.environ["DEST_CHANNEL"]

# 環境可変の運用パラメータ
CLOSE_REACTIONS = [s.strip() for s in os.environ.get("CLOSE_REACTIONS","no_entry,x,white_check_mark").split(',')]
CLOSE_KEYWORDS = [s.strip().lower() for s in os.environ.get("CLOSE_KEYWORDS", "締切,〆切,クローズ,closed,close").split(',')]


now = datetime.now(JST)
RANGE_FROM = now

# 抽出用のざっくり正規表現
EVENT_RE = re.compile(r"^■\s*イベント名\s*\n(.+)$", re.MULTILINE)
DATE_LINE_RE = re.compile(r"^■\s*日時\s*\n(.+)$", re.MULTILINE)
PLACE_RE = re.compile(r"^■\s*場所\s*\n(.+)$", re.MULTILINE)

DATE_TOKEN_RE = re.compile(
    r"""
    # 年あり: 2026/10/11 , 2026 10 11 , 2026年10月11日 , 2026.10.11
    (?P<y>\d{4})\s*(?:[./\-年\s])\s*(?P<m>\d{1,2})\s*(?:[./\-月\s])\s*(?P<d>\d{1,2})\s*(?:日)?
    |
    # 年なし: 10/11 , 10-11 , 10 11 , 10月11日
    (?P<m2>\d{1,2})\s*(?:[./\-月\s])\s*(?P<d2>\d{1,2})\s*(?:日)?
    """,
    re.VERBOSE
)

WEEKDAY_NOISE_RE = re.compile(
    r"[（(]\s*[月火水木金土日]\s*(?:曜|曜日)?\s*[)）]|[月火水木金土日]\s*(?:曜|曜日)"
)
def parse_event_date(line: str, now_jst: datetime) -> datetime | None:
    s = (line or "").strip()
    if not s:
        return None

    # ★ 全角数字/全角記号などを正規化（これが効きます）
    s = unicodedata.normalize("NFKC", s)

    # 曜日ノイズ除去
    s = WEEKDAY_NOISE_RE.sub("", s)
    s = s.replace("　", " ")
    s = re.sub(r"\s+", " ", s)

    # 時刻があれば拾う（なければ 23:59）
    tm = re.search(r"(\d{1,2}:\d{2})", s)
    hhmm = tm.group(1) if tm else "23:59"

    def build(y: int, mo: int, d: int) -> datetime | None:
        try:
            dt_str = f"{y:04d}-{mo:02d}-{d:02d} {hhmm}"
            return dateparser.parse(dt_str).replace(tzinfo=JST)
        except Exception:
            return None

    # 1) 年あり（優先）
    m = re.search(r"(\d{4})\s*[./\-\s年]\s*(\d{1,2})\s*[./\-\s月]\s*(\d{1,2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return build(y, mo, d)

    # 2) 年なし（5月3日 / 10/11 / 10 11 など）
    m = re.search(r"(\d{1,2})\s*[./\-\s月]\s*(\d{1,2})", s)
    if not m:
        return None
    mo, d = int(m.group(1)), int(m.group(2))

    # ★「未来になる方」：今年→来年で試して、未来になった方を採用
    for y in (now_jst.year, now_jst.year + 1):
        cand = build(y, mo, d)
        if cand and cand >= now_jst:
            return cand
    return None

# --- 追加: 範囲/複数日の検出 ---
RANGE_SEP_RE = re.compile(r"\s*(?:-|〜|～)\s*")
COMMA_SPLIT_RE = re.compile(r"\s*[,，、]\s*")

# --- 追加: 曜日表記（Mon/Tue...） ---
DOW_EN = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
def dow(dt: datetime) -> str:
    return DOW_EN[dt.weekday()]

# --- 追加: 一覧表示の統一フォーマット ---
def format_date_range(start: datetime, end: datetime | None) -> str:
    if end is None or end.date() == start.date():
        return f"{start.month}/{start.day}({dow(start)})"
    if start.year == end.year and start.month == end.month:
        return f"{start.month}/{start.day}({dow(start)})-{end.day}({dow(end)})"
    return f"{start.month}/{start.day}({dow(start)})-{end.month}/{end.day}({dow(end)})"

# --- 追加: 単日/期間/複数/未定をまとめて解釈 ---
def parse_event_date_info(line: str, now_jst: datetime):
    """
    return: (start_dt, end_dt, undecided)
      - undecided=True のとき start/end は None
      - 期間/複数日は start/end を返す（表示は format_date_range で統一）
    """
    s = (line or "").strip()
    if not s:
        return None, None, False

    # 未定
    if "未定" in s or "TBD" in s.upper():
        return None, None, True

    # 全角→半角など（既存と同様）
    s = unicodedata.normalize("NFKC", s)

    # 期間（ハイフン/〜/～）
    parts = RANGE_SEP_RE.split(s, maxsplit=1)
    if len(parts) == 2:
        left, right = parts[0].strip(), parts[1].strip()
        start = parse_event_date(left, now_jst)
        if not start:
            return None, None, False

        rr = WEEKDAY_NOISE_RE.sub("", right).strip()

        # 右が「日だけ」例: 5 / 5日
        m_day_only = re.fullmatch(r"(\d{1,2})\s*(?:日)?", rr)
        if m_day_only:
            end = start.replace(day=int(m_day_only.group(1)))
        else:
            end = parse_event_date(rr, now_jst)

        return start, end, False

    # 複数日（カンマ）例: 2026/5/3,4 / 5/3,4 / 5月3日,4日
    if "," in s or "，" in s or "、" in s:
        parts = COMMA_SPLIT_RE.split(s)
        left = parts[0].strip()
        start = parse_event_date(left, now_jst)
        if not start:
            return None, None, False

        last = WEEKDAY_NOISE_RE.sub("", parts[-1]).strip()
        m_day_only = re.fullmatch(r"(\d{1,2})\s*(?:日)?", last)
        if m_day_only:
            end = start.replace(day=int(m_day_only.group(1)))
        else:
            end = parse_event_date(last, now_jst)

        return start, end, False

    # 単日
    start = parse_event_date(s, now_jst)
    if not start:
        return None, None, False
    return start, None, False



def load_category_map():
    m = {}
    def add(env_key, label):
        ids = os.environ.get(env_key, "")
        for cid in [x.strip() for x in ids.split(",") if x.strip()]:
            m[cid] = label
    add("FREE_CHANNELS", "無料ライブ")
    add("PAID_CHANNELS", "チャージありライブ")
    add("OTHER_CHANNELS", "その他")
    return m

CATEGORY_BY_CHANNEL = load_category_map()
CATEGORY_ORDER = ["無料ライブ", "チャージありライブ", "その他"]

def parse_fields(text):
    # イベント名
    m = EVENT_RE.search(text)
    title = m.group(1).strip() if m else None

    # 場所
    mp = PLACE_RE.search(text)
    place = mp.group(1).strip() if mp else None

    # 日時（行全体 → 日付だけ抜く → 23:59を補う）
    md_line = DATE_LINE_RE.search(text)
    start = end = None
    undecided = False
    if md_line:
        line = md_line.group(1).strip()
        start, end, undecided = parse_event_date_info(line, now)
        print("line=", line, "=> start=", start, "end=", end, "undecided=", undecided)

    return title, place, start, end, undecided


def is_closed(parent_ts, channel):
    # 親リアクション
    rx = client.reactions_get(channel=channel, timestamp=parent_ts)
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
        res = client.conversations_history(channel=ch, cursor=res["response_metadata"]["next_cursor"], limit=200)
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
            title, place, start, end, undecided = parse_fields(text)
            if not (title and place):
                continue
            # startがある場合だけ過去を除外（未定は通す）
            if (start is not None) and (start < now):
                continue
            if is_closed(m["ts"], ch):
                continue
            # 親パーマリンク
            perma = client.chat_getPermalink(channel=ch, message_ts=m["ts"]).get("permalink")
            # チャンネル名
            info = client.conversations_info(channel=ch)
            category = CATEGORY_BY_CHANNEL.get(ch, "その他")  # 未設定ならその他扱い

            events.append({
                "ts": m["ts"],
                "channel": ch,
                "category": category,
                "title": title,
                "place": place,
                "permalink": perma,
                "start": start,           # Noneあり
                "end": end,               # Noneあり
                "undecided": undecided,   # Trueなら未定
            })

    # 日時昇順
    def sort_key(e):
        if e.get("undecided") or e.get("start") is None:
            return (1, datetime.max.replace(tzinfo=JST))
        return (0, e["start"])
    events.sort(key=sort_key)
    return events


def format_blocks(events):
    header = "📢✨*金曜配信！募集中イベント*✨📢\n気になるイベントがないかチェック👀\nイベント名リンクから募集スレッドに飛べるよ！"

    category_emoji = {
        "無料ライブ": "🆓",
        "チャージありライブ": "🎺",
        "その他": "🎈",
    }

    blocks = [{"type":"section","text":{"type":"mrkdwn","text": header}}]

    grouped = {k: [] for k in CATEGORY_ORDER}
    for e in events:
        grouped.setdefault(e["category"], []).append(e)

    for cat in CATEGORY_ORDER:
        lst = grouped.get(cat, [])
        if not lst:
            continue 

        lines = []
        for e in lst:
            title_link = f"<{e['permalink']}|{e['title']}>"
            if e.get("undecided") or e.get("start") is None:
                date_part = "未定"
            else:
                date_part = format_date_range(e["start"], e.get("end"))

            lines.append(f"• {date_part}: {title_link}（{e['place']}）")

        emoji = category_emoji.get(cat, "📌")
        text = f"*{emoji} {cat}*\n" + "\n".join(lines)
        blocks.append({"type":"section","text":{"type":"mrkdwn","text": text}})

    return blocks



def run():
    events = collect_events()

    if not events:
        print("No open events found. Skip posting.")
        return

    blocks = format_blocks(events)

    client.chat_postMessage(channel=DEST, text="週次イベントまとめ", blocks=blocks)

if __name__ == "__main__":
    run()

