#!/usr/bin/env python3
"""Stream Polymarket 1X2 football prices to an append-only CSV.

Uses Gamma API for market discovery and the public CLOB market WebSocket for
live top-of-book/last trade updates. No API key is required.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import signal
import sys
import threading
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
import websocket

GAMMA = "https://gamma-api.polymarket.com"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
LEAGUES = {
    "epl": ("英超", ("premier league", "english premier league", "epl")),
    "lal": ("西甲", ("la liga", "laliga", "spanish la liga")),
    "bun": ("德甲", ("bundesliga", "german bundesliga")),
}
CSV_FIELDS = [
    "比赛时间", "主客双方", "联赛", "联赛轮次", "主队胜率", "客队胜率", "平局概率",
]


def configure_console() -> None:
    """Use UTF-8 for console and redirected output.

    Windows consoles default to a legacy code page (for example cp936). When
    stdout/stderr are redirected to a file or another program, Python would
    encode with that code page, which may raise UnicodeEncodeError on characters
    outside it. Reconfiguring to UTF-8 with replacement keeps logs readable.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue


def resolve_output_path(value: str) -> Path:
    """Resolve --output, anchoring relative paths at the script directory.

    The macOS launcher changed into the project directory before starting, while
    on Windows the script may be started from Explorer or another directory.
    Anchoring keeps the CSV next to the script in both cases.
    """
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    return path


def read_windows_proxy() -> str | None:
    """Return the WinINET (Clash/V2Ray etc.) system proxy as a URL.

    Python does not read the Windows "Internet Settings" proxy by itself, and
    the Polymarket WebSocket host is unreachable without it on some networks.
    """
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled = int(winreg.QueryValueEx(key, "ProxyEnable")[0])
            server = str(winreg.QueryValueEx(key, "ProxyServer")[0] or "").strip()
    except (OSError, ValueError, TypeError):
        return None
    if not enabled or not server:
        return None
    entries: dict[str, str] = {}
    if "=" in server:
        for part in server.split(";"):
            name, _, value = part.partition("=")
            if value.strip():
                entries[name.strip().lower()] = value.strip()
    else:
        entries["http"] = server
    for scheme in ("https", "http"):
        if scheme in entries:
            return f"http://{entries[scheme]}"
    return None


def environment_proxy() -> str | None:
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy"):
        value = os.environ.get(name)
        if value and value.strip():
            value = value.strip()
            return value if "://" in value else f"http://{value}"
    return None


def resolve_proxy(explicit: str | None, disabled: bool) -> str | None:
    """Pick the proxy: --proxy, then Windows system settings, then env vars."""
    if disabled:
        return None
    candidate = explicit or read_windows_proxy() or environment_proxy()
    if not candidate:
        return None
    candidate = candidate.strip()
    return candidate if "://" in candidate else f"http://{candidate}"


def proxy_host_port(proxy: str | None) -> tuple[str, int] | None:
    """Split an HTTP proxy URL into the host/port pair websocket-client wants."""
    if not proxy:
        return None
    parsed = urlsplit(proxy)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        # websocket-client needs the PySocks extra for SOCKS proxies.
        logging.warning("暂不支持 %s 代理，WebSocket 将直连", parsed.scheme or proxy)
        return None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.hostname, port


def as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            return []
    return []


def league_for(event: dict[str, Any]) -> str | None:
    forced = event.get("_league_code")
    if forced in LEAGUES:
        return str(forced)
    # Prefer the event's series metadata; fall back to title/slug only when needed.
    text = " ".join(str(event.get(k) or "") for k in ("title", "slug", "seriesSlug", "series_slug")).lower()
    tags = event.get("tags") or []
    if isinstance(tags, list):
        text += " " + " ".join(str(t.get("label") or t.get("slug") or "") if isinstance(t, dict) else str(t) for t in tags)
    for code, (_, needles) in LEAGUES.items():
        if any(n in text for n in needles):
            return code
    # Match event slugs commonly use prefixes such as epl-ars-bur-YYYY-MM-DD.
    slug = str(event.get("slug") or "").lower()
    for code in LEAGUES:
        if slug.startswith(code + "-"):
            return code
    # Soccer series/league abbreviations can appear as event metadata.
    abbreviation = str(event.get("seriesAbbreviation") or event.get("league") or "").lower()
    return abbreviation if abbreviation in LEAGUES else None


