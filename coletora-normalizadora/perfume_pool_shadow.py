#!/usr/bin/env python3
"""Perfume opportunity pool — SHADOW MODE.

Why this exists
---------------
The live collector is a streaming pipeline: it reads the tap, delivers up to
MAX_PROMOS_PER_RUN (3), advances its cursor to EOF and forgets everything else.
Combined with MAX_PROMO_AGE_SECONDS (90min) this means the 10h burst of dozens of
offers is spent on 3 posts and the rest is destroyed. At 16h, when Breno needs a
post, the choice set is whatever landed in the last 90 minutes -- often 1-2 items.
Best-of-1 is worst-of-1.

This module accumulates every eligible offer into a persistent, scored inventory
so publication can later pick the best of the DAY instead of the best of the last
four minutes.

SHADOW MODE CONTRACT (important)
--------------------------------
- Reads the tap read-only, from the tail, by timestamp window.
- NEVER touches perfume_deliver's STATE_FILE / cursor / seen_ids.
- NEVER sends anything to WhatsApp.
- Only writes its own pool file + its own report.
Running this cannot affect production delivery. It exists to measure how much
value the current window discards, before any cutover is considered.

Usage
-----
  python3 perfume_pool_shadow.py --minutes 240        # ingest recent window
  python3 perfume_pool_shadow.py --report             # what the pool holds now
  python3 perfume_pool_shadow.py --peek 5             # what it WOULD publish
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time
from collections import deque
from pathlib import Path

import perfume_deliver as p

POOL_DIR = Path.home() / ".hermes" / "perfume_image_bank" / "pool"
POOL_FILE = POOL_DIR / "pool.jsonl"
REPORT_FILE = POOL_DIR / "pool_shadow_report.json"
SESSION_FILE = POOL_DIR / "session.json"

# An offer is worth remembering far longer than the live 90min window, but not
# forever: prices and stock rot. 10h covers a full working day of promos.
POOL_TTL_SECONDS = 10 * 60 * 60
# Score decay: full value while fresh, then a steady haircut so a stale bargain
# loses to a fresh comparable one without being deleted outright.
DECAY_GRACE_SECONDS = 2 * 60 * 60
DECAY_PER_HOUR = 0.15
DECAY_FLOOR = 0.35


# --------------------------------------------------------------------------
# session boundary
#
# TTL alone is not enough: a 10h TTL lets a 22h offer stay alive until 08h the
# next morning, so the pool would start the day already holding yesterday's
# leftovers. The session starts when the automation actually starts collecting,
# NOT at midnight -- Breno works in shifts and has no use for offers scraped
# while he was asleep. So a gap in collection (machine off, timer paused, or
# simply overnight) ends the session, and the next run opens a fresh one.
# --------------------------------------------------------------------------
# Any silence longer than this means the automation was not running: whatever
# happened before it belongs to a previous session.
SESSION_GAP_SECONDS = 90 * 60


def load_session_start(touch: bool = False) -> float:
    """Epoch of the current session. A gap in real promo activity starts a new one.

    The heartbeat is deliberately "when an offer was last collected", not "when
    the timer last ran": the timer fires all night, so run-based timing would
    never break the session. Source groups go quiet overnight, so promo activity
    is the honest signal for when a working shift begins and ends.
    """
    now = time.time()
    started = 0.0
    last_seen = 0.0
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        started = float(data.get("started_at") or 0)
        last_seen = float(data.get("last_offer_at") or started)
    except Exception:
        pass

    if started and (now - last_seen) <= SESSION_GAP_SECONDS:
        if touch:
            _touch_session(started, now)
        return started
    # No session yet, or promos stopped flowing long enough: new shift.
    return start_session(now)


def _touch_session(started: float, when: float) -> None:
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(
        json.dumps({
            "started_at": int(started),
            "started_human": dt.datetime.fromtimestamp(started).isoformat(timespec="seconds"),
            "last_offer_at": int(when),
        }, ensure_ascii=False),
        encoding="utf-8",
    )


def start_session(when: float | None = None) -> float:
    when = time.time() if when is None else when
    _touch_session(float(int(when)), when)
    return float(int(when))


# --------------------------------------------------------------------------
# tap reading (read-only, never advances production cursor)
# --------------------------------------------------------------------------
def recent_source_messages(minutes: int, max_lines: int = 20000) -> list[dict]:
    if not os.path.exists(p.TAP_FILE):
        return []
    lines = deque(maxlen=max_lines)
    with open(p.TAP_FILE, encoding="utf-8", errors="ignore") as f:
        for line in f:
            lines.append(line)
    now = time.time()
    rows: list[dict] = []
    for line in lines:
        try:
            msg = json.loads(line)
        except Exception:
            continue
        chat_id = str(msg.get("chatId") or "").split("@")[0]
        ts = p._message_epoch_seconds(msg.get("timestamp"))
        if chat_id not in p.PERFUME_GROUPS or ts is None:
            continue
        if not (0 <= now - ts <= minutes * 60):
            continue
        body = str(msg.get("body") or "").strip()
        if not body:
            continue
        rows.append({
            "messageId": msg.get("messageId"),
            "chatId": chat_id,
            "groupName": p.PERFUME_GROUPS[chat_id],
            "body": body,
            "hasMedia": bool(msg.get("hasMedia")),
            "mediaPath": p.message_media_path(msg),
            "timestamp": ts,
        })
    return rows


# --------------------------------------------------------------------------
# pool persistence
# --------------------------------------------------------------------------
def load_pool() -> dict[str, dict]:
    pool: dict[str, dict] = {}
    if not POOL_FILE.exists():
        return pool
    for line in POOL_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        key = row.get("pool_key")
        if key:
            pool[key] = row
    return pool


def save_pool(pool: dict[str, dict]) -> None:
    POOL_DIR.mkdir(parents=True, exist_ok=True)
    tmp = POOL_FILE.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for row in pool.values():
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(POOL_FILE)


def prune_pool(pool: dict[str, dict], now: float | None = None) -> tuple[dict[str, dict], int]:
    """Drop consumed, expired, and pre-session entries.

    The session cut is what guarantees "today only": yesterday's leftovers are
    removed even if their TTL has not run out yet.
    """
    now = time.time() if now is None else now
    session_start = load_session_start()
    kept = {
        k: r for k, r in pool.items()
        if now - float(r.get("first_seen") or 0) <= POOL_TTL_SECONDS
        and float(r.get("first_seen") or 0) >= session_start
        and not r.get("consumed")
    }
    return kept, len(pool) - len(kept)


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------
def decay_factor(first_seen: float | None, now: float | None = None) -> float:
    """Age penalty. Fresh offers keep full score; old ones fade, never vanish."""
    now = time.time() if now is None else now
    age = max(0.0, now - float(first_seen or now))
    if age <= DECAY_GRACE_SECONDS:
        return 1.0
    hours_over = (age - DECAY_GRACE_SECONDS) / 3600.0
    return max(DECAY_FLOOR, 1.0 - DECAY_PER_HOUR * hours_over)


# --------------------------------------------------------------------------
# (B) price-history bonus
#
# The live pipeline already applies history in validated_opportunity_score, but
# that runs AFTER validation. The ranking that decides WHICH offers deserve the
# validation budget (annotate_candidate) is history-blind, so a genuine
# all-time-low can be ranked below a loud-but-average offer and never make the
# shortlist. In a pool that must pick the best of hundreds, "is this actually
# cheap for this SKU?" is the strongest available signal, so it is applied here.
# --------------------------------------------------------------------------
def history_bonus(product: str, price_num: float | None) -> tuple[float, str]:
    if not product or not price_num:
        return 0.0, ""
    try:
        history = p.recent_product_prices(p.canonical_product_key(product))
    except Exception:
        return 0.0, ""
    if not history:
        return 0.0, ""
    ordered = sorted(history)
    median = ordered[len(ordered) // 2]
    previous_min = ordered[0]
    if price_num < previous_min * 0.98:
        return 20.0, f"menor histórico ({len(history)} obs)"
    if price_num <= median * 0.90:
        return 12.0, f"abaixo da mediana ({len(history)} obs)"
    if price_num > median * 1.10:
        return -10.0, f"acima da mediana ({len(history)} obs)"
    return 0.0, f"na mediana ({len(history)} obs)"


def effective_score(row: dict, now: float | None = None) -> float:
    base = float(row.get("base_score") or 0) + float(row.get("history_bonus") or 0)
    return round(base * decay_factor(row.get("first_seen"), now), 2)


def pool_key_for(body: str) -> str:
    """One canonical SKU = one pool entry, regardless of source/link/price."""
    keys = p.normalize_offer_keys(body or "")
    for key in keys:
        if key.startswith("product:"):
            return key
    return keys[0] if keys else ""


# --------------------------------------------------------------------------
# ingestion
# --------------------------------------------------------------------------
def ingest(minutes: int, verbose: bool = False) -> dict:
    rows = recent_source_messages(minutes)
    pool = load_pool()
    pool, pruned = prune_pool(pool)
    now = time.time()
    session_start = load_session_start(touch=False)
    pre_session = sum(1 for r in rows if float(r.get("timestamp") or 0) < session_start)
    rows = [r for r in rows if float(r.get("timestamp") or 0) >= session_start]

    stats = {
        "session_start": dt.datetime.fromtimestamp(session_start).isoformat(timespec="seconds"),
        "read": len(rows),
        "skipped_pre_session": pre_session,
        "rejected_category": 0,
        "no_price": 0,
        "consolidated": 0,
        "new": 0,
        "improved_media": 0,
        "improved_price": 0,
        "history_scored": 0,
    }

    for row in rows:
        body = row["body"]
        product = p.extract_product_name(body)
        ok_cat, cat_reason = p.is_fragrance_candidate(product, body)
        if not ok_cat:
            stats["rejected_category"] += 1
            continue
        price = p.extract_price(body)
        price_num = p.parse_price_number(price)
        key = pool_key_for(body)
        if not key:
            stats["no_price"] += 1
            continue

        annotated = p.annotate_candidate(row)
        base = float(annotated.get("candidate_score") or 0)
        hbonus, hreason = history_bonus(product, price_num)

        existing = pool.get(key)
        if existing is None:
            pool[key] = {
                "pool_key": key,
                "product": product,
                "price": price,
                "price_num": price_num,
                "link": p.extract_link(body),
                "body": body,
                "media_path": row.get("mediaPath") or "",
                "bank_image": annotated.get("bank_image") or "",
                "bank_source": annotated.get("bank_source") or "",
                "has_context": bool(annotated.get("has_context")),
                "base_score": base,
                "history_bonus": hbonus,
                "history_reason": hreason,
                "score_reasons": annotated.get("score_reasons") or [],
                "first_seen": row["timestamp"],
                "last_seen": row["timestamp"],
                "sources": [row["groupName"]],
                "message_ids": [row["messageId"]],
                "consumed": False,
            }
            stats["new"] += 1
            if hbonus > 0:
                stats["history_scored"] += 1
            continue

        # Same SKU seen again: consolidate instead of discarding. The live
        # pipeline drops these as duplicate_skip and can lose the better copy.
        stats["consolidated"] += 1
        existing["last_seen"] = max(existing.get("last_seen") or 0, row["timestamp"])
        if row["groupName"] not in existing.get("sources", []):
            existing.setdefault("sources", []).append(row["groupName"])
        existing.setdefault("message_ids", []).append(row["messageId"])
        # keep the cheapest evidenced price
        if price_num and (not existing.get("price_num") or price_num < existing["price_num"]):
            existing.update({
                "price": price, "price_num": price_num, "body": body,
                "history_bonus": hbonus, "history_reason": hreason,
            })
            stats["improved_price"] += 1
        # keep a real source photo if we only had a text-only post
        if row.get("mediaPath") and not existing.get("media_path"):
            existing["media_path"] = row["mediaPath"]
            stats["improved_media"] += 1
        existing["base_score"] = max(existing.get("base_score") or 0, base)

    # Backfill: entries stored before the history bonus existed must be scored
    # too, otherwise the ranking silently favours whatever was ingested last.
    for entry in pool.values():
        if "history_reason" not in entry:
            hb, hr = history_bonus(entry.get("product") or "", entry.get("price_num"))
            entry["history_bonus"] = hb
            entry["history_reason"] = hr
            stats["backfilled"] = stats.get("backfilled", 0) + 1

    save_pool(pool)
    # Heartbeat: only real collected offers keep the session alive. The timer
    # runs overnight too, so marking every run would never let a shift end.
    if rows:
        newest = max(float(r.get("timestamp") or 0) for r in rows)
        _touch_session(session_start, newest)
    stats["pruned"] = pruned
    stats["pool_size"] = len(pool)
    stats["generated_at"] = int(now)
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    return stats


# --------------------------------------------------------------------------
# read-side: what would we publish right now
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# (A) revalidation at consumption time
#
# The whole point of a pool is that an offer collected at 10h can be published at
# 16h. But its PRICE was observed at 10h and may be dead by then -- and Breno's
# rule is that we never publish an unverified price. So freshness of the price is
# decoupled from freshness of the collection: the offer waits in the pool cheaply,
# and the expensive marketplace check happens only for the one item about to be
# published. If it fails, the entry is dropped and the next best is tried.
# This is what makes a day-long inventory safe rather than a liability.
# --------------------------------------------------------------------------
def revalidate(row: dict) -> tuple[bool, str, dict]:
    """Re-check identity/availability/price NOW. Returns (ok, reason, patch)."""
    body = row.get("body") or ""
    link = row.get("link") or p.extract_link(body)
    if not link:
        return False, "sem link", {}
    candidate = {
        "body": body,
        "product": row.get("product") or "",
        "price": row.get("price"),
    }
    try:
        checked = p.validate_shortlist_candidate(candidate)
    except Exception as exc:
        return False, f"erro validação: {type(exc).__name__}", {}
    if not checked.get("validation_ok"):
        return False, str(checked.get("validation_reason") or "não validado"), {}

    page_price = p.parse_price_number(checked.get("validated_price"))
    pooled_price = row.get("price_num")
    patch = {
        "revalidated_at": int(time.time()),
        "revalidated_price": page_price,
        "validated_product": checked.get("validated_product") or "",
    }
    if page_price and pooled_price:
        drift = (page_price - pooled_price) / pooled_price
        patch["price_drift_pct"] = round(drift * 100, 1)
        # Cheaper than pooled is a bonus, not a problem. More expensive means the
        # promo we stored no longer exists at that price -- never publish it.
        if drift > 0.02:
            return False, f"preço subiu {drift*100:.0f}% (R${pooled_price:.2f}→R${page_price:.2f})", patch
    return True, "ok", patch


def next_best(limit: int = 1, dry_run: bool = True) -> list[dict]:
    """Pick the best pool entries that still survive a live re-check."""
    pool = load_pool()
    picked: list[dict] = []
    checked_count = 0
    for row in ranked_pool():
        if len(picked) >= limit:
            break
        checked_count += 1
        if checked_count > 12:  # bound the validation budget per call
            break
        ok, reason, patch = revalidate(row)
        entry = pool.get(row.get("pool_key") or "")
        if entry is not None:
            entry.update(patch)
            entry["last_revalidation"] = reason
        if not ok:
            print(f"   ✗ descartado: {row.get('product')} — {reason}")
            if entry is not None and not dry_run:
                entry["consumed"] = True
                entry["consumed_reason"] = reason
            continue
        row.update(patch)
        picked.append(row)
        if entry is not None and not dry_run:
            entry["consumed"] = True
            entry["consumed_reason"] = "published"
    save_pool(pool)
    return picked


def ranked_pool(now: float | None = None) -> list[dict]:
    pool, _ = prune_pool(load_pool(), now)
    rows = list(pool.values())
    for r in rows:
        r["effective_score"] = effective_score(r, now)
    rows.sort(key=lambda r: (r["effective_score"], bool(r.get("bank_image")), r.get("last_seen") or 0), reverse=True)
    return rows


def peek(limit: int) -> list[dict]:
    """Best-of-pool with brand diversity, mirroring select_diverse_batch intent."""
    out: list[dict] = []
    seen_brands: set[str] = set()
    for row in ranked_pool():
        brand = (str(row.get("product") or "").split() or ["?"])[0].lower()
        if brand in seen_brands and len(out) < limit:
            continue
        out.append(row)
        seen_brands.add(brand)
        if len(out) >= limit:
            break
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=0, help="ingest window in minutes")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--peek", type=int, default=0)
    ap.add_argument("--next", type=int, default=0,
                    help="best-of-pool WITH live price revalidation (dry-run: nothing is sent)")
    ap.add_argument("--start-session", action="store_true",
                    help="reset the pool to start collecting from NOW (drops earlier offers)")
    args = ap.parse_args()

    if args.start_session:
        started = start_session()
        pool = load_pool()
        pool, dropped = prune_pool(pool)
        save_pool(pool)
        print(f"sessão iniciada: {dt.datetime.fromtimestamp(started):%d/%m %H:%M}")
        print(f"ofertas anteriores descartadas: {dropped} | pool agora: {len(pool)}")
        return

    if args.minutes:
        stats = ingest(args.minutes)
        print(json.dumps(stats, ensure_ascii=False, indent=2))

    if args.report:
        rows = ranked_pool()
        session_start = load_session_start()
        with_photo = sum(1 for r in rows if r.get("bank_image") or r.get("media_path"))
        print(f"sessão desde: {dt.datetime.fromtimestamp(session_start):%d/%m %H:%M}")
        print(f"pool: {len(rows)} ofertas vivas | com foto: {with_photo}")
        if rows:
            ages = [(time.time() - float(r.get("first_seen") or 0)) / 3600 for r in rows]
            print(f"idade: {min(ages):.1f}h - {max(ages):.1f}h")

    if args.peek:
        for i, r in enumerate(peek(args.peek), 1):
            age_h = (time.time() - float(r.get("first_seen") or 0)) / 3600
            photo = "foto✅" if (r.get("bank_image") or r.get("media_path")) else "foto❌"
            hist = r.get("history_reason") or "sem histórico"
            print(f"{i}. [{r['effective_score']:.0f}] {r.get('product')} — {r.get('price')} "
                  f"| {age_h:.1f}h | {photo} | {len(r.get('sources') or [])} grupo(s) | {hist}")

    if args.next:
        print(f"revalidando ao vivo (dry-run, nada é enviado)...")
        picked = next_best(args.next, dry_run=True)
        print()
        if not picked:
            print("nenhuma oferta sobreviveu à revalidação")
        for i, r in enumerate(picked, 1):
            age_h = (time.time() - float(r.get("first_seen") or 0)) / 3600
            drift = r.get("price_drift_pct")
            dtxt = f" | drift {drift:+.1f}%" if drift is not None else ""
            print(f"{i}. ✅ [{r['effective_score']:.0f}] {r.get('product')} — {r.get('price')} "
                  f"| {age_h:.1f}h | preço ML R${r.get('revalidated_price')}{dtxt}")


if __name__ == "__main__":
    main()
