#!/usr/bin/env python3
"""KOL Town local integrated demo server.

This server intentionally binds to localhost, stores only demo aliases and
hashed local PINs, and never provides real rewards, wallets, KYC, or external
conversion callbacks.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse


APP_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = APP_DIR / "data" / "kol-town.db"
SESSION_COOKIE = "kol_town_session"
SESSION_DAYS = 7
CONSENT_VERSION = "local-demo-0.4.0"
BUILD_ID = "kol-town-experience-v0.16.2-20260916"
CAMPAIGN_ID = "cmp_kol_town_demo"
CONTRACT_VERSION = "0.10.0"
PLAYABLE_ID = "plb_kol_town_demo"
PLAYABLE_CONFIG_VERSION = "0.16.2"
ALIAS_RE = re.compile(r"^[A-Za-z0-9_\-\u3400-\u9fff]{2,24}$")
PIN_RE = re.compile(r"^[0-9]{4,12}$")
ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,80}$")

ALLOWED_EVENTS = {
    "impression", "play_start", "valid_interaction", "level_start",
    "level_complete", "play_complete", "play_fail", "replay", "share",
    "cta_view", "cta_click", "registration_complete", "reward_eligible",
    "reward_claim", "consent_granted", "return_session", "error",
}
ATTRIBUTION_FIELDS = ("campaign_id", "creative_id", "channel_id", "kol_id", "link_id")
TASK_RULES = {
    "quiz": (40, 40), "predict": (35, 35), "live": (28, 30),
    "story": (25, 25), "community": (30, 30), "memorial": (22, 25),
}
BRAND_ORDER_IDS = (
    "nova-download", "nova-register", "nova-deposit", "nova-trade",
    "nova-retention", "nova-reactivate",
)
REWARD_RULES = {
    "local_demo_coffee": {"benefit": "coffee", "cost": 60, "name": "小镇咖啡体验卡"},
    "local_demo_badge": {"benefit": "badge", "cost": 90, "name": "旗舰楼纪念徽章卡"},
}
INVITE_RULES = {
    "LUNA2026": "kol_demo_luna",
    "VERA2026": "kol_demo_vera",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def json_bytes(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def pin_hash(pin: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, 240_000)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def default_state() -> dict:
    now = utc_now()
    return {
        "schema": 1,
        "accessChosen": False,
        "onboardingComplete": False,
        "theme": "luna",
        "bricks": 35,
        "totalEarned": 35,
        "prosperity": 0,
        "totalProsperity": 0,
        "level": 1,
        "streak": 0,
        "lastCheckin": None,
        "completedTasks": [],
        "taskAttempts": {},
        "buildingLevels": {key: 1 for key in ("htx", "video", "live", "quiz", "predict", "tg", "shop", "memorial")},
        "avatar": {"hair": "violet", "outfit": "mayor", "accessory": "star"},
        "walletConnected": False,
        "inviteBound": False,
        "benefits": [],
        "ledger": [{"id": new_id("led"), "label": "欢迎来到小镇", "delta": 35, "at": now}],
        "businessRevenue": 0,
        "brandReputation": 0,
        "brandOrders": {key: "available" for key in BRAND_ORDER_IDS},
        "activeBrandOrder": None,
        "growthProofs": {key: False for key in BRAND_ORDER_IDS},
        "businessLedger": [],
        "firstUpgradeShown": False,
        "firstUpgradeThisRun": False,
        "terminalEmitted": False,
        "run": 1,
        "predictionWins": 0,
        "predictionAttempts": 0,
        "memorialProgress": 0,
        "airdropTokens": 0,
        "airdropBrickCredits": 0,
        "airdropFlightsSeen": 0,
        "reducedMotion": False,
        "currentScreen": "onboarding",
        "currentBuilding": None,
        "createdAt": now,
    }


def bounded_int(value: object, minimum: int, maximum: int, fallback: int) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError):
        return fallback


def sanitize_state(candidate: object) -> dict:
    """Keep the persisted demo state bounded and free of arbitrary fields."""
    fresh = default_state()
    if not isinstance(candidate, dict) or candidate.get("schema") != 1:
        return fresh

    result = dict(fresh)
    bool_fields = (
        "accessChosen", "onboardingComplete", "walletConnected", "inviteBound",
        "firstUpgradeShown", "firstUpgradeThisRun", "terminalEmitted", "reducedMotion",
    )
    for field in bool_fields:
        result[field] = bool(candidate.get(field, fresh[field]))

    numeric_bounds = {
        "bricks": (0, 1_000_000), "totalEarned": (0, 2_000_000),
        "prosperity": (0, 10_000), "totalProsperity": (0, 2_000_000),
        "level": (1, 20_000), "streak": (0, 7), "run": (1, 100_000),
        "predictionWins": (0, 1_000_000), "predictionAttempts": (0, 1_000_000),
        "memorialProgress": (0, 100),
        "airdropTokens": (0, 999), "airdropBrickCredits": (0, 999_999),
        "airdropFlightsSeen": (0, 9_999),
        "businessRevenue": (0, 1_000_000), "brandReputation": (0, 100),
    }
    for field, (minimum, maximum) in numeric_bounds.items():
        result[field] = bounded_int(candidate.get(field), minimum, maximum, fresh[field])

    result["theme"] = candidate.get("theme") if candidate.get("theme") in {"luna", "vera"} else "luna"
    result["lastCheckin"] = str(candidate.get("lastCheckin"))[:20] if candidate.get("lastCheckin") else None
    result["completedTasks"] = list(dict.fromkeys(
        item for item in candidate.get("completedTasks", []) if item in TASK_RULES
    ))[: len(TASK_RULES)]

    attempts = candidate.get("taskAttempts", {})
    result["taskAttempts"] = {
        key: bounded_int(attempts.get(key), 0, 10_000, 0)
        for key in TASK_RULES if isinstance(attempts, dict) and key in attempts
    }

    levels = candidate.get("buildingLevels", {})
    result["buildingLevels"] = {
        key: bounded_int(levels.get(key), 1, 100, 1)
        for key in fresh["buildingLevels"]
    } if isinstance(levels, dict) else fresh["buildingLevels"]

    avatar = candidate.get("avatar", {})
    if isinstance(avatar, dict):
        result["avatar"] = {
            "hair": avatar.get("hair") if avatar.get("hair") in {"violet", "night", "mint", "rose", "cloud-pony", "amber-braid", "navy-buns", "latte-lob"} else "violet",
            "outfit": avatar.get("outfit") if avatar.get("outfit") in {"mayor", "neon", "sunset", "classic", "stage", "knit", "utility", "varsity"} else "mayor",
            "accessory": avatar.get("accessory") if avatar.get("accessory") in {"star", "headset", "glasses", "none", "moon", "crystal", "sunglasses", "beret"} else "star",
        }

    result["benefits"] = list(dict.fromkeys(
        item for item in candidate.get("benefits", []) if item in {"coffee", "badge"}
    ))[:2]

    ledger = []
    for item in candidate.get("ledger", []) if isinstance(candidate.get("ledger"), list) else []:
        if not isinstance(item, dict):
            continue
        ledger.append({
            "id": str(item.get("id") or new_id("led"))[:96],
            "label": str(item.get("label") or "本地记录")[:80],
            "delta": bounded_int(item.get("delta"), -1_000_000, 1_000_000, 0),
            "at": str(item.get("at") or utc_now())[:40],
        })
        if len(ledger) >= 24:
            break
    result["ledger"] = ledger or fresh["ledger"]

    orders = candidate.get("brandOrders", {})
    if not isinstance(orders, dict):
        orders = {}
    result["brandOrders"] = {
        key: orders.get(key) if orders.get(key) in {"available", "accepted", "settled"} else "available"
        for key in BRAND_ORDER_IDS
    }
    accepted_orders = [key for key in BRAND_ORDER_IDS if result["brandOrders"][key] == "accepted"]
    active_order = candidate.get("activeBrandOrder")
    result["activeBrandOrder"] = active_order if active_order in accepted_orders else (accepted_orders[0] if accepted_orders else None)
    for key in accepted_orders:
        if key != result["activeBrandOrder"]:
            result["brandOrders"][key] = "available"

    proofs = candidate.get("growthProofs", {})
    if not isinstance(proofs, dict):
        proofs = {}
    result["growthProofs"] = {key: bool(proofs.get(key, False)) for key in BRAND_ORDER_IDS}

    business_ledger = []
    for item in candidate.get("businessLedger", []) if isinstance(candidate.get("businessLedger"), list) else []:
        if not isinstance(item, dict):
            continue
        business_ledger.append({
            "id": str(item.get("id") or new_id("biz"))[:96],
            "label": str(item.get("label") or "本地经营记录")[:80],
            "delta": bounded_int(item.get("delta"), 0, 1_000_000, 0),
            "note": str(item.get("note") or "本地模拟")[:120],
            "at": str(item.get("at") or utc_now())[:40],
        })
        if len(business_ledger) >= 20:
            break
    result["businessLedger"] = business_ledger

    result["currentScreen"] = str(candidate.get("currentScreen") or "town")[:40]
    building = candidate.get("currentBuilding")
    result["currentBuilding"] = building if building in fresh["buildingLevels"] else None
    result["createdAt"] = str(candidate.get("createdAt") or fresh["createdAt"])[:40]
    return result


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.init_schema()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def init_schema(self) -> None:
        with self._lock, self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    alias TEXT NOT NULL,
                    alias_key TEXT NOT NULL UNIQUE,
                    pin_hash BLOB NOT NULL,
                    pin_salt BLOB NOT NULL,
                    consent_version TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'player',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    guest_id TEXT NOT NULL,
                    user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS progress (
                    principal_id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    event_name TEXT NOT NULL,
                    session_hash TEXT NOT NULL,
                    guest_id TEXT NOT NULL,
                    user_id TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attributions (
                    click_id TEXT PRIMARY KEY,
                    guest_id TEXT NOT NULL,
                    campaign_id TEXT,
                    creative_id TEXT,
                    channel_id TEXT,
                    kol_id TEXT,
                    link_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS invite_bindings (
                    principal_id TEXT PRIMARY KEY,
                    invite_code TEXT NOT NULL,
                    kol_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reward_claims (
                    claim_id TEXT PRIMARY KEY,
                    principal_id TEXT NOT NULL,
                    reward_id TEXT NOT NULL,
                    cost INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(principal_id, reward_id)
                );
                CREATE TABLE IF NOT EXISTS shares (
                    share_id TEXT PRIMARY KEY,
                    principal_id TEXT NOT NULL,
                    surface TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS server_config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_name_time ON events(event_name, created_at);
                CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
                """
            )
            db.execute(
                "INSERT OR IGNORE INTO server_config(key, value, updated_at) VALUES('kill_switch', 'off', ?)",
                (utc_now(),),
            )

    def set_config(self, key: str, value: str) -> None:
        with self._lock, self.connect() as db:
            db.execute(
                "INSERT INTO server_config(key, value, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, utc_now()),
            )

    def get_config(self, key: str, fallback: str = "") -> str:
        with self.connect() as db:
            row = db.execute("SELECT value FROM server_config WHERE key=?", (key,)).fetchone()
            return row["value"] if row else fallback

    def create_session(self) -> tuple[str, sqlite3.Row]:
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        record = {
            "token_hash": token_hash(token),
            "guest_id": new_id("gst"),
            "user_id": None,
            "created_at": now.isoformat(timespec="seconds"),
            "expires_at": (now + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds"),
        }
        with self._lock, self.connect() as db:
            db.execute(
                "INSERT INTO sessions(token_hash, guest_id, user_id, created_at, expires_at) VALUES(:token_hash,:guest_id,:user_id,:created_at,:expires_at)",
                record,
            )
            row = db.execute("SELECT * FROM sessions WHERE token_hash=?", (record["token_hash"],)).fetchone()
        return token, row

    def get_session(self, token: str | None) -> sqlite3.Row | None:
        if not token:
            return None
        hashed = token_hash(token)
        with self._lock, self.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires_at < ?", (utc_now(),))
            return db.execute("SELECT * FROM sessions WHERE token_hash=?", (hashed,)).fetchone()

    def principal(self, session: sqlite3.Row) -> str:
        return f"usr:{session['user_id']}" if session["user_id"] else f"gst:{session['guest_id']}"

    def get_user(self, user_id: str | None) -> dict | None:
        if not user_id:
            return None
        with self.connect() as db:
            row = db.execute("SELECT id, alias, role, consent_version, created_at FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def get_progress(self, principal_id: str) -> tuple[dict | None, int]:
        with self.connect() as db:
            row = db.execute("SELECT state_json, revision FROM progress WHERE principal_id=?", (principal_id,)).fetchone()
        if not row:
            return None, 0
        try:
            return sanitize_state(json.loads(row["state_json"])), int(row["revision"])
        except (json.JSONDecodeError, TypeError):
            return None, 0

    def save_progress(self, principal_id: str, state: object) -> tuple[dict, int]:
        clean = sanitize_state(state)
        with self._lock, self.connect() as db:
            db.execute(
                "INSERT INTO progress(principal_id, state_json, revision, updated_at) VALUES(?, ?, 1, ?) "
                "ON CONFLICT(principal_id) DO UPDATE SET state_json=excluded.state_json, revision=progress.revision+1, updated_at=excluded.updated_at",
                (principal_id, json.dumps(clean, ensure_ascii=False), utc_now()),
            )
            revision = db.execute("SELECT revision FROM progress WHERE principal_id=?", (principal_id,)).fetchone()["revision"]
        return clean, int(revision)

    def transfer_guest_progress(self, session: sqlite3.Row, user_id: str, client_state: object | None) -> dict:
        user_principal = f"usr:{user_id}"
        guest_principal = f"gst:{session['guest_id']}"
        user_state, _ = self.get_progress(user_principal)
        guest_state, _ = self.get_progress(guest_principal)
        chosen = user_state or guest_state or (sanitize_state(client_state) if client_state else default_state())
        chosen["accessChosen"] = True
        saved, _ = self.save_progress(user_principal, chosen)
        with self._lock, self.connect() as db:
            db.execute("DELETE FROM progress WHERE principal_id=?", (guest_principal,))
        return saved


class KolTownHandler(SimpleHTTPRequestHandler):
    server_version = "KolTownLocal/0.2"
    protocol_version = "HTTP/1.1"

    @property
    def store(self) -> Store:
        return self.server.store  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}\n")

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        super().end_headers()

    def cookie_token(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except Exception:
            return None
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def ensure_session(self) -> tuple[sqlite3.Row, str | None]:
        token = self.cookie_token()
        session = self.store.get_session(token)
        if session:
            return session, None
        new_token, session = self.store.create_session()
        return session, new_token

    def read_json(self, limit: int = 1_000_000) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid_content_length") from exc
        if length <= 0 or length > limit:
            raise ValueError("invalid_body_size")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid_json") from exc
        if not isinstance(payload, dict):
            raise ValueError("json_object_required")
        return payload

    def send_json(self, status: int, payload: object, *, token: str | None = None) -> None:
        body = json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if token:
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_DAYS * 86400}",
            )
        self.end_headers()
        self.wfile.write(body)

    def reject(self, status: int, code: str, message: str) -> None:
        self.send_json(status, {"ok": False, "error": {"code": code, "message": message}})

    def require_local_mutation(self) -> bool:
        if self.headers.get("X-KOL-Town") != "local-demo":
            self.reject(HTTPStatus.FORBIDDEN, "csrf_header_required", "缺少本地演示请求标记。")
            return False
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            expected_port = self.server.server_address[1]
            if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.port != expected_port:
                self.reject(HTTPStatus.FORBIDDEN, "origin_rejected", "仅允许当前本机端口的同源请求。")
                return False
        return True

    def bootstrap_payload(self, session: sqlite3.Row) -> dict:
        principal = self.store.principal(session)
        progress, revision = self.store.get_progress(principal)
        user = self.store.get_user(session["user_id"])
        attribution = None
        with self.store.connect() as db:
            row = db.execute(
                "SELECT click_id, campaign_id, creative_id, channel_id, kol_id, link_id, created_at "
                "FROM attributions WHERE guest_id=? ORDER BY created_at DESC LIMIT 1",
                (session["guest_id"],),
            ).fetchone()
            if row:
                attribution = dict(row)
        return {
            "ok": True,
            "mode": "LOCAL_INTEGRATED_DEMO",
            "build_id": BUILD_ID,
            "contract_version": CONTRACT_VERSION,
            "session": {"guest_id": session["guest_id"], "authenticated": bool(user)},
            "user": user,
            "progress": progress,
            "progress_revision": revision,
            "kill_switch": self.store.get_config("kill_switch", "off") == "on",
            "attribution": attribution,
            "capabilities": {
                "local_alias_auth": True,
                "server_progress": True,
                "server_events": True,
                "local_leaderboard": True,
                "local_invite_attribution": True,
                "server_reward_validation": True,
                "local_kol_chat": True,
                "chat_raw_text_retained": False,
                "real_wallet": False,
                "real_kyc": False,
                "external_cta": False,
                "real_reward": False,
            },
        }

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/health":
            self.send_json(HTTPStatus.OK, {
                "ok": True,
                "service": "kol-town-local",
                "build_id": BUILD_ID,
                "database": "sqlite",
                "kill_switch": self.store.get_config("kill_switch", "off") == "on",
            })
            return
        if path == "/api/bootstrap":
            session, token = self.ensure_session()
            self.send_json(HTTPStatus.OK, self.bootstrap_payload(session), token=token)
            return
        if path == "/api/leaderboard":
            session, token = self.ensure_session()
            self.send_json(HTTPStatus.OK, self.leaderboard_payload(session), token=token)
            return
        if path.startswith("/api/"):
            self.reject(HTTPStatus.NOT_FOUND, "not_found", "本地 API 不存在。")
            return
        self.serve_static(path)

    def serve_static(self, path: str) -> None:
        if path in {"", "/"}:
            path = "/index.html"
        relative = unquote(path).lstrip("/")
        target = (APP_DIR / relative).resolve()
        if APP_DIR not in target.parents and target != APP_DIR:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not target.is_file() or target.name in {"server.py"} or "data" in target.parts:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        mime, _ = mimetypes.guess_type(target.name)
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", (mime or "application/octet-stream") + ("; charset=utf-8" if mime and (mime.startswith("text/") or mime in {"application/javascript", "application/json"}) else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache" if target.suffix in {".html", ".js", ".css", ".json", ".yaml"} else "public, max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        self.handle_mutation("POST")

    def do_PUT(self) -> None:
        self.handle_mutation("PUT")

    def do_DELETE(self) -> None:
        self.handle_mutation("DELETE")

    def handle_mutation(self, method: str) -> None:
        if not self.require_local_mutation():
            return
        path = urlparse(self.path).path
        session, token = self.ensure_session()
        try:
            payload = {} if method == "DELETE" else self.read_json()
            if path == "/api/auth/register" and method == "POST":
                result = self.register(session, payload)
            elif path == "/api/auth/login" and method == "POST":
                result = self.login(session, payload)
            elif path == "/api/auth/logout" and method == "POST":
                result = self.logout(session)
            elif path == "/api/auth/account" and method == "DELETE":
                result = self.delete_account(session)
            elif path == "/api/progress" and method == "PUT":
                result = self.save_client_progress(session, payload)
            elif path == "/api/events" and method == "POST":
                result = self.record_event(session, payload)
            elif path == "/api/attribution/click" and method == "POST":
                result = self.record_attribution(session, payload)
            elif path == "/api/invite/bind" and method == "POST":
                result = self.bind_invite(session, payload)
            elif path == "/api/rewards/redeem" and method == "POST":
                result = self.redeem_reward(session, payload)
            elif path == "/api/share" and method == "POST":
                result = self.record_share(session, payload)
            elif path == "/api/chat" and method == "POST":
                result = self.kol_chat(session, payload)
            else:
                self.reject(HTTPStatus.NOT_FOUND, "not_found", "本地 API 不存在。")
                return
            status, body = result
            self.send_json(status, body, token=token)
        except ValueError as exc:
            self.reject(HTTPStatus.BAD_REQUEST, str(exc), "请求内容不符合本地演示规则。")
        except sqlite3.IntegrityError:
            self.reject(HTTPStatus.CONFLICT, "conflict", "该操作已完成或数据发生冲突。")
        except Exception as exc:
            self.log_error("Unhandled API error: %r", exc)
            self.reject(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", "本地服务处理失败。")

    def register(self, session: sqlite3.Row, payload: dict) -> tuple[int, dict]:
        alias = str(payload.get("alias", "")).strip()
        pin = str(payload.get("pin", ""))
        consent = payload.get("consent") is True
        if not ALIAS_RE.fullmatch(alias):
            raise ValueError("invalid_alias")
        if not PIN_RE.fullmatch(pin):
            raise ValueError("invalid_pin")
        if not consent:
            raise ValueError("consent_required")
        salt = secrets.token_bytes(16)
        user_id = new_id("usr")
        now = utc_now()
        with self.store._lock, self.store.connect() as db:
            db.execute(
                "INSERT INTO users(id,alias,alias_key,pin_hash,pin_salt,consent_version,role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (user_id, alias, alias.casefold(), pin_hash(pin, salt), salt, CONSENT_VERSION, "player", now, now),
            )
            db.execute("UPDATE sessions SET user_id=? WHERE token_hash=?", (user_id, session["token_hash"]))
        progress = self.store.transfer_guest_progress(session, user_id, payload.get("progress"))
        user = self.store.get_user(user_id)
        return HTTPStatus.CREATED, {
            "ok": True, "confirmation_source": "local_demo_server", "user": user,
            "progress": progress, "consent_version": CONSENT_VERSION,
        }

    def login(self, session: sqlite3.Row, payload: dict) -> tuple[int, dict]:
        alias = str(payload.get("alias", "")).strip()
        pin = str(payload.get("pin", ""))
        if not ALIAS_RE.fullmatch(alias) or not PIN_RE.fullmatch(pin):
            raise ValueError("invalid_credentials")
        with self.store._lock, self.store.connect() as db:
            row = db.execute("SELECT * FROM users WHERE alias_key=?", (alias.casefold(),)).fetchone()
            if not row or not hmac.compare_digest(bytes(row["pin_hash"]), pin_hash(pin, bytes(row["pin_salt"]))):
                return HTTPStatus.UNAUTHORIZED, {"ok": False, "error": {"code": "invalid_credentials", "message": "别名或本地 PIN 不正确。"}}
            db.execute("UPDATE sessions SET user_id=? WHERE token_hash=?", (row["id"], session["token_hash"]))
        progress = self.store.transfer_guest_progress(session, row["id"], payload.get("progress"))
        return HTTPStatus.OK, {"ok": True, "user": self.store.get_user(row["id"]), "progress": progress}

    def logout(self, session: sqlite3.Row) -> tuple[int, dict]:
        with self.store._lock, self.store.connect() as db:
            db.execute("UPDATE sessions SET user_id=NULL WHERE token_hash=?", (session["token_hash"],))
        guest_principal = f"gst:{session['guest_id']}"
        progress, _ = self.store.save_progress(guest_principal, default_state())
        return HTTPStatus.OK, {"ok": True, "user": None, "progress": progress}

    def delete_account(self, session: sqlite3.Row) -> tuple[int, dict]:
        user_id = session["user_id"]
        if not user_id:
            return HTTPStatus.UNAUTHORIZED, {"ok": False, "error": {"code": "auth_required", "message": "当前不是本地账号。"}}
        user_principal = f"usr:{user_id}"
        guest_principal = f"gst:{session['guest_id']}"
        with self.store._lock, self.store.connect() as db:
            db.execute("DELETE FROM invite_bindings WHERE principal_id=?", (user_principal,))
            db.execute("DELETE FROM reward_claims WHERE principal_id=?", (user_principal,))
            db.execute("DELETE FROM shares WHERE principal_id=?", (user_principal,))
            db.execute("DELETE FROM events WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM attributions WHERE guest_id=?", (session["guest_id"],))
            db.execute("DELETE FROM progress WHERE principal_id IN (?,?)", (user_principal, guest_principal))
            db.execute("DELETE FROM users WHERE id=?", (user_id,))
            db.execute("UPDATE sessions SET user_id=NULL WHERE token_hash=?", (session["token_hash"],))
        progress, _ = self.store.save_progress(guest_principal, default_state())
        return HTTPStatus.OK, {"ok": True, "deleted": True, "progress": progress}

    def save_client_progress(self, session: sqlite3.Row, payload: dict) -> tuple[int, dict]:
        if "state" not in payload:
            raise ValueError("state_required")
        progress, revision = self.store.save_progress(self.store.principal(session), payload["state"])
        return HTTPStatus.OK, {"ok": True, "progress": progress, "revision": revision, "saved_at": utc_now()}

    def record_event(self, session: sqlite3.Row, payload: dict) -> tuple[int, dict]:
        event = payload.get("event")
        if not isinstance(event, dict):
            raise ValueError("event_required")
        event_id = str(event.get("event_id", ""))
        event_name = str(event.get("event_name", ""))
        if not ID_RE.fullmatch(event_id) or event_name not in ALLOWED_EVENTS:
            raise ValueError("invalid_event")
        safe_event = {
            key: event.get(key) for key in (
                "event_id", "event_name", "event_time", "campaign_id", "contract_version",
                "playable_id", "playable_config_version", "build_id", "creative_id",
                "channel_id", "kol_id", "link_id", "session_id", "screen_id", "task_id",
                "building_id", "locale", "region", "device_class", "consent_state", "properties",
            )
        }
        raw = json.dumps(safe_event, ensure_ascii=False)
        if len(raw.encode("utf-8")) > 64_000:
            raise ValueError("event_too_large")
        try:
            with self.store._lock, self.store.connect() as db:
                db.execute(
                    "INSERT INTO events(event_id,event_name,session_hash,guest_id,user_id,payload_json,created_at) VALUES(?,?,?,?,?,?,?)",
                    (event_id, event_name, session["token_hash"], session["guest_id"], session["user_id"], raw, utc_now()),
                )
            return HTTPStatus.ACCEPTED, {"ok": True, "accepted": True, "event_id": event_id}
        except sqlite3.IntegrityError:
            return HTTPStatus.OK, {"ok": True, "accepted": False, "duplicate": True, "event_id": event_id}

    def record_attribution(self, session: sqlite3.Row, payload: dict) -> tuple[int, dict]:
        values = {}
        for field in ATTRIBUTION_FIELDS:
            value = payload.get(field)
            values[field] = str(value)[:80] if value is not None else None
        if values["campaign_id"] not in {None, "", CAMPAIGN_ID}:
            raise ValueError("campaign_mismatch")
        click_id = new_id("clk")
        with self.store._lock, self.store.connect() as db:
            db.execute(
                "INSERT INTO attributions(click_id,guest_id,campaign_id,creative_id,channel_id,kol_id,link_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (click_id, session["guest_id"], values["campaign_id"] or CAMPAIGN_ID, values["creative_id"], values["channel_id"], values["kol_id"], values["link_id"], utc_now()),
            )
        return HTTPStatus.CREATED, {"ok": True, "click_id": click_id, "model": "local_demo_last_click", **values}

    def bind_invite(self, session: sqlite3.Row, payload: dict) -> tuple[int, dict]:
        code = str(payload.get("code", "")).strip().upper()
        kol_id = INVITE_RULES.get(code)
        if not kol_id:
            raise ValueError("invalid_invite_code")
        principal = self.store.principal(session)
        with self.store._lock, self.store.connect() as db:
            existing = db.execute("SELECT invite_code, kol_id FROM invite_bindings WHERE principal_id=?", (principal,)).fetchone()
            if existing:
                if existing["invite_code"] != code:
                    return HTTPStatus.CONFLICT, {"ok": False, "error": {"code": "invite_already_bound", "message": "当前身份已绑定其他演示邀请码。"}}
            else:
                db.execute(
                    "INSERT INTO invite_bindings(principal_id,invite_code,kol_id,created_at) VALUES(?,?,?,?)",
                    (principal, code, kol_id, utc_now()),
                )
        progress, _ = self.store.get_progress(principal)
        progress = progress or default_state()
        progress["inviteBound"] = True
        saved, revision = self.store.save_progress(principal, progress)
        return HTTPStatus.OK, {"ok": True, "invite_code": code, "kol_id": kol_id, "progress": saved, "revision": revision}

    def redeem_reward(self, session: sqlite3.Row, payload: dict) -> tuple[int, dict]:
        reward_id = str(payload.get("reward_id", ""))
        rule = REWARD_RULES.get(reward_id)
        if not rule:
            raise ValueError("unknown_reward")
        principal = self.store.principal(session)
        progress, _ = self.store.get_progress(principal)
        if not progress:
            raise ValueError("progress_required")
        if rule["benefit"] in progress.get("benefits", []):
            return HTTPStatus.OK, {"ok": True, "already_claimed": True, "claim_status": "local_demo_card", "progress": progress}
        if int(progress.get("bricks", 0)) < int(rule["cost"]):
            return HTTPStatus.CONFLICT, {"ok": False, "error": {"code": "insufficient_bricks", "message": "服务器记录的虚拟砖块不足。"}}
        progress["bricks"] -= int(rule["cost"])
        progress.setdefault("benefits", []).append(rule["benefit"])
        progress.setdefault("ledger", []).insert(0, {
            "id": new_id("led"), "label": f"加入演示卡包 · {rule['name']}",
            "delta": -int(rule["cost"]), "at": utc_now(),
        })
        progress["ledger"] = progress["ledger"][:24]
        saved, revision = self.store.save_progress(principal, progress)
        with self.store._lock, self.store.connect() as db:
            db.execute(
                "INSERT INTO reward_claims(claim_id,principal_id,reward_id,cost,status,created_at) VALUES(?,?,?,?,?,?)",
                (new_id("clm"), principal, reward_id, rule["cost"], "local_demo_card", utc_now()),
            )
        return HTTPStatus.OK, {
            "ok": True, "claim_status": "local_demo_card", "fulfillment": "none",
            "progress": saved, "revision": revision,
        }

    def record_share(self, session: sqlite3.Row, payload: dict) -> tuple[int, dict]:
        surface = str(payload.get("surface", "local_preview"))[:40]
        status = str(payload.get("status", "intent"))[:40]
        share_id = new_id("shr")
        with self.store._lock, self.store.connect() as db:
            db.execute(
                "INSERT INTO shares(share_id,principal_id,surface,status,created_at) VALUES(?,?,?,?,?)",
                (share_id, self.store.principal(session), surface, status, utc_now()),
            )
        return HTTPStatus.ACCEPTED, {"ok": True, "share_id": share_id, "external_share": False}

    def kol_chat(self, session: sqlite3.Row, payload: dict) -> tuple[int, dict]:
        """Return a bounded local KOL-avatar reply without retaining raw chat text."""
        message = re.sub(r"\s+", " ", str(payload.get("message", ""))).strip()
        if not message:
            raise ValueError("chat_message_required")
        if len(message) > 240:
            raise ValueError("chat_message_too_long")

        progress, _ = self.store.get_progress(self.store.principal(session))
        progress = progress or default_state()
        theme = "vera" if progress.get("theme") == "vera" else "luna"
        persona_name = "Vera AI 分身" if theme == "vera" else "Luna AI 分身"
        completed = [str(item) for item in progress.get("completedTasks", [])]
        task_names = {
            "quiz": "小镇知识挑战", "predict": "限时方向预测", "live": "直播广场签到",
            "story": "街区剧情", "community": "社群徽记收集", "memorial": "纪念广场共建",
        }
        next_task = next((task_id for task_id in task_names if task_id not in completed), "quiz")
        lowered = message.casefold()

        def mentions(*terms: str) -> bool:
            return any(term.casefold() in lowered for term in terms)

        blocked = False
        action = None
        if mentions("助记词", "私钥", "private key", "seed phrase", "身份证", "政府证件", "kyc 文件", "密码"):
            intent = "sensitive_data"
            blocked = True
            reply = "为了安全，请不要发送助记词、私钥、密码、身份证或 KYC 文件。我不会接收、验证或保存这些信息。"
        elif mentions("稳赚", "保证收益", "投资建议", "买币", "卖币", "交易建议", "提现", "真实代币", "行情会涨", "行情会跌"):
            intent = "financial_boundary"
            blocked = True
            reply = "我只能介绍小镇玩法，不能提供投资、交易或收益建议。预测馆也是本地娱乐任务，不代表真实行情。"
        elif mentions("你是谁", "真人", "本人", "分身", "人工智能", "ai"):
            intent = "identity"
            reply = f"我是 {persona_name}，使用本地规则根据你的小镇进度回答，不是 KOL 本人实时在线，也不会替本人作出商业承诺。"
        elif mentions("进度", "砖块", "繁荣", "等级", "完成了", "成绩"):
            intent = "progress"
            reply = f"你现在有 {int(progress.get('bricks', 0))} 块虚拟砖块、{int(progress.get('prosperity', 0))} 繁荣度，完成了 {len(completed)} 个不同任务。再完成 {max(0, 3 - len(completed))} 个任务并达到 100 繁荣度，就能完成本轮目标。"
            action = {"type": "open_screen", "target": "task_center", "label": "查看任务进度"}
        elif mentions("邀请码", "luna2026", "vera2026", "邀请"):
            intent = "invite"
            reply = "演示邀请码只用于本机 KOL 归因和无价值演示卡，不会创建真实品牌账号，也不参与商业结算。你可以去旗舰楼查看和绑定。"
            action = {"type": "open_screen", "target": "flagship", "label": "去旗舰楼"}
        elif mentions("答题", "知识学院"):
            intent = "task_quiz"
            reply = "答题学院有 3 道小镇规则题，每题都会即时反馈，完成可获得 40 虚拟砖块和 40 繁荣度。"
            action = {"type": "open_task", "target": "quiz", "label": "开始答题"}
        elif mentions("直播", "直播广场"):
            intent = "task_live"
            reply = "直播广场是本地模拟互动：完成 3 次互动后签到，不会打开真实直播或收集联系方式。"
            action = {"type": "open_task", "target": "live", "label": "去直播广场"}
        elif mentions("预测", "预测馆", "行情"):
            intent = "task_predict"
            reply = "预测馆是 8 秒娱乐方向判断，只影响小镇任务进度，不构成投资建议，也不连接真实行情。"
            action = {"type": "open_task", "target": "predict", "label": "体验预测任务"}
        elif mentions("任务", "今天", "做什么", "推荐", "去哪"):
            intent = "task_recommendation"
            reply = f"建议先做“{task_names[next_task]}”。这是你尚未完成的任务，完成后会增加虚拟砖块和繁荣度。"
            action = {"type": "open_task", "target": next_task, "label": f"去做{task_names[next_task]}"}
        elif mentions("怎么玩", "规则", "小镇", "建筑"):
            intent = "game_help"
            reply = "玩法很简单：打开带“任务可玩”气泡的建筑，完成 3 个不同任务并达到 100 繁荣度。砖块可以升级建筑或加入无价值演示卡包。"
            action = {"type": "open_screen", "target": "task_center", "label": "查看全部任务"}
        elif mentions("你好", "嗨", "hello", "hi", "早上好", "晚上好"):
            intent = "greeting"
            reply = f"你好呀，我是 {persona_name}。我可以介绍小镇、查看你的进度，或者推荐下一项任务。"
        else:
            intent = "fallback"
            reply = "这版分身目前最擅长回答小镇玩法、任务、进度和邀请码。你可以换个方式问我，例如“我今天做什么？”或“我的进度怎么样？”。"

        return HTTPStatus.OK, {
            "ok": True,
            "message_id": new_id("msg"),
            "persona": {"name": persona_name, "mode": "local_rules", "real_person_live": False},
            "intent": intent,
            "reply": reply,
            "blocked": blocked,
            "action": action,
            "quick_prompts": ["我今天做什么？", "我的进度怎么样？", "小镇怎么玩？", "邀请码是什么？"],
            "privacy": {"raw_text_retained": False, "retention": "request_only", "remote_model": False},
            "disclosure": "本地规则回复 · 非本人实时发言",
        }

    def leaderboard_payload(self, session: sqlite3.Row) -> dict:
        total_rows = []
        with self.store.connect() as db:
            for row in db.execute(
                "SELECT u.id, u.alias, p.state_json, p.updated_at FROM users u "
                "LEFT JOIN progress p ON p.principal_id='usr:' || u.id ORDER BY p.updated_at DESC"
            ).fetchall():
                try:
                    state = sanitize_state(json.loads(row["state_json"])) if row["state_json"] else default_state()
                except (json.JSONDecodeError, TypeError):
                    state = default_state()
                total_rows.append({
                    "id": row["id"], "name": row["alias"],
                    "score": int(state["totalProsperity"]) + len(state["completedTasks"]) * 20,
                    "weekly_score": int(state["prosperity"]) + len(state["completedTasks"]) * 20,
                    "is_me": row["id"] == session["user_id"],
                })
            invite_rows = [dict(row) for row in db.execute(
                "SELECT kol_id AS name, COUNT(*) AS score FROM invite_bindings GROUP BY kol_id ORDER BY score DESC, kol_id ASC"
            ).fetchall()]
        total_rows.sort(key=lambda item: (-item["score"], item["name"].casefold()))
        weekly_rows = sorted(total_rows, key=lambda item: (-item["weekly_score"], item["name"].casefold()))
        return {
            "ok": True,
            "source": "local_sqlite_server",
            "total": [{**row, "rank": index + 1} for index, row in enumerate(total_rows[:50])],
            "weekly": [{**row, "score": row["weekly_score"], "rank": index + 1} for index, row in enumerate(weekly_rows[:50])],
            "invite": [{**row, "rank": index + 1} for index, row in enumerate(invite_rows[:50])],
            "note": "仅统计本机演示账号与本地邀请码绑定，不代表公开或商业排名。",
        }


class KolTownServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], store: Store):
        super().__init__(address, KolTownHandler)
        self.store = store


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="KOL Town local integrated demo server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host; localhost is strongly recommended")
    parser.add_argument("--port", type=int, default=4173)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--set-kill-switch", choices=("on", "off"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    store = Store(args.db.resolve())
    if args.set_kill_switch:
        store.set_config("kill_switch", args.set_kill_switch)
        print(f"kill_switch={args.set_kill_switch} db={store.path}")
        return 0
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print("Refusing non-local bind for this demo server.", file=sys.stderr)
        return 2
    server = KolTownServer((args.host, args.port), store)
    print(f"KOL Town local integrated demo: http://{args.host}:{args.port}/")
    print(f"SQLite: {store.path}")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