def is_1x2(market: dict[str, Any]) -> bool:
    outcomes = [str(x).strip().lower() for x in as_list(market.get("outcomes"))]
    question = str(market.get("question") or "").lower()
    if len(outcomes) != 3:
        return False
    has_draw = any(x in {"draw", "tie"} for x in outcomes)
    # Market names often format the 1X2 result as “Will X win ...?” per outcome;
    # a three-outcome market containing a draw is the clearest generic signal.
    return has_draw or ("draw" in question and len(outcomes) == 3)


def moneyline_binary(market: dict[str, Any]) -> bool:
    market_type = str(market.get("sportsMarketType") or market.get("sports_market_type") or "").lower()
    outcomes = [str(x).strip().lower() for x in as_list(market.get("outcomes"))]
    return market_type == "moneyline" and outcomes == ["yes", "no"]


def moneyline_side(question: str, event_title: str, outcome: str = "") -> str | None:
    if re.search(r"\bdraw\b|\btie\b", question, re.IGNORECASE) or str(outcome).strip().casefold() in {"draw", "tie"}:
        return "draw"
    winner = re.search(r"Will\s+(.+?)\s+win\b", question, re.IGNORECASE)
    if winner:
        team = winner.group(1).strip().casefold()
    else:
        team = str(outcome).strip().casefold()
    if team in {"home", "home win", "home team"}:
        return "home"
    if team in {"away", "away win", "away team"}:
        return "away"
    participants = re.split(r"\s+vs\.?\s+|\s+v\.?\s+", event_title, maxsplit=1, flags=re.IGNORECASE)
    if len(participants) == 2:
        home, away = (team_name.strip().casefold() for team_name in participants)
        if team and (team == home or team in home or home in team):
            return "home"
        if team and (team == away or team in away or away in team):
            return "away"
    return None


def event_time(event: dict[str, Any], market: dict[str, Any] | None = None) -> datetime:
    # Sports market metadata exposes gameStartTime; prefer it because it tracks
    # postponements more directly than a generic event date.
    candidates = [
        (market or {}).get("gameStartTime"),
        event.get("startTime"),
        event.get("eventDate"),
        (market or {}).get("startTime"),
        (market or {}).get("eventDate"),
    ]
    for value in candidates:
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            continue
    return datetime.max.replace(tzinfo=timezone.utc)


def round_for(event: dict[str, Any]) -> tuple[str, str]:
    for key in ("round", "matchday", "matchWeek", "matchweek", "eventWeek", "weekNumber", "week", "roundName", "roundNumber"):
        value = event.get(key)
        if value not in (None, ""):
            raw = str(value).strip()
            found = re.search(r"\d+", raw)
            if found:
                number = int(found.group())
                return f"round:{number}", str(number)
            # Keep a stable grouping key, but emit a blank numeric round label.
            return f"round:{raw.casefold()}", ""
    description = " ".join(str(event.get(k) or "") for k in ("description", "title"))
    match = re.search(r"\b(match\s*week|matchday|round)\s*[:#-]?\s*(\d+)\b", description, re.IGNORECASE)
    if match:
        number = int(match.group(2))
        return f"round:{number}", str(number)

    # Gamma does not consistently expose a matchday. Use the kickoff's ISO week
    # as a fallback value for the informational round column.
    kickoff = event_time(event)
    if kickoff != datetime.max.replace(tzinfo=timezone.utc):
        iso = kickoff.isocalendar()
        return f"week:{iso.year:04d}-{iso.week:02d}", str(iso.week)
    return "round:unknown", ""


def select_matches_next_20_days(metadata: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Keep matches kicking off between now and 20 days from now, inclusive."""
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=20)
    match_tokens: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for token, item in metadata.items():
        match_tokens.setdefault(item["match_key"], []).append((token, item))
    selected_tokens: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {label: 0 for label, _ in LEAGUES.values()}
    missing_time = 0
    for entries in match_tokens.values():
        dated_entries = [(item["kickoff"], item.get("kickoff_from_market", False))
                         for _, item in entries
                         if item["kickoff"] != datetime.max.replace(tzinfo=timezone.utc)]
        market_times = [value for value, from_market in dated_entries if from_market]
        times = market_times or [value for value, _ in dated_entries]
        if not times:
            missing_time += 1
            continue
        # All 1X2 legs should share a kickoff. If the API temporarily disagrees,
        # prefer the latest value to account for a postponement.
        kickoff = max(times)
        if now <= kickoff <= cutoff:
            for token, item in entries:
                selected_item = dict(item)
                selected_item["kickoff"] = kickoff
                selected_tokens[token] = selected_item
            counts[entries[0][1]["league"]] += 1
    for league_name, count in counts.items():
        logging.info("%s：未来 20 天内开球且识别到 1X2 市场的比赛 %d 场", league_name, count)
    if missing_time:
        logging.warning("%d 场 1X2 比赛未提供可识别的开球时间，已跳过", missing_time)
    return selected_tokens


def format_local_kickoff(value: datetime) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


def discover_events(session: requests.Session) -> list[dict[str, Any]]:
    """Discover soccer events by league series from Gamma's sports metadata."""
    events: list[dict[str, Any]] = []
    logging.info("正在从 Gamma API 获取联赛和赛事信息")
    response = session.get(f"{GAMMA}/sports", timeout=30)
    response.raise_for_status()
    sports = response.json()
    if not isinstance(sports, list):
        raise ValueError("Gamma /sports did not return a list")

    series_to_league: dict[str, str] = {}
    for sport in sports:
        if not isinstance(sport, dict):
            continue
        sport_key = str(sport.get("sport") or sport.get("slug") or "").strip().lower()
        league_code = next((code for code, (_, names) in LEAGUES.items()
                            if sport_key == code or any(name == sport_key for name in names)), None)
        if not league_code:
            continue
        # Gamma's sports metadata publishes series IDs as a comma-separated string.
        series_ids = sport.get("series") or sport.get("seriesIds") or sport.get("series_ids") or ""
        if isinstance(series_ids, list):
            ids = series_ids
        else:
            ids = str(series_ids).split(",")
        for series_id in ids:
            if str(series_id).strip():
                series_to_league[str(series_id).strip()] = league_code

    logging.info("找到 %d 个英超/西甲/德甲赛事系列，开始读取比赛列表", len(series_to_league))

    for series_id, league_code in series_to_league.items():
        offset = 0
        logging.info("正在读取 %s 赛事系列 %s", LEAGUES[league_code][0], series_id)
        while True:
            # Include inactive but not-yet-closed events too: filtering to active
            # events can hide an adjacent matchday and leave only one round.
            params = {"series_id": series_id, "closed": "false", "limit": 500, "offset": offset}
            response = session.get(f"{GAMMA}/events", params=params, timeout=30)
            response.raise_for_status()
            batch = response.json()
            if not isinstance(batch, list) or not batch:
                break
            for event in batch:
                if isinstance(event, dict):
                    event["_league_code"] = league_code
                    events.append(event)
            if len(batch) < 500:
                break
            offset += len(batch)
    return events


def parse_markets(events: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    assets: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for event in events:
        league_code = league_for(event)
        if not league_code:
            continue
        event_title = str(event.get("title") or event.get("slug") or "")
        round_key, round_name = round_for(event)
        match_key = f"{league_code}:{event.get('id') or event.get('slug') or event_title}"
        event_markets = event.get("markets") or []
        found_1x2 = False
        for market in event_markets:
            if not isinstance(market, dict):
                continue
            market_kickoff = event_time(event, market)
            outcomes = as_list(market.get("outcomes"))
            token_ids = as_list(market.get("clobTokenIds") or market.get("clob_token_ids"))
            if len(token_ids) != len(outcomes):
                continue
            question = str(market.get("question") or "")
            # Soccer moneyline events are normally three binary YES/NO markets
            # (home win, draw, away win), not one market with three outcomes.
            if moneyline_binary(market):
                yes_index = next(i for i, value in enumerate(outcomes) if str(value).strip().casefold() == "yes")
                token_id = str(token_ids[yes_index])
                side = moneyline_side(question, event_title)
                if side is None:
                    continue
                assets[token_id] = {
                    "league": LEAGUES[league_code][0], "event": event_title,
                    "round": round_name, "round_key": round_key, "kickoff": market_kickoff,
                    "kickoff_from_market": bool(market.get("gameStartTime")),
                    "match_key": match_key, "side": side,
                    "condition_id": market.get("conditionId") or market.get("condition_id"),
                }
                found_1x2 = True
                continue
            if not is_1x2(market):
                continue
            found_1x2 = True
            for outcome, token_id in zip(outcomes, token_ids):
                token_id = str(token_id)
                side = moneyline_side(question, event_title, str(outcome))
                if side is None:
                    continue
                assets[token_id] = {
                    "league": LEAGUES[league_code][0], "event": event_title,
                    "round": round_name, "round_key": round_key, "kickoff": market_kickoff,
                    "kickoff_from_market": bool(market.get("gameStartTime")),
                    "match_key": match_key, "side": side,
                    "condition_id": market.get("conditionId") or market.get("condition_id"),
                }
        if not found_1x2:
            missing.append(event_title)
    return assets, missing


class CsvWriter:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.new_file = not path.exists() or path.stat().st_size == 0
        if not self.new_file:
            with path.open("r", newline="", encoding="utf-8-sig") as existing:
                old_header = next(csv.reader(existing), [])
            if old_header != CSV_FIELDS:
                backup_base = path.with_name(f"{path.stem}_old_format_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
                backup = backup_base.with_suffix(path.suffix)
                counter = 1
                while backup.exists():
                    backup = backup_base.with_name(f"{backup_base.name}_{counter}").with_suffix(path.suffix)
                    counter += 1
                try:
                    os.replace(path, backup)
                except OSError as exc:
                    # Common on Windows when the CSV is open in Excel or an
                    # antivirus scanner briefly holds the file.
                    raise RuntimeError(
                        f"无法备份旧格式 CSV：{path}。请关闭占用该文件的程序（例如 Excel）后重试：{exc}"
                    ) from exc
                logging.warning("CSV 表头已更新；旧格式文件已备份至 %s", backup)
                self.new_file = True
        self.rows: list[dict[str, Any]] = []
        self.seen_rows: set[tuple[str, ...]] = set()
        self.write_failed = False
        if self.new_file:
            with path.open("w", newline="", encoding="utf-8-sig") as output:
                csv.DictWriter(output, fieldnames=CSV_FIELDS).writeheader()

    @staticmethod
    def _sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
        league_order = {"德甲": 0, "英超": 1, "西甲": 2}
        return (league_order.get(str(row.get("联赛") or ""), 99),
                str(row.get("比赛时间") or ""),
                str(row.get("主客双方") or ""),
                str(row.get("主队胜率") or ""), str(row.get("客队胜率") or ""),
                str(row.get("平局概率") or ""))

    def write(self, row: dict[str, Any]) -> bool:
        with self.lock:
            signature = tuple("" if row.get(field) is None else str(row.get(field)) for field in CSV_FIELDS)
            if signature in self.seen_rows:
                return False
            self.seen_rows.add(signature)
            self.rows.append(row)
            return self._flush_locked()

    def _flush_locked(self) -> bool:
        """Rewrite the CSV; the caller must already hold self.lock.

        Windows keeps a share lock on files opened by Excel and similar
        programs, so a failed write is reported once and retried on the next
        update instead of aborting the collector.
        """
        ordered = sorted(self.rows, key=self._sort_key)
        try:
            with self.path.open("w", newline="", encoding="utf-8-sig") as output:
                writer = csv.DictWriter(output, fieldnames=CSV_FIELDS)
                writer.writeheader()
                writer.writerows(ordered)
        except OSError as exc:
            if not self.write_failed:
                logging.warning("写入 CSV 失败（文件可能被其他程序占用，例如 Excel），将自动重试：%s", exc)
                self.write_failed = True
            return False
        if self.write_failed:
            logging.info("CSV 写入已恢复正常：%s", self.path)
            self.write_failed = False
        return True

    def close(self) -> None:
        with self.lock:
            if self.rows:
                self._flush_locked()


def price_fields(state: dict[str, Any]) -> tuple[Any, Any, Any]:
    bid, ask = state.get("best_bid"), state.get("best_ask")
    mid = (float(bid) + float(ask)) / 2 if bid is not None and ask is not None else None
    return bid, ask, mid


def timestamped_csv_path(requested_path: Path) -> Path:
    """Insert a local-time generation timestamp before the CSV extension."""
    suffix = requested_path.suffix or ".csv"
    stem = requested_path.stem if requested_path.suffix else requested_path.name
    timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    candidate = requested_path.with_name(f"{stem}_{timestamp}{suffix}")
    counter = 1
    while candidate.exists():
        candidate = requested_path.with_name(f"{stem}_{timestamp}_{counter}{suffix}")
        counter += 1
    return candidate


def main() -> int:
    parser = argparse.ArgumentParser(description="将 Polymarket 英超/西甲/德甲 1X2 实时行情写入 CSV")
    parser.add_argument("--output", default="polymarket_odds.csv", help="输出 CSV 路径")
    parser.add_argument("--snapshot-seconds", type=float, default=0, help="按固定间隔写入全部当前赔率；0 表示只在行情变动时写入")
    parser.add_argument("--status-seconds", type=float, default=60, help="状态日志间隔秒数（默认 60）")
    parser.add_argument("--discovery-only", action="store_true", help="只列出可识别的市场，不连接 WebSocket")
    parser.add_argument("--proxy", default=None, help="HTTP(S) 代理，例如 http://127.0.0.1:7897；默认自动读取 Windows 系统代理")
    parser.add_argument("--no-proxy", action="store_true", help="忽略系统代理，全部直连")
    args = parser.parse_args()
    if args.status_seconds <= 0:
        parser.error("--status-seconds 必须大于 0")
    if args.proxy and args.no_proxy:
        parser.error("--proxy 与 --no-proxy 不能同时使用")

    configure_console()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    proxy = resolve_proxy(args.proxy, args.no_proxy)
    if proxy:
        logging.info("使用代理 %s（可用 --no-proxy 关闭）", proxy)
    session = requests.Session()
    session.headers.update({"User-Agent": "polymarket-football-csv/1.0"})
    request_proxy = proxy
    if request_proxy:
        session.proxies.update({"http": request_proxy, "https": request_proxy})
    try:
        events = discover_events(session)
    except requests.RequestException as exc:
        if request_proxy:
            logging.warning("通过代理访问 Gamma API 失败（%s），改为直连重试", exc)
            session.proxies.clear()
            request_proxy = None
            try:
                events = discover_events(session)
            except requests.RequestException as direct_exc:
                logging.error("Gamma API 市场发现失败: %s", direct_exc)
                return 1
        else:
            logging.error("Gamma API 市场发现失败: %s", exc)
            return 1
    event_count = len(events)
    all_metadata, _ = parse_markets(events)
    metadata = select_matches_next_20_days(all_metadata)
    logging.info("赛事发现 %d 场；全量识别 %d 个 1X2 token，按未来 20 天开球时间筛选后保留 %d 个",
                 event_count, len(all_metadata), len(metadata))
    if not metadata:
        logging.warning("没有发现匹配的 1X2 市场。Gamma 的标签/标题可能变化，或当前尚无相关市场。")
    logging.info("发现 %d 个结果 token（英超/西甲/德甲 1X2）", len(metadata))
    if args.discovery_only:
        for token, item in metadata.items():
            print(f"{item['league']}\t{format_local_kickoff(item['kickoff'])}\t{item['event']}\t{item['side']}\t{token}")
        return 0
    if not metadata:
        logging.error("没有可订阅 token，退出。")
        return 2

    output_path = timestamped_csv_path(resolve_output_path(args.output))
    writer = CsvWriter(output_path)
    logging.info("本次 CSV 文件：%s", writer.path)
    states: dict[str, dict[str, Any]] = {token: {} for token in metadata}
    stop = threading.Event()
    stats_lock = threading.Lock()
    stats: dict[str, Any] = {
        "connected": False,
        "connected_at": None,
        "last_message_at": None,
        "messages": 0,
        "rows_written": 0,
    }

    def record(token: str) -> None:
        info, state = metadata[token], states[token]
        bid, ask, mid = price_fields(state)
        last = state.get("last_trade_price")
        indicative = mid if mid is not None else (float(last) if last is not None else None)
        if indicative is None:
            return
        state["probability"] = indicative
        # A row represents a complete 1X2 snapshot. Wait until each of the
        # three independent Yes contracts has supplied a price.
        event_states = {
            item["side"]: states[asset].get("probability")
            for asset, item in metadata.items()
            if item["match_key"] == info["match_key"]
        }
        if any(event_states.get(side) is None for side in ("home", "away", "draw")):
            return
        wrote = writer.write({
            "比赛时间": format_local_kickoff(info["kickoff"]),
            "主客双方": info["event"], "联赛": info["league"], "联赛轮次": info["round"],
            "主队胜率": event_states.get("home"), "客队胜率": event_states.get("away"),
            "平局概率": event_states.get("draw"),
        })
        if wrote:
            with stats_lock:
                stats["rows_written"] += 1

    def on_message(_ws: websocket.WebSocketApp, raw: str) -> None:
        with stats_lock:
            stats["messages"] += 1
            stats["last_message_at"] = datetime.now(timezone.utc)
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return
        messages = decoded if isinstance(decoded, list) else [decoded]
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            kind = msg.get("event_type") or msg.get("type")
            if kind == "book":
                token = str(msg.get("asset_id") or msg.get("payload", {}).get("assetId") or "")
                if token in metadata:
                    payload = msg.get("payload") or msg
                    bids, asks = payload.get("bids") or [], payload.get("asks") or []
                    states[token]["best_bid"] = max((float(x["price"]) for x in bids), default=None)
                    states[token]["best_ask"] = min((float(x["price"]) for x in asks), default=None)
                    record(token)
            elif kind == "best_bid_ask":
                token = str(msg.get("asset_id") or msg.get("payload", {}).get("assetId") or "")
                if token in metadata:
                    payload = msg.get("payload") or msg
                    states[token]["best_bid"] = float(payload["best_bid"]) if payload.get("best_bid") else None
                    states[token]["best_ask"] = float(payload["best_ask"]) if payload.get("best_ask") else None
                    record(token)
            elif kind == "price_change":
                payload = msg.get("payload") or msg
                for change in payload.get("price_changes") or payload.get("priceChanges") or []:
                    token = str(change.get("asset_id") or change.get("assetId") or "")
                    if token not in metadata:
                        continue
                    if change.get("best_bid") is not None or change.get("bestBid") is not None:
                        val = change.get("best_bid", change.get("bestBid"))
                        states[token]["best_bid"] = float(val) if val else None
                    if change.get("best_ask") is not None or change.get("bestAsk") is not None:
                        val = change.get("best_ask", change.get("bestAsk"))
                        states[token]["best_ask"] = float(val) if val else None
                    record(token)
            elif kind == "last_trade_price":
                payload = msg.get("payload") or msg
                token = str(payload.get("asset_id") or payload.get("assetId") or "")
                if token in metadata and payload.get("price") is not None:
                    states[token]["last_trade_price"] = float(payload["price"])
                    record(token)

    def on_open(ws: websocket.WebSocketApp) -> None:
        ws.send(json.dumps({"assets_ids": list(metadata), "type": "market", "custom_feature_enabled": True}))
        ws_state["opened"] = True
        with stats_lock:
            stats["connected"] = True
            stats["connected_at"] = datetime.now(timezone.utc)
        logging.info("WebSocket 已连接并订阅 %d 个结果", len(metadata))

    def on_error(_ws: websocket.WebSocketApp, error: Any) -> None:
        logging.warning("WebSocket 错误: %s", error)

    def on_close(_ws: websocket.WebSocketApp, code: Any, message: Any) -> None:
        with stats_lock:
            stats["connected"] = False
        logging.warning("WebSocket 已断开 (%s %s)，将自动重连", code, message)

    def report_status() -> None:
        while not stop.wait(args.status_seconds):
            now = datetime.now(timezone.utc)
            with stats_lock:
                connected = stats["connected"]
                messages = stats["messages"]
                rows_written = stats["rows_written"]
                last_message = stats["last_message_at"]
                connected_at = stats["connected_at"]
            if last_message:
                idle_seconds = int((now - last_message).total_seconds())
                age_text = f"最近收到数据 {idle_seconds} 秒前"
            elif connected_at:
                idle_seconds = int((now - connected_at).total_seconds())
                age_text = f"连接后尚未收到行情（已等待 {idle_seconds} 秒）"
            else:
                idle_seconds = None
                age_text = "尚未收到行情"
            message = (
                f"运行状态：WebSocket={'已连接' if connected else '未连接'}；{age_text}；"
                f"累计收到消息 {messages} 条；新增 CSV 行 {rows_written} 行；文件 {writer.path}"
            )
            if connected and idle_seconds is not None and idle_seconds >= args.status_seconds:
                logging.warning(message + "。长时间无消息，请检查网络或市场是否有行情变化。")
            else:
                logging.info(message)

    def snapshots() -> None:
        while not stop.wait(max(args.snapshot_seconds, 0.1)):
            for token in list(metadata):
                record(token)

    snapshot_thread = None
    if args.snapshot_seconds > 0:
        snapshot_thread = threading.Thread(target=snapshots, daemon=True)
        snapshot_thread.start()
    status_thread = threading.Thread(target=report_status, daemon=True)
    status_thread.start()

    # Run the WebSocket in a worker thread and keep the main thread waiting on
    # a short timeout. On Windows a main thread blocked in run_forever cannot
    # dispatch the Ctrl-C handler, so the wait has to wake up regularly.
    active_ws: dict[str, Any] = {"app": None}
    ws_proxy = proxy_host_port(proxy)
    # Some networks only reach the CLOB host through the proxy, while others
    # only work direct, so fall back to the other mode after a failed attempt.
    ws_modes: list[tuple[str, int] | None] = [ws_proxy] if ws_proxy else [None]
    if ws_proxy:
        ws_modes.append(None)
    ws_state = {"mode": 0, "opened": False}

    def stream() -> None:
        while not stop.is_set():
            mode = ws_modes[ws_state["mode"] % len(ws_modes)]
            ws_state["opened"] = False
            try:
                app = websocket.WebSocketApp(
                    WS_URL, on_open=on_open, on_message=on_message,
                    on_error=on_error, on_close=on_close,
                )
                active_ws["app"] = app
                app.run_forever(
                    ping_interval=10, ping_timeout=5,
                    http_proxy_host=mode[0] if mode else None,
                    http_proxy_port=mode[1] if mode else None,
                    # Without an explicit no_proxy entry websocket-client would
                    # fall back to the http(s)_proxy environment variables.
                    http_no_proxy=None if mode else ["*"],
                    proxy_type="http" if mode else None,
                )
            except Exception as exc:
                logging.warning("连接异常: %s", exc)
            finally:
                active_ws["app"] = None
            if stop.is_set():
                break
            if not ws_state["opened"]:
                # The last attempt never opened; try the alternate network path.
                ws_state["mode"] += 1
            stop.wait(3)

    def handle_signal(_sig: int, _frame: Any) -> None:
        stop.set()

    handled_signals = [signal.SIGINT]
    for name in ("SIGTERM", "SIGBREAK"):  # SIGBREAK exists only on Windows.
        value = getattr(signal, name, None)
        if value is not None:
            handled_signals.append(value)
    for sig in handled_signals:
        try:
            signal.signal(sig, handle_signal)
        except (OSError, ValueError):
            continue

    socket_thread = threading.Thread(target=stream, name="clob-websocket", daemon=True)
    socket_thread.start()
    logging.info("采集已启动；按 Ctrl-C 停止并保存 CSV")
    try:
        while not stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        stop.set()
    finally:
        stop.set()
        app = active_ws.get("app")
        if app is not None:
            try:
                app.close()
            except Exception as exc:
                logging.debug("关闭 WebSocket 时出错: %s", exc)
        socket_thread.join(timeout=5)
        writer.close()
        logging.info("CSV 已保存至 %s", writer.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
