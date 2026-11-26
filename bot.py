import asyncio
import logging
import os
import random
import re
import sqlite3
import time
from decimal import Decimal

import discord
from aiohttp import web
from discord import app_commands

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("economy-bot")

# ===== 設定 =====
CURRENCY = "￥"  # 通貨記号(金額の前に付く)
WORK_COOLDOWN = 60 * 60  # 1時間
WORK_MIN, WORK_MAX = 1000, 3000
MAX_ITEMS = 25  # セレクトメニュー/埋め込みの上限に合わせる

# レベル機能
XP_PER_MESSAGE = 1  # 1メッセージで貰えるXP
XP_PER_LEVEL = 100  # 1レベルあがるのに必要なXP
XP_TO_MONEY = 50  # 1XP = ￥50
XP_CONVERT_COOLDOWN = 2 * 60 * 60  # /xp-convert のクールダウン(2時間)

# 借金
MAX_DEBT = 10 * 10**9  # 借金の上限(10B)。利息を含めた借金の合計がこれを超えない
DEBT_INTEREST_PERCENT = 10  # 利息(%)
DEBT_INTEREST_PERIOD = 3 * 60 * 60  # 利息がつく間隔(3時間)
MAX_LEVEL_ROLES = 25  # ロール報酬の最大数
DB_PATH = os.environ.get("DB_PATH", "economy.db")
DEV_GUILD_ID = os.environ.get("DEV_GUILD_ID")  # 指定するとそのサーバーに即時反映
# 会社(/company)
COMPANY_BONUS_PERCENT = 10  # 会社に入っている人が /work で貰えるお金への上乗せ(%)
COMPANY_INVITE_TTL = 24 * 60 * 60  # 招待の有効期限(24時間)
COMPANY_NAME_MIN, COMPANY_NAME_MAX = 2, 20
COMPANY_NAME_FORBIDDEN = "@<>`*_~|\\"  # メンションや装飾に使われる文字は会社名に使えない

# 固定メッセージ(/pin)
STICKY_DELAY = 3  # 新しいメッセージが来てから、固定メッセージを一番下に出し直すまでの秒数(連投時のAPI制限対策)
STICKY_FOOTER = "📌 固定メッセージ"
STICKY_MAX_LENGTH = 4000

# Botのステータス(「◯◯をプレイ中」の◯◯)。環境変数 STATUS_TEXT で変更できます
STATUS_TEXT = os.environ.get("STATUS_TEXT", "製作者：ぼく！")

# 職業(ランクが高いほど /work の報酬が高い)。解放レベルは /level のレベル。
# 無職のときは WORK_MIN〜WORK_MAX。ここを書き換えれば給料や解放レベルを調整できます。
JOBS = [
    {"id": 1, "name": "配達員",       "emoji": "🚚", "level": 0,  "min": 1500,  "max": 3500,  "msg": "荷物を配達して"},
    {"id": 2, "name": "カフェ店員",   "emoji": "☕", "level": 3,  "min": 2500,  "max": 5000,  "msg": "カフェで接客をして"},
    {"id": 3, "name": "事務員",       "emoji": "🗂️", "level": 8,  "min": 4000,  "max": 7500,  "msg": "オフィスで事務作業をして"},
    {"id": 4, "name": "エンジニア",   "emoji": "💻", "level": 15, "min": 6500,  "max": 11000, "msg": "システムを開発して"},
    {"id": 5, "name": "医者",         "emoji": "🩺", "level": 25, "min": 10000, "max": 17000, "msg": "患者を診察して"},
    {"id": 6, "name": "弁護士",       "emoji": "⚖️", "level": 35, "min": 15000, "max": 26000, "msg": "裁判で弁護をして"},
    {"id": 7, "name": "社長",         "emoji": "👔", "level": 50, "min": 24000, "max": 45000, "msg": "会社を経営して"},
]
JOBS_BY_ID = {j["id"]: j for j in JOBS}

WORK_MESSAGES = [
    "コンビニで夜勤をして",
    "配達のアルバイトをして",
    "エンジニアとして働いて",
    "畑仕事を手伝って",
    "イベントの設営をして",
    "カフェで接客をして",
    "引っ越しの手伝いをして",
    "ブログの記事を書いて",
]


MONEY_UNITS = ((10**12, "T"), (10**9, "B"), (10**6, "M"), (10**3, "K"))
MONEY_MULTIPLIERS = {suffix.lower(): unit for unit, suffix in MONEY_UNITS}  # 入力用: k/m/b/t


def fmt(n: int) -> str:
    """金額を短縮表示する。小数第3位以下は切り捨て。例: ￥999 / ￥1.52K / ￥31.59M / ￥2B"""
    sign = "-" if n < 0 else ""
    n = abs(int(n))
    body = str(n)
    for unit, suffix in MONEY_UNITS:
        if n >= unit:
            v = n * 100 // unit  # 整数演算で小数第2位まで切り捨て(floatの誤差を避ける)
            body = f"{v // 100}.{v % 100:02d}".rstrip("0").rstrip(".") + suffix
            break
    return f"{sign}{CURRENCY}{body}"


def apply_interest(debt: int, periods: int) -> int:
    """借金に利息を periods 回ぶんつける。1回ごとに10%を足し、小数点以下は切り捨て。上限は MAX_DEBT"""
    for _ in range(max(periods, 0)):
        interest = debt * DEBT_INTEREST_PERCENT // 100  # 整数の割り算なので切り捨てになる
        if debt >= MAX_DEBT or interest == 0:
            break  # 上限に達した、または利息が1未満(それ以上増えない)
        debt = min(MAX_DEBT, debt + interest)
    return debt


class EconomyError(Exception):
    """ユーザーに表示するエラー"""


def normalize_company_name(name: str) -> str:
    """会社名を整える(空白は1つにまとめる)。使えない名前なら EconomyError"""
    name = " ".join(name.split())
    if not COMPANY_NAME_MIN <= len(name) <= COMPANY_NAME_MAX:
        raise EconomyError(f"会社名は{COMPANY_NAME_MIN}〜{COMPANY_NAME_MAX}文字で入力してください。(現在{len(name)}文字)")
    bad = [c for c in COMPANY_NAME_FORBIDDEN if c in name]
    if bad:
        raise EconomyError(f"会社名に使えない文字が含まれています: {' '.join(bad)}")
    return name


# ===== データベース =====
class Database:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    guild_id  INTEGER NOT NULL,
                    user_id   INTEGER NOT NULL,
                    cash      INTEGER NOT NULL DEFAULT 0,
                    bank      INTEGER NOT NULL DEFAULT 0,
                    last_work INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS items (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id    INTEGER NOT NULL,
                    name        TEXT NOT NULL,
                    price       INTEGER NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    stock       INTEGER NOT NULL DEFAULT -1,
                    role_id     INTEGER
                );
                CREATE TABLE IF NOT EXISTS inventory (
                    guild_id  INTEGER NOT NULL,
                    user_id   INTEGER NOT NULL,
                    item_name TEXT NOT NULL,
                    quantity  INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id, item_name)
                );
                CREATE TABLE IF NOT EXISTS stickies (
                    channel_id INTEGER PRIMARY KEY,
                    guild_id   INTEGER NOT NULL,
                    content    TEXT NOT NULL,
                    message_id INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS companies (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id   INTEGER NOT NULL,
                    name       TEXT NOT NULL,
                    name_key   TEXT NOT NULL,
                    owner_id   INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    UNIQUE (guild_id, name_key)
                );
                CREATE TABLE IF NOT EXISTS company_members (
                    guild_id   INTEGER NOT NULL,
                    user_id    INTEGER NOT NULL,
                    company_id INTEGER NOT NULL,
                    joined_at  INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS company_invites (
                    company_id INTEGER NOT NULL,
                    user_id    INTEGER NOT NULL,
                    invited_by INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY (company_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS verify_panels (
                    message_id INTEGER PRIMARY KEY,
                    guild_id   INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    role_id    INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS level_settings (
                    guild_id    INTEGER PRIMARY KEY,
                    notify_mode TEXT NOT NULL DEFAULT 'current',
                    channel_id  INTEGER
                );
                CREATE TABLE IF NOT EXISTS level_roles (
                    guild_id INTEGER NOT NULL,
                    role_id  INTEGER NOT NULL,
                    level    INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, role_id)
                );
                """
            )
        self._migrate()

    def _migrate(self):
        """既存DBに XP 用の列がなければ追加する"""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(users)")}
        with self.conn:
            for name in ("xp", "xp_spent", "last_xp", "last_convert", "job", "debt", "debt_since"):
                if name not in cols:
                    self.conn.execute(
                        f"ALTER TABLE users ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0"
                    )

    # --- ユーザー ---
    def _ensure(self, g: int, u: int) -> sqlite3.Row:
        self.conn.execute(
            "INSERT OR IGNORE INTO users (guild_id, user_id) VALUES (?, ?)", (g, u)
        )
        row = self.conn.execute(
            "SELECT * FROM users WHERE guild_id=? AND user_id=?", (g, u)
        ).fetchone()
        if row["debt"] > 0:
            row = self._accrue(row)
        return row

    def _accrue(self, row: sqlite3.Row) -> sqlite3.Row:
        """借金に、たまっている利息を反映する。起点は借りた時刻で、3時間ごとに区切る"""
        now = int(time.time())
        g, u = row["guild_id"], row["user_id"]
        since = row["debt_since"] or now
        periods = max((now - since) // DEBT_INTEREST_PERIOD, 0)
        if periods == 0 and row["debt_since"]:
            return row
        self.conn.execute(
            "UPDATE users SET debt=?, debt_since=? WHERE guild_id=? AND user_id=?",
            (apply_interest(row["debt"], periods), since + periods * DEBT_INTEREST_PERIOD, g, u),
        )
        return self.conn.execute(
            "SELECT * FROM users WHERE guild_id=? AND user_id=?", (g, u)
        ).fetchone()

    def _accrue_all(self, g: int):
        for row in self.conn.execute(
            "SELECT * FROM users WHERE guild_id=? AND debt>0", (g,)
        ).fetchall():
            self._accrue(row)

    def get_user(self, g: int, u: int) -> dict:
        with self.conn:
            return dict(self._ensure(g, u))

    def work(self, g: int, u: int):
        """(成功したか, 稼いだ額(会社ボーナス込み) or 次に働ける時刻, 職業のid(0=無職), 会社ボーナスの額)"""
        now = int(time.time())
        with self.conn:
            row = self._ensure(g, u)
            ready_at = row["last_work"] + WORK_COOLDOWN
            if now < ready_at:
                return False, ready_at, row["job"], 0
            job = JOBS_BY_ID.get(row["job"])
            low, high = (job["min"], job["max"]) if job else (WORK_MIN, WORK_MAX)
            amount = random.randint(low, high)
            in_company = self.conn.execute(
                "SELECT 1 FROM company_members WHERE guild_id=? AND user_id=?", (g, u)
            ).fetchone()
            bonus = amount * COMPANY_BONUS_PERCENT // 100 if in_company else 0  # 小数点以下は切り捨て
            total = amount + bonus
            self.conn.execute(
                "UPDATE users SET cash=cash+?, last_work=? WHERE guild_id=? AND user_id=?",
                (total, now, g, u),
            )
            return True, total, row["job"], bonus

    def set_job(self, g: int, u: int, job_id: int):
        """職を変更する。job_id=0 で無職。解放レベル未満なら EconomyError。変更後の職業(dict or None)を返す"""
        with self.conn:
            row = self._ensure(g, u)
            if job_id == 0:
                job = None
            else:
                job = JOBS_BY_ID.get(job_id)
                if job is None:
                    raise EconomyError("その職業は存在しません。")
                level = row["xp"] // XP_PER_LEVEL
                if level < job["level"]:
                    raise EconomyError(
                        f"**{job['name']}** にはレベル{job['level']}が必要です。(現在: Lv{level})"
                    )
            if row["job"] == job_id:
                raise EconomyError("すでにその職業に就いています。")
            self.conn.execute(
                "UPDATE users SET job=? WHERE guild_id=? AND user_id=?", (job_id, g, u)
            )
            return job

    def transfer(self, g: int, u: int, direction: str, amount):
        """direction: deposit / withdraw。amount=None で全額。(移動額, 現金, 銀行)"""
        with self.conn:
            row = self._ensure(g, u)
            src = row["cash"] if direction == "deposit" else row["bank"]
            if amount is None:
                amount = src
            if amount <= 0:
                raise EconomyError("移動できるお金がありません。")
            if amount > src:
                where = "所持金" if direction == "deposit" else "銀行残高"
                raise EconomyError(f"{where}が足りません。({where}: {fmt(src)})")
            sign = 1 if direction == "deposit" else -1
            self.conn.execute(
                "UPDATE users SET cash=cash-?, bank=bank+? WHERE guild_id=? AND user_id=?",
                (sign * amount, sign * amount, g, u),
            )
            return amount, row["cash"] - sign * amount, row["bank"] + sign * amount

    def leaderboard(self, g: int, limit: int = 10):
        """資産(現金+銀行-借金)のランキング。借金は引くので、借りたお金で順位は上がらない"""
        with self.conn:
            self._accrue_all(g)
        return self.conn.execute(
            "SELECT user_id, cash, bank, debt, cash+bank-debt AS total FROM users "
            "WHERE guild_id=? AND (cash+bank>0 OR debt>0) ORDER BY total DESC LIMIT ?",
            (g, limit),
        ).fetchall()

    def rank_of(self, g: int, u: int):
        with self.conn:
            self._accrue_all(g)
        row = self.get_user(g, u)
        total = row["cash"] + row["bank"] - row["debt"]
        higher = self.conn.execute(
            "SELECT COUNT(*) FROM users WHERE guild_id=? AND cash+bank-debt>?", (g, total)
        ).fetchone()[0]
        return higher + 1, total

    # --- 借金 / 送金 ---
    def borrow(self, g: int, u: int, amount):
        """借りる。amount=None で上限まで。(借りた額, 現金, 借金)"""
        with self.conn:
            row = self._ensure(g, u)
            room = MAX_DEBT - row["debt"]
            if room <= 0:
                raise EconomyError(f"借金が上限（{fmt(MAX_DEBT)}）に達しています。先に返済してください。")
            if amount is None:
                amount = room
            if amount <= 0:
                raise EconomyError("借りる金額は1以上で指定してください。")
            if amount > room:
                raise EconomyError(
                    f"借金は合計{fmt(MAX_DEBT)}までです。あと借りられるのは **{fmt(room)}** です。"
                    f"(現在の借金: {fmt(row['debt'])})"
                )
            since = row["debt_since"] if row["debt"] > 0 else int(time.time())
            self.conn.execute(
                "UPDATE users SET cash=cash+?, debt=debt+?, debt_since=? WHERE guild_id=? AND user_id=?",
                (amount, amount, since, g, u),
            )
            return amount, row["cash"] + amount, row["debt"] + amount

    def repay(self, g: int, u: int, amount):
        """返す(所持金から)。amount=None で返せるだけ全部。借金より多い金額は借金の額まで。(返した額, 現金, 借金)"""
        with self.conn:
            row = self._ensure(g, u)
            if row["debt"] <= 0:
                raise EconomyError("借金はありません。")
            if amount is None:
                amount = min(row["cash"], row["debt"])
                if amount <= 0:
                    raise EconomyError("返済に使える所持金がありません。`/bank` で引き出してください。")
            amount = min(amount, row["debt"])
            if amount > row["cash"]:
                raise EconomyError(
                    f"所持金が足りません。(所持金: {fmt(row['cash'])})\n"
                    "銀行に預けているお金は `/bank` で引き出せます。"
                )
            new_debt = row["debt"] - amount
            self.conn.execute(
                "UPDATE users SET cash=cash-?, debt=?, debt_since=? WHERE guild_id=? AND user_id=?",
                (amount, new_debt, row["debt_since"] if new_debt > 0 else 0, g, u),
            )
            return amount, row["cash"] - amount, new_debt

    def pay(self, g: int, from_id: int, to_id: int, amount):
        """所持金を他のプレイヤーに送る。借金がある間は送れない(借金の押し付け・隠し)。(送った額, 送り主の現金, 相手の現金)"""
        with self.conn:
            row = self._ensure(g, from_id)
            if row["debt"] > 0:
                raise EconomyError(f"借金がある間は送金できません。(借金: {fmt(row['debt'])})")
            if amount is None:
                amount = row["cash"]
            if amount <= 0:
                raise EconomyError("送れるお金がありません。")
            if amount > row["cash"]:
                raise EconomyError(f"所持金が足りません。(所持金: {fmt(row['cash'])})")
            target = self._ensure(g, to_id)
            self.conn.execute(
                "UPDATE users SET cash=cash-? WHERE guild_id=? AND user_id=?", (amount, g, from_id)
            )
            self.conn.execute(
                "UPDATE users SET cash=cash+? WHERE guild_id=? AND user_id=?", (amount, g, to_id)
            )
            return amount, row["cash"] - amount, target["cash"] + amount

    # --- XP / レベル ---
    def add_xp(self, g: int, u: int):
        """XPを加算する。(加算前のLv, 加算後のLv) を返す"""
        with self.conn:
            row = self._ensure(g, u)
            new_xp = row["xp"] + XP_PER_MESSAGE
            self.conn.execute(
                "UPDATE users SET xp=? WHERE guild_id=? AND user_id=?",
                (new_xp, g, u),
            )
            return row["xp"] // XP_PER_LEVEL, new_xp // XP_PER_LEVEL

    def xp_rank(self, g: int, u: int) -> int:
        row = self.get_user(g, u)
        higher = self.conn.execute(
            "SELECT COUNT(*) FROM users WHERE guild_id=? AND xp>?", (g, row["xp"])
        ).fetchone()[0]
        return higher + 1

    def convert_xp(self, g: int, u: int, amount):
        """XPをお金に変換する。amount=None で全部。(使ったXP, 受け取った額, 現金, 残りの変換可能XP)
        レベルは累計XPで決まるので、変換してもレベルは下がらない。"""
        with self.conn:
            row = self._ensure(g, u)
            now = int(time.time())
            ready_at = row["last_convert"] + XP_CONVERT_COOLDOWN
            if now < ready_at:
                raise EconomyError(f"次にXPを変換できるのは <t:{ready_at}:R> です。")
            available = row["xp"] - row["xp_spent"]
            if amount is None:
                amount = available
            if amount <= 0:
                raise EconomyError("変換できるXPがありません。")
            if amount > available:
                raise EconomyError(f"変換できるXPが足りません。(変換可能: {available:,}XP)")
            money = amount * XP_TO_MONEY
            self.conn.execute(
                "UPDATE users SET xp_spent=xp_spent+?, cash=cash+?, last_convert=? "
                "WHERE guild_id=? AND user_id=?",
                (amount, money, now, g, u),
            )
            return amount, money, row["cash"] + money, available - amount

    def get_level_settings(self, g: int) -> dict:
        row = self.conn.execute(
            "SELECT notify_mode, channel_id FROM level_settings WHERE guild_id=?", (g,)
        ).fetchone()
        return dict(row) if row else {"notify_mode": "current", "channel_id": None}

    def set_notify(self, g: int, mode: str, channel_id=None):
        """mode: current(メッセージを送ったチャンネル) / channel(指定チャンネル) / off"""
        with self.conn:
            self.conn.execute(
                "INSERT INTO level_settings (guild_id, notify_mode, channel_id) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET "
                "notify_mode=excluded.notify_mode, channel_id=excluded.channel_id",
                (g, mode, channel_id),
            )

    def list_level_roles(self, g: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT level, role_id FROM level_roles WHERE guild_id=? ORDER BY level, role_id",
            (g,),
        ).fetchall()
        return [dict(r) for r in rows]

    def level_role_ids_up_to(self, g: int, level: int) -> list[int]:
        rows = self.conn.execute(
            "SELECT role_id FROM level_roles WHERE guild_id=? AND level<=?", (g, level)
        ).fetchall()
        return [r["role_id"] for r in rows]

    def set_level_role(self, g: int, level: int, role_id: int):
        with self.conn:
            exists = self.conn.execute(
                "SELECT 1 FROM level_roles WHERE guild_id=? AND role_id=?", (g, role_id)
            ).fetchone()
            count = self.conn.execute(
                "SELECT COUNT(*) FROM level_roles WHERE guild_id=?", (g,)
            ).fetchone()[0]
            if not exists and count >= MAX_LEVEL_ROLES:
                raise EconomyError(f"ロール報酬は最大{MAX_LEVEL_ROLES}個までです。")
            self.conn.execute(
                "INSERT INTO level_roles (guild_id, role_id, level) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id, role_id) DO UPDATE SET level=excluded.level",
                (g, role_id, level),
            )

    def delete_level_role(self, g: int, role_id: int):
        with self.conn:
            self.conn.execute(
                "DELETE FROM level_roles WHERE guild_id=? AND role_id=?", (g, role_id)
            )

    # --- ギャンブル ---
    def bet_take(self, g: int, u: int, bet: int) -> int:
        """賭け金を所持金から引く(ブラックジャック用)。残りの所持金を返す"""
        with self.conn:
            row = self._ensure(g, u)
            if row["cash"] < bet:
                raise EconomyError(f"所持金が足りません。(所持金: {fmt(row['cash'])})")
            self.conn.execute(
                "UPDATE users SET cash=cash-? WHERE guild_id=? AND user_id=?", (bet, g, u)
            )
            return row["cash"] - bet

    def bet_give(self, g: int, u: int, amount: int) -> int:
        """払い戻しを所持金に足す。所持金を返す"""
        with self.conn:
            row = self._ensure(g, u)
            self.conn.execute(
                "UPDATE users SET cash=cash+? WHERE guild_id=? AND user_id=?", (amount, g, u)
            )
            return row["cash"] + amount

    def play(self, g: int, u: int, bet: int, payout: int) -> int:
        """1ゲームを一度に精算する(賭け金を引いて払い戻しを足す)。所持金を返す"""
        with self.conn:
            row = self._ensure(g, u)
            if row["cash"] < bet:
                raise EconomyError(f"所持金が足りません。(所持金: {fmt(row['cash'])})")
            new_cash = row["cash"] - bet + payout
            self.conn.execute(
                "UPDATE users SET cash=? WHERE guild_id=? AND user_id=?", (new_cash, g, u)
            )
            return new_cash

    # --- 固定メッセージ ---
    def load_stickies(self) -> dict[int, dict]:
        rows = self.conn.execute("SELECT * FROM stickies").fetchall()
        return {
            r["channel_id"]: {"guild_id": r["guild_id"], "content": r["content"], "message_id": r["message_id"]}
            for r in rows
        }

    def set_sticky(self, g: int, channel_id: int, content: str, message_id: int = 0):
        with self.conn:
            self.conn.execute(
                "INSERT INTO stickies (channel_id, guild_id, content, message_id) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(channel_id) DO UPDATE SET "
                "guild_id=excluded.guild_id, content=excluded.content, message_id=excluded.message_id",
                (channel_id, g, content, message_id),
            )

    def set_sticky_message(self, channel_id: int, message_id: int):
        with self.conn:
            self.conn.execute(
                "UPDATE stickies SET message_id=? WHERE channel_id=?", (message_id, channel_id)
            )

    def delete_sticky(self, channel_id: int):
        with self.conn:
            self.conn.execute("DELETE FROM stickies WHERE channel_id=?", (channel_id,))

    # --- 会社 ---
    def _company_row(self, g: int, user_id: int):
        return self.conn.execute(
            "SELECT c.* FROM companies c JOIN company_members m ON m.company_id=c.id "
            "WHERE m.guild_id=? AND m.user_id=?",
            (g, user_id),
        ).fetchone()

    def get_company_of(self, g: int, user_id: int) -> dict | None:
        row = self._company_row(g, user_id)
        return dict(row) if row else None

    def find_company(self, g: int, name: str) -> dict | None:
        key = " ".join(name.split()).casefold()
        row = self.conn.execute(
            "SELECT * FROM companies WHERE guild_id=? AND name_key=?", (g, key)
        ).fetchone()
        return dict(row) if row else None

    def company_members(self, company_id: int) -> list[int]:
        rows = self.conn.execute(
            "SELECT user_id FROM company_members WHERE company_id=? ORDER BY joined_at, user_id",
            (company_id,),
        ).fetchall()
        return [r["user_id"] for r in rows]

    def pending_invites(self, g: int, user_id: int) -> list[dict]:
        """有効期限内の招待(古い順)"""
        rows = self.conn.execute(
            "SELECT c.* FROM company_invites i JOIN companies c ON c.id=i.company_id "
            "WHERE c.guild_id=? AND i.user_id=? AND i.created_at>? ORDER BY i.created_at, c.id",
            (g, user_id, int(time.time()) - COMPANY_INVITE_TTL),
        ).fetchall()
        return [dict(r) for r in rows]

    def create_company(self, g: int, owner_id: int, name: str) -> dict:
        name = normalize_company_name(name)
        with self.conn:
            if self._company_row(g, owner_id):
                raise EconomyError("すでに会社に入っています。会社を作るには、先に `/company leave` で脱退してください。")
            if self.find_company(g, name):
                raise EconomyError("同じ名前の会社がすでにあります。別の名前にしてください。")
            now = int(time.time())
            cur = self.conn.execute(
                "INSERT INTO companies (guild_id, name, name_key, owner_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (g, name, name.casefold(), owner_id, now),
            )
            company_id = cur.lastrowid
            self.conn.execute(
                "INSERT INTO company_members (guild_id, user_id, company_id, joined_at) VALUES (?, ?, ?, ?)",
                (g, owner_id, company_id, now),
            )
            self.conn.execute(  # 自分で会社を作ったので、もらっていた招待は不要
                "DELETE FROM company_invites WHERE user_id=? AND company_id IN "
                "(SELECT id FROM companies WHERE guild_id=?)",
                (owner_id, g),
            )
            return dict(self.conn.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone())

    def delete_company(self, g: int, user_id: int):
        """社長だけが会社を削除できる。全員が脱退した扱いになる。(会社, 所属していたユーザーIDの一覧)を返す"""
        with self.conn:
            row = self._company_row(g, user_id)
            if row is None:
                raise EconomyError("会社に入っていません。")
            if row["owner_id"] != user_id:
                raise EconomyError("会社を削除できるのは社長だけです。")
            members = self.company_members(row["id"])
            self.conn.execute("DELETE FROM company_members WHERE company_id=?", (row["id"],))
            self.conn.execute("DELETE FROM company_invites WHERE company_id=?", (row["id"],))
            self.conn.execute("DELETE FROM companies WHERE id=?", (row["id"],))
            return dict(row), members

    def invite_to_company(self, g: int, owner_id: int, target_id: int) -> dict:
        """社長が、他のユーザーを招待する(有効期限24時間)。招待した会社を返す"""
        with self.conn:
            company = self._company_row(g, owner_id)
            if company is None:
                raise EconomyError("会社に入っていません。先に `/company create` で会社を設立してください。")
            if company["owner_id"] != owner_id:
                raise EconomyError("招待できるのは社長だけです。")
            if target_id == owner_id:
                raise EconomyError("自分は招待できません。")
            if self._company_row(g, target_id):
                raise EconomyError("その人はすでに会社に入っています。")
            now = int(time.time())
            self.conn.execute(
                "DELETE FROM company_invites WHERE created_at<=?", (now - COMPANY_INVITE_TTL,)
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO company_invites (company_id, user_id, invited_by, created_at) "
                "VALUES (?, ?, ?, ?)",
                (company["id"], target_id, owner_id, now),
            )
            return dict(company)

    def join_company(self, g: int, user_id: int, name: str | None = None) -> dict:
        """招待された会社に参加する。招待が1つだけなら会社名は省略できる"""
        with self.conn:
            if self._company_row(g, user_id):
                raise EconomyError("すでに会社に入っています。別の会社に入るには、先に `/company leave` で脱退してください。")
            invites = self.pending_invites(g, user_id)
            if not invites:
                raise EconomyError(
                    f"招待されている会社がありません。（招待の有効期限は{COMPANY_INVITE_TTL // 3600}時間です）"
                )
            names = "、".join(c["name"] for c in invites)
            if name is not None and name.strip():
                key = " ".join(name.split()).casefold()
                matched = [c for c in invites if c["name_key"] == key]
                if not matched:
                    raise EconomyError(f"その会社からは招待されていません。招待されている会社: {names}")
                company = matched[0]
            elif len(invites) > 1:
                raise EconomyError(f"複数の会社から招待されています。会社名を指定してください: {names}")
            else:
                company = invites[0]
            self.conn.execute(
                "INSERT INTO company_members (guild_id, user_id, company_id, joined_at) VALUES (?, ?, ?, ?)",
                (g, user_id, company["id"], int(time.time())),
            )
            self.conn.execute(  # 参加したので、このサーバーでの他の招待は無効にする
                "DELETE FROM company_invites WHERE user_id=? AND company_id IN "
                "(SELECT id FROM companies WHERE guild_id=?)",
                (user_id, g),
            )
            return company

    def leave_company(self, g: int, user_id: int) -> dict:
        with self.conn:
            row = self._company_row(g, user_id)
            if row is None:
                raise EconomyError("会社に入っていません。")
            if row["owner_id"] == user_id:
                raise EconomyError("社長は脱退できません。会社をやめる場合は `/company delete` で会社を削除してください。")
            self.conn.execute(
                "DELETE FROM company_members WHERE guild_id=? AND user_id=?", (g, user_id)
            )
            return dict(row)

    # --- 認証パネル ---
    def add_verify_panel(self, g: int, channel_id: int, message_id: int, role_id: int):
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO verify_panels (message_id, guild_id, channel_id, role_id) "
                "VALUES (?, ?, ?, ?)",
                (message_id, g, channel_id, role_id),
            )

    def get_verify_panel(self, message_id: int):
        row = self.conn.execute(
            "SELECT * FROM verify_panels WHERE message_id=?", (message_id,)
        ).fetchone()
        return dict(row) if row else None

    def delete_verify_panel(self, message_id: int):
        with self.conn:
            self.conn.execute("DELETE FROM verify_panels WHERE message_id=?", (message_id,))

    # --- ショップ ---
    def list_items(self, g: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM items WHERE guild_id=? ORDER BY price, id", (g,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_item(self, g: int, item_id: int):
        row = self.conn.execute(
            "SELECT * FROM items WHERE guild_id=? AND id=?", (g, item_id)
        ).fetchone()
        return dict(row) if row else None

    def add_item(self, g, name, price, description, stock, role_id):
        with self.conn:
            count = self.conn.execute(
                "SELECT COUNT(*) FROM items WHERE guild_id=?", (g,)
            ).fetchone()[0]
            if count >= MAX_ITEMS:
                raise EconomyError(f"アイテムは最大{MAX_ITEMS}個までです。")
            self.conn.execute(
                "INSERT INTO items (guild_id, name, price, description, stock, role_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (g, name, price, description, stock, role_id),
            )

    def update_item(self, g, item_id, name, price, description, stock, role_id):
        with self.conn:
            self.conn.execute(
                "UPDATE items SET name=?, price=?, description=?, stock=?, role_id=? "
                "WHERE guild_id=? AND id=?",
                (name, price, description, stock, role_id, g, item_id),
            )

    def delete_item(self, g, item_id):
        with self.conn:
            self.conn.execute(
                "DELETE FROM items WHERE guild_id=? AND id=?", (g, item_id)
            )

    def buy(self, g: int, u: int, item_id: int) -> dict:
        with self.conn:
            item = self.conn.execute(
                "SELECT * FROM items WHERE guild_id=? AND id=?", (g, item_id)
            ).fetchone()
            if not item:
                raise EconomyError("そのアイテムは存在しません。(削除された可能性があります)")
            if item["stock"] == 0:
                raise EconomyError("在庫切れです。")
            row = self._ensure(g, u)
            if row["cash"] < item["price"]:
                raise EconomyError(
                    f"所持金が足りません。(価格: {fmt(item['price'])} / 所持金: {fmt(row['cash'])})\n"
                    "銀行に預けているお金は `/bank` で引き出せます。"
                )
            self.conn.execute(
                "UPDATE users SET cash=cash-? WHERE guild_id=? AND user_id=?",
                (item["price"], g, u),
            )
            if item["stock"] > 0:
                self.conn.execute(
                    "UPDATE items SET stock=stock-1 WHERE id=?", (item_id,)
                )
            self.conn.execute(
                "INSERT INTO inventory (guild_id, user_id, item_name, quantity) "
                "VALUES (?, ?, ?, 1) "
                "ON CONFLICT(guild_id, user_id, item_name) DO UPDATE SET quantity=quantity+1",
                (g, u, item["name"]),
            )
            return dict(item)

    def inventory(self, g: int, u: int):
        return self.conn.execute(
            "SELECT item_name, quantity FROM inventory "
            "WHERE guild_id=? AND user_id=? AND quantity>0 ORDER BY item_name",
            (g, u),
        ).fetchall()


# ===== カジノ(ゲームロジック。Discordに依存しない部分) =====
# --- casino logic start ---
MIN_BET = 100  # 最低賭け金

# ルーレット(ヨーロピアン: 0〜36)。倍率は「賭け金を含めた受取額の倍数」
RED_NUMBERS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
ROULETTE_BETS = {
    # key: (表示名, 倍率, 当たりの数字の数)
    "red": ("🔴 赤", 2, 18),
    "black": ("⚫ 黒", 2, 18),
    "even": ("偶数", 2, 18),
    "odd": ("奇数", 2, 18),
    "low": ("1〜18", 2, 18),
    "high": ("19〜36", 2, 18),
    "d1": ("1〜12", 3, 12),
    "d2": ("13〜24", 3, 12),
    "d3": ("25〜36", 3, 12),
    "number": ("🎯 数字を指定 (0〜36)", 36, 1),
}


def roulette_color(n: int) -> str:
    return "🟢" if n == 0 else ("🔴" if n in RED_NUMBERS else "⚫")


def roulette_wins(key: str, n: int, number: int | None = None) -> bool:
    if key == "number":
        return n == number
    if n == 0:
        return False  # 0は数字指定以外すべてハズレ
    return {
        "red": n in RED_NUMBERS,
        "black": n not in RED_NUMBERS,
        "even": n % 2 == 0,
        "odd": n % 2 == 1,
        "low": n <= 18,
        "high": n >= 19,
        "d1": n <= 12,
        "d2": 13 <= n <= 24,
        "d3": n >= 25,
    }[key]


# スロット: (絵文字, 出やすさ(重み), 3つそろった時の倍率)
SLOT_SYMBOLS = [
    ("🍒", 30, 5),
    ("🍋", 25, 10),
    ("🔔", 20, 18),
    ("⭐", 14, 35),
    ("💎", 8, 80),
    ("7️⃣", 3, 400),
]
SLOT_CHERRY_PAIR = 2  # 🍒がちょうど2つの時の倍率


def slot_multiplier(reels: list[str]) -> int:
    if len(set(reels)) == 1:
        return next(m for s, _w, m in SLOT_SYMBOLS if s == reels[0])
    if reels.count("🍒") == 2:
        return SLOT_CHERRY_PAIR
    return 0


def slot_spin() -> list[str]:
    symbols = [s for s, _w, _m in SLOT_SYMBOLS]
    weights = [w for _s, w, _m in SLOT_SYMBOLS]
    return random.choices(symbols, weights=weights, k=3)


def slot_rtp() -> float:
    """還元率(賭け金に対する期待払戻の割合)を全組み合わせの列挙で計算する"""
    total = sum(w for _s, w, _m in SLOT_SYMBOLS)
    rtp = 0.0
    for a, wa, _ in SLOT_SYMBOLS:
        for b, wb, _ in SLOT_SYMBOLS:
            for c, wc, _ in SLOT_SYMBOLS:
                p = (wa / total) * (wb / total) * (wc / total)
                rtp += p * slot_multiplier([a, b, c])
    return rtp


def fmt_pct(p: float) -> str:
    """確率(0〜1)を%表示にする。小さい確率が0.00%にならないよう、桁数を自動で増やす"""
    percent = p * 100
    if percent >= 1:
        return f"{percent:.1f}%"
    if percent >= 0.01:
        return f"{percent:.2f}%"
    return f"{percent:.4f}%"


# ブラックジャック
SUITS = "♠♥♦♣"
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]


def new_deck() -> list[tuple[str, str]]:
    deck = [(r, s) for r in RANKS for s in SUITS]
    random.shuffle(deck)
    return deck


def hand_value(hand: list[tuple[str, str]]) -> int:
    total, aces = 0, 0
    for rank, _suit in hand:
        if rank == "A":
            total += 11
            aces += 1
        elif rank in ("J", "Q", "K"):
            total += 10
        else:
            total += int(rank)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def is_blackjack(hand) -> bool:
    return len(hand) == 2 and hand_value(hand) == 21


def hand_text(hand, hide_second: bool = False) -> str:
    cards = [f"{r}{s}" for r, s in hand]
    if hide_second:
        cards[1:] = ["🂠"]
    return " ".join(cards)


def dealer_play(dealer: list, deck: list) -> None:
    """ディーラーは17以上になるまで引く(ソフト17でも止まる)"""
    while hand_value(dealer) < 17:
        dealer.append(deck.pop())


def blackjack_outcome(player: list, dealer: list) -> str:
    """ディーラーが引き終わった後の勝敗: blackjack / win / push / lose"""
    p, d = hand_value(player), hand_value(dealer)
    if is_blackjack(player) or is_blackjack(dealer):
        if is_blackjack(player) and is_blackjack(dealer):
            return "push"
        return "blackjack" if is_blackjack(player) else "lose"
    if p > 21:
        return "lose"
    if d > 21 or p > d:
        return "win"
    return "push" if p == d else "lose"


BLACKJACK_PAYOUT_HALVES = {"blackjack": 5, "win": 4, "push": 2, "lose": 0}  # 受取額 = 賭け金 × (値 / 2)


def blackjack_payout(bet: int, outcome: str) -> int:
    return bet * BLACKJACK_PAYOUT_HALVES[outcome] // 2


# ビデオポーカー(ジャックス・オア・ベター 8/5)。倍率は賭け金を含めた受取額の倍数
POKER_PAYTABLE = [
    # (役のkey, 表示名, 倍率)
    ("royal", "ロイヤルフラッシュ", 250),
    ("straight_flush", "ストレートフラッシュ", 50),
    ("four", "フォーカード", 25),
    ("full_house", "フルハウス", 8),
    ("flush", "フラッシュ", 5),
    ("straight", "ストレート", 4),
    ("three", "スリーカード", 3),
    ("two_pair", "ツーペア", 2),
    ("jacks_or_better", "ジャックス・オア・ベター（Jのワンペア以上）", 1),
]
POKER_NAMES = {key: name for key, name, _m in POKER_PAYTABLE}
POKER_MULTIPLIERS = {key: mult for key, _n, mult in POKER_PAYTABLE}
POKER_VALUES = {"A": 14, "K": 13, "Q": 12, "J": 11}


def poker_value(rank: str) -> int:
    return POKER_VALUES.get(rank) or int(rank)


def poker_hand_key(hand: list[tuple[str, str]]) -> str | None:
    """5枚の役を判定する。役なし(Jのワンペア未満)は None"""
    values = sorted(poker_value(r) for r, _s in hand)
    flush = len({s for _r, s in hand}) == 1
    distinct = sorted(set(values))
    straight = len(distinct) == 5 and (distinct[-1] - distinct[0] == 4 or distinct == [2, 3, 4, 5, 14])
    if straight and flush:
        return "royal" if values == [10, 11, 12, 13, 14] else "straight_flush"
    counts: dict[int, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    shape = sorted(counts.values(), reverse=True)
    if shape[0] == 4:
        return "four"
    if shape == [3, 2]:
        return "full_house"
    if flush:
        return "flush"
    if straight:
        return "straight"
    if shape[0] == 3:
        return "three"
    if shape == [2, 2, 1]:
        return "two_pair"
    if shape == [2, 1, 1, 1] and max(v for v, c in counts.items() if c == 2) >= 11:
        return "jacks_or_better"
    return None


def poker_multiplier(key: str | None) -> int:
    return POKER_MULTIPLIERS.get(key, 0)
# --- casino logic end ---


# ===== Bot本体 =====
def build_sticky_embed(content: str) -> discord.Embed:
    embed = discord.Embed(description=content, color=discord.Color.gold())
    embed.set_footer(text=STICKY_FOOTER)
    return embed


class EconomyBot(discord.Client):
    def __init__(self):
        super().__init__(
            intents=discord.Intents.default(),
            activity=discord.Game(name=STATUS_TEXT),  # 「製作者：ぼく！をプレイ中」と表示される
        )
        self.tree = app_commands.CommandTree(self)
        self.db = Database(DB_PATH)
        self.stickies: dict[int, dict] = self.db.load_stickies()  # channel_id -> {content, message_id, ...}
        self.sticky_pending: set[int] = set()  # 出し直し待ちのチャンネル
        self._tasks: set[asyncio.Task] = set()

    # --- 固定メッセージ ---
    def is_sticky_message(self, message: discord.Message) -> bool:
        """このBotが出した固定メッセージ自身か(自分の再投稿で無限ループしないため)"""
        info = self.stickies.get(message.channel.id)
        if info and message.id == info["message_id"]:
            return True
        if self.user is not None and message.author.id == self.user.id and message.embeds:
            footer = message.embeds[0].footer
            return bool(footer and footer.text == STICKY_FOOTER)
        return False

    def schedule_sticky(self, channel_id: int):
        """新しいメッセージが来たら、少し待ってから一番下に出し直す。待っている間に来たぶんはまとめる"""
        if channel_id in self.sticky_pending:
            return
        self.sticky_pending.add(channel_id)
        task = asyncio.get_running_loop().create_task(self._repost_later(channel_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _repost_later(self, channel_id: int):
        try:
            await asyncio.sleep(STICKY_DELAY)
        finally:
            self.sticky_pending.discard(channel_id)
        try:
            await self.repost_sticky(channel_id)
        except Exception:
            log.exception("固定メッセージの出し直しに失敗しました (channel=%s)", channel_id)

    async def repost_sticky(self, channel_id: int):
        """固定メッセージを新しく送り、前のものを消す。成功したら True、権限不足などで送れなければ False"""
        info = self.stickies.get(channel_id)
        if info is None:
            return True
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.NotFound:  # チャンネルが削除された
                self.stickies.pop(channel_id, None)
                self.db.delete_sticky(channel_id)
                return False
            except discord.HTTPException:
                return False
        old_id = info["message_id"]
        try:
            msg = await channel.send(embed=build_sticky_embed(info["content"]))  # 先に新しいものを送る
        except discord.Forbidden:
            log.warning("固定メッセージを送信できません。権限を確認してください (channel=%s)", channel_id)
            return False
        except discord.HTTPException:
            log.exception("固定メッセージの送信に失敗しました (channel=%s)", channel_id)
            return False
        if self.stickies.get(channel_id) is not info:  # 送っている間に /unpin や /pin の上書きがあった
            try:
                await msg.delete()
            except discord.HTTPException:
                pass
            return True
        info["message_id"] = msg.id
        self.db.set_sticky_message(channel_id, msg.id)
        if old_id:
            try:
                await channel.get_partial_message(old_id).delete()
            except discord.HTTPException:
                pass  # すでに消されている場合など
        return True

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        """認証パネルのメッセージが削除されたら、DBの記録も消す"""
        self.db.delete_verify_panel(payload.message_id)

    async def setup_hook(self):
        self.add_view(VerifyView())  # 再起動後も、すでに送ってある認証パネルのボタンを動かす
        await start_web_server()
        if DEV_GUILD_ID:
            guild = discord.Object(id=int(DEV_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id)

    async def on_message(self, message: discord.Message):
        if (
            message.guild is not None
            and message.channel.id in self.stickies
            and not self.is_sticky_message(message)
        ):
            self.schedule_sticky(message.channel.id)
        if message.author.bot or message.guild is None:
            return
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return
        result = self.db.add_xp(message.guild.id, message.author.id)
        if result[1] > result[0]:
            try:
                await handle_level_up(message, result[1])
            except Exception:
                log.exception("レベルアップ処理に失敗しました")


bot = EconomyBot()


# ===== Render用HTTPサーバー =====
async def start_web_server():
    async def health(_request):
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/healthz", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", "10000"))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("HTTP server listening on port %s", port)


# ===== 共通ヘルパー =====
def is_admin(interaction: discord.Interaction) -> bool:
    return (
        isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.administrator
    )


def parse_amount(text: str, label: str = "金額"):
    """'all'/'全額' なら None、数値ならint。`1500` のほか `1.5K` `31.59M` `10B` のような短縮形も使える
    (小数点以下は切り捨て)。不正なら EconomyError"""
    s = text.strip().replace(",", "")
    for mark in ("円", "￥", "¥"):
        s = s.replace(mark, "")
    s = s.lower().replace("xp", "").strip()
    if s in ("all", "max", "全額", "全部", "すべて"):
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([kmbt]?)", s)
    if not m or (not m.group(2) and "." in m.group(1)):  # 小数は K/M/B/T と一緒のときだけ
        raise EconomyError(f"{label}は数字（`1500` `1.5K` `10B` など）、または `all`（全部）で指定してください。")
    value = int(Decimal(m.group(1)) * MONEY_MULTIPLIERS.get(m.group(2), 1))
    if value <= 0:
        raise EconomyError(f"{label}は1以上で指定してください。")
    return value


def item_stock_text(item: dict) -> str:
    return "無制限" if item["stock"] < 0 else ("売り切れ" if item["stock"] == 0 else f"{item['stock']}個")


def build_shop_embed(guild: discord.Guild, items: list[dict]) -> discord.Embed:
    embed = discord.Embed(title=f"🛒 {guild.name} のショップ", color=discord.Color.gold())
    if not items:
        embed.description = "まだ商品がありません。\n管理者は `/shop-setting` から商品を追加できます。"
        return embed
    embed.description = "下のメニューから商品を選ぶと購入できます。（所持金から支払われます）"
    for it in items:
        lines = [it["description"] or "説明なし", f"在庫: {item_stock_text(it)}"]
        if it["role_id"]:
            lines.append(f"🎭 購入するとロール <@&{it['role_id']}> を付与")
        embed.add_field(
            name=f"{it['name']} — {fmt(it['price'])}",
            value="\n".join(lines),
            inline=False,
        )
    return embed


# ===== ショップ(購入パネル) =====
class BuySelect(discord.ui.Select):
    def __init__(self, items: list[dict]):
        options = [
            discord.SelectOption(
                label=f"{it['name']} - {fmt(it['price'])}"[:100],
                description=f"在庫: {item_stock_text(it)}",
                value=str(it["id"]),
            )
            for it in items[:MAX_ITEMS]
        ]
        super().__init__(placeholder="購入する商品を選択…", options=options)

    async def callback(self, interaction: discord.Interaction):
        try:
            item = bot.db.buy(interaction.guild_id, interaction.user.id, int(self.values[0]))
        except EconomyError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return

        msg = f"✅ **{item['name']}** を {fmt(item['price'])} で購入しました！"
        if item["role_id"]:
            role = interaction.guild.get_role(item["role_id"])
            try:
                if role is None:
                    raise LookupError
                await interaction.user.add_roles(role, reason="ショップで購入")
                msg += f"\nロール {role.mention} を付与しました。"
            except (discord.Forbidden, discord.HTTPException, LookupError):
                msg += "\n⚠️ ロールを付与できませんでした。管理者に連絡してください。"
        await interaction.response.send_message(msg, ephemeral=True)


class ShopView(discord.ui.View):
    def __init__(self, items: list[dict]):
        super().__init__(timeout=600)
        self.add_item(BuySelect(items))
        self.source: discord.Interaction | None = None

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.source:
            try:
                await self.source.edit_original_response(view=self)
            except discord.HTTPException:
                pass


# ===== ショップ設定(管理者) =====
def parse_optional_int(text: str, label: str, minimum: int = 0):
    text = text.strip()
    if not text:
        return None
    try:
        value = int(text.replace(",", ""))
    except ValueError:
        raise EconomyError(f"{label}は数字で入力してください。")
    if value < minimum:
        raise EconomyError(f"{label}は{minimum}以上で入力してください。")
    return value


class ItemModal(discord.ui.Modal):
    def __init__(self, item: dict | None = None):
        super().__init__(title="アイテムを変更" if item else "アイテムを追加")
        self.item = item
        self.name_in = discord.ui.TextInput(
            label="名前", max_length=50, default=item["name"] if item else None
        )
        self.price_in = discord.ui.TextInput(
            label="価格（￥）", max_length=12, default=str(item["price"]) if item else None
        )
        self.desc_in = discord.ui.TextInput(
            label="説明（任意）",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=300,
            default=item["description"] if item else None,
        )
        self.stock_in = discord.ui.TextInput(
            label="在庫数（空欄で無制限）",
            required=False,
            max_length=6,
            default=str(item["stock"]) if item and item["stock"] >= 0 else None,
        )
        self.role_in = discord.ui.TextInput(
            label="購入時に付与するロールID（任意）",
            required=False,
            max_length=25,
            default=str(item["role_id"]) if item and item["role_id"] else None,
        )
        for w in (self.name_in, self.price_in, self.desc_in, self.stock_in, self.role_in):
            self.add_item(w)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("管理者のみ実行できます。", ephemeral=True)
            return
        try:
            price = parse_optional_int(self.price_in.value, "価格")
            if price is None:
                raise EconomyError("価格を入力してください。")
            stock = parse_optional_int(self.stock_in.value, "在庫数")
            stock = -1 if stock is None else stock
            role_id = parse_optional_int(self.role_in.value, "ロールID", 1)
            if role_id and interaction.guild.get_role(role_id) is None:
                raise EconomyError("指定したIDのロールがこのサーバーに見つかりません。")
            name = self.name_in.value.strip()
            if not name:
                raise EconomyError("名前を入力してください。")
            desc = self.desc_in.value.strip()
            if self.item:
                bot.db.update_item(interaction.guild_id, self.item["id"], name, price, desc, stock, role_id)
                text = f"✅ **{name}** を更新しました。"
            else:
                bot.db.add_item(interaction.guild_id, name, price, desc, stock, role_id)
                text = f"✅ **{name}** を追加しました。"
        except EconomyError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await interaction.response.send_message(text, ephemeral=True)


class ItemPickSelect(discord.ui.Select):
    def __init__(self, items: list[dict], mode: str):
        self.mode = mode  # "edit" or "delete"
        options = [
            discord.SelectOption(label=f"{it['name']} - {fmt(it['price'])}"[:100], value=str(it["id"]))
            for it in items[:MAX_ITEMS]
        ]
        placeholder = "変更するアイテムを選択…" if mode == "edit" else "削除するアイテムを選択…"
        super().__init__(placeholder=placeholder, options=options)

    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("管理者のみ実行できます。", ephemeral=True)
            return
        item = bot.db.get_item(interaction.guild_id, int(self.values[0]))
        if item is None:
            await interaction.response.send_message("❌ アイテムが見つかりません。", ephemeral=True)
            return
        if self.mode == "edit":
            await interaction.response.send_modal(ItemModal(item))
        else:
            bot.db.delete_item(interaction.guild_id, item["id"])
            await interaction.response.edit_message(
                content=f"🗑️ **{item['name']}** を削除しました。", view=None
            )


class ItemPickView(discord.ui.View):
    def __init__(self, items: list[dict], mode: str):
        super().__init__(timeout=120)
        self.add_item(ItemPickSelect(items, mode))


class AdminView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if not is_admin(interaction):
            await interaction.response.send_message("管理者のみ実行できます。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="追加", style=discord.ButtonStyle.success, emoji="➕")
    async def add_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if await self._guard(interaction):
            await interaction.response.send_modal(ItemModal())

    @discord.ui.button(label="変更", style=discord.ButtonStyle.primary, emoji="✏️")
    async def edit_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        items = bot.db.list_items(interaction.guild_id)
        if not items:
            await interaction.response.send_message("変更できるアイテムがありません。", ephemeral=True)
            return
        await interaction.response.send_message(view=ItemPickView(items, "edit"), ephemeral=True)

    @discord.ui.button(label="削除", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def delete_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        items = bot.db.list_items(interaction.guild_id)
        if not items:
            await interaction.response.send_message("削除できるアイテムがありません。", ephemeral=True)
            return
        await interaction.response.send_message(view=ItemPickView(items, "delete"), ephemeral=True)


# ===== コマンド =====
@bot.tree.command(name="work", description="働いてお金を稼ぎます（クールダウン1時間）")
@app_commands.guild_only()
async def work(interaction: discord.Interaction):
    ok, value, job_id, bonus = bot.db.work(interaction.guild_id, interaction.user.id)
    if not ok:
        await interaction.response.send_message(
            f"⏳ まだ疲れています。次に働けるのは <t:{value}:R> です。", ephemeral=True
        )
        return
    job = JOBS_BY_ID.get(job_id)
    text = job["msg"] if job else random.choice(WORK_MESSAGES)
    description = f"{text} **{fmt(value)}** 稼ぎました！"
    if bonus:
        company = bot.db.get_company_of(interaction.guild_id, interaction.user.id)
        description += (
            f"\n🏢 {company['name'] if company else '会社'} のボーナス **+{fmt(bonus)}**"
            f"（基本報酬 {fmt(value - bonus)} + {COMPANY_BONUS_PERCENT}%）"
        )
    embed = discord.Embed(description=description, color=discord.Color.green())
    embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
    embed.set_footer(
        text=f"職業: {job['emoji']} {job['name']}" if job else "職業: 無職（/job で職に就こう）"
    )
    await interaction.response.send_message(embed=embed)


# ===== 職業 =====
def job_label(job: dict | None) -> str:
    return f"{job['emoji']} {job['name']}" if job else "無職"


def build_job_embed(user_level: int, current_job_id: int) -> discord.Embed:
    embed = discord.Embed(title="💼 職業一覧", color=discord.Color.blue())
    embed.description = (
        f"現在の職業: **{job_label(JOBS_BY_ID.get(current_job_id))}** / あなたのレベル: **Lv{user_level}**\n"
        "ランクが高い職業ほど `/work` の報酬が増えます。下のメニューから職を選べます。"
    )
    embed.add_field(
        name="無職",
        value=f"報酬: {fmt(WORK_MIN)}〜{fmt(WORK_MAX)}",
        inline=False,
    )
    for rank, job in enumerate(JOBS, start=1):
        unlocked = user_level >= job["level"]
        mark = "✅" if unlocked else "🔒"
        here = "（就業中）" if job["id"] == current_job_id else ""
        embed.add_field(
            name=f"{mark} ランク{rank}: {job['emoji']} {job['name']}{here}",
            value=f"報酬: {fmt(job['min'])}〜{fmt(job['max'])}\n必要レベル: Lv{job['level']}",
            inline=False,
        )
    return embed


class JobSelect(discord.ui.Select):
    def __init__(self, user_level: int, current_job_id: int):
        options = []
        for rank, job in enumerate(JOBS, start=1):
            if user_level < job["level"]:
                continue
            options.append(
                discord.SelectOption(
                    label=f"ランク{rank}: {job['name']}",
                    description=f"{fmt(job['min'])}〜{fmt(job['max'])}",
                    emoji=job["emoji"],
                    value=str(job["id"]),
                    default=job["id"] == current_job_id,
                )
            )
        options.append(
            discord.SelectOption(
                label="辞める（無職）", emoji="🚪", value="0", default=current_job_id == 0
            )
        )
        super().__init__(placeholder="就きたい職業を選択…", options=options)

    async def callback(self, interaction: discord.Interaction):
        view: "JobView" = self.view
        if interaction.user.id != view.owner_id:
            await interaction.response.send_message("これはあなたのパネルではありません。", ephemeral=True)
            return
        try:
            job = bot.db.set_job(interaction.guild_id, interaction.user.id, int(self.values[0]))
        except EconomyError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        if job:
            msg = (
                f"✅ **{job_label(job)}** になりました！"
                f" `/work` の報酬は {fmt(job['min'])}〜{fmt(job['max'])} です。"
            )
        else:
            msg = f"🚪 仕事を辞めました。`/work` の報酬は {fmt(WORK_MIN)}〜{fmt(WORK_MAX)} に戻ります。"
        await interaction.response.edit_message(content=msg, embed=None, view=None)


class JobView(discord.ui.View):
    def __init__(self, owner_id: int, user_level: int, current_job_id: int):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.add_item(JobSelect(user_level, current_job_id))


@bot.tree.command(name="job", description="職業を確認・選択します（ランクが高いほど /work の報酬アップ）")
@app_commands.guild_only()
async def job_cmd(interaction: discord.Interaction):
    row = bot.db.get_user(interaction.guild_id, interaction.user.id)
    level = row["xp"] // XP_PER_LEVEL
    await interaction.response.send_message(
        embed=build_job_embed(level, row["job"]),
        view=JobView(interaction.user.id, level, row["job"]),
        ephemeral=True,
    )


@bot.tree.command(name="balance", description="所持金と銀行残高を確認します")
@app_commands.describe(user="確認したい相手（省略すると自分）")
@app_commands.guild_only()
async def balance(interaction: discord.Interaction, user: discord.Member | None = None):
    target = user or interaction.user
    row = bot.db.get_user(interaction.guild_id, target.id)
    embed = discord.Embed(color=discord.Color.blurple())
    embed.set_author(name=target.display_name, icon_url=target.display_avatar.url)
    embed.add_field(name="💴 所持金", value=fmt(row["cash"]))
    embed.add_field(name="🏦 銀行", value=fmt(row["bank"]))
    if row["debt"] > 0:
        embed.add_field(name="🧾 借金", value=fmt(row["debt"]))
        embed.add_field(name="純資産（借金を引いた額）", value=fmt(row["cash"] + row["bank"] - row["debt"]))
    else:
        embed.add_field(name="合計", value=fmt(row["cash"] + row["bank"]))
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="bank", description="銀行にお金を預けたり、引き出したりします")
@app_commands.describe(action="預けるか引き出すか", amount="金額（`all` または 全額 で全部）")
@app_commands.choices(
    action=[
        app_commands.Choice(name="預ける", value="deposit"),
        app_commands.Choice(name="引き出す", value="withdraw"),
    ]
)
@app_commands.guild_only()
async def bank(interaction: discord.Interaction, action: app_commands.Choice[str], amount: str):
    try:
        parsed = parse_amount(amount)
        moved, cash, bank_bal = bot.db.transfer(
            interaction.guild_id, interaction.user.id, action.value, parsed
        )
    except EconomyError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return
    verb = "預けました" if action.value == "deposit" else "引き出しました"
    embed = discord.Embed(
        description=f"🏦 **{fmt(moved)}** を{verb}。", color=discord.Color.teal()
    )
    embed.add_field(name="💴 所持金", value=fmt(cash))
    embed.add_field(name="🏦 銀行", value=fmt(bank_bal))
    await interaction.response.send_message(embed=embed)


# ===== 借金・送金 =====
def build_debt_embed(row: dict, headline: str | None = None) -> discord.Embed:
    debt = row["debt"]
    embed = discord.Embed(title="🧾 借金", color=discord.Color.dark_red() if debt else discord.Color.green())
    lines = [headline] if headline else []
    if debt > 0:
        next_at = row["debt_since"] + DEBT_INTEREST_PERIOD
        nxt = apply_interest(debt, 1)
        if nxt > debt:
            lines.append(f"⏳ 次の利息: <t:{next_at}:R> に {fmt(debt)} → **{fmt(nxt)}**")
        else:
            lines.append("これ以上利息は増えません。")
    else:
        lines.append("借金はありません。")
    embed.description = "\n".join(lines)
    embed.add_field(name="🧾 借金", value=fmt(debt))
    embed.add_field(name="💴 所持金", value=fmt(row["cash"]))
    embed.add_field(name="借りられる額", value=fmt(MAX_DEBT - debt))
    embed.set_footer(
        text=f"利息: {DEBT_INTEREST_PERIOD // 3600}時間ごとに{DEBT_INTEREST_PERCENT}%（小数点以下は切り捨て）"
        f" / 借金の上限: {fmt(MAX_DEBT)}"
    )
    return embed


@bot.tree.command(
    name="debt",
    description=f"借金を借りる・返す・確認します（利息: {DEBT_INTEREST_PERIOD // 3600}時間ごとに{DEBT_INTEREST_PERCENT}%）",
)
@app_commands.describe(
    action="借りる・返す・確認",
    amount="金額（借りる:`all`で上限まで / 返す:`all`で返せるだけ全部 / 確認では不要）",
)
@app_commands.choices(
    action=[
        app_commands.Choice(name="借りる", value="borrow"),
        app_commands.Choice(name="返す", value="repay"),
        app_commands.Choice(name="確認", value="status"),
    ]
)
@app_commands.guild_only()
async def debt_cmd(interaction: discord.Interaction, action: app_commands.Choice[str], amount: str = ""):
    g, u = interaction.guild_id, interaction.user.id
    headline = None
    try:
        if action.value == "borrow":
            moved, _cash, _debt = bot.db.borrow(g, u, parse_amount(amount, "金額"))
            headline = f"✅ **{fmt(moved)}** を借りました。"
        elif action.value == "repay":
            moved, _cash, remaining = bot.db.repay(g, u, parse_amount(amount, "金額"))
            headline = (
                f"✅ **{fmt(moved)}** 返済しました。完済です！🎉" if remaining == 0
                else f"✅ **{fmt(moved)}** 返済しました。"
            )
    except EconomyError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return
    row = bot.db.get_user(g, u)
    await interaction.response.send_message(embed=build_debt_embed(row, headline))


@bot.tree.command(name="pay", description="他のプレイヤーに所持金を送金します")
@app_commands.describe(user="送る相手", amount="金額（`all` で所持金すべて）")
@app_commands.guild_only()
async def pay_cmd(interaction: discord.Interaction, user: discord.Member, amount: str):
    if user.bot:
        await interaction.response.send_message("❌ Botには送金できません。", ephemeral=True)
        return
    if user.id == interaction.user.id:
        await interaction.response.send_message("❌ 自分には送金できません。", ephemeral=True)
        return
    try:
        moved, my_cash, _their_cash = bot.db.pay(
            interaction.guild_id, interaction.user.id, user.id, parse_amount(amount, "金額")
        )
    except EconomyError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return
    embed = discord.Embed(
        description=f"💸 {interaction.user.mention} が {user.mention} に **{fmt(moved)}** を送金しました。",
        color=discord.Color.green(),
    )
    embed.add_field(name="あなたの所持金", value=fmt(my_cash))
    await interaction.response.send_message(content=user.mention, embed=embed)


# ===== 固定メッセージ(/pin) =====
async def resolve_sticky_channel(interaction: discord.Interaction, channel) -> "discord.TextChannel | None":
    """対象チャンネルを決める。管理者でない・テキストチャンネルでない場合は、理由を返信して None"""
    if not is_admin(interaction):
        await interaction.response.send_message("❌ このコマンドは管理者のみ実行できます。", ephemeral=True)
        return None
    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel):
        await interaction.response.send_message(
            "❌ テキストチャンネルで実行するか、`channel` でテキストチャンネルを指定してください。", ephemeral=True
        )
        return None
    return target


@bot.tree.command(name="pin", description="【管理者のみ】チャンネルの一番下にメッセージを固定します")
@app_commands.describe(
    content="固定する内容（`\\n` で改行できます）",
    channel="固定するチャンネル（省略するとこのチャンネル）",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def pin_cmd(interaction: discord.Interaction, content: str, channel: discord.TextChannel | None = None):
    target = await resolve_sticky_channel(interaction, channel)
    if target is None:
        return
    text = content.replace("\\n", "\n").strip()
    if not text:
        await interaction.response.send_message("❌ 固定する内容を入力してください。", ephemeral=True)
        return
    if len(text) > STICKY_MAX_LENGTH:
        await interaction.response.send_message(
            f"❌ 内容が長すぎます。({STICKY_MAX_LENGTH}文字まで / 現在{len(text)}文字)", ephemeral=True
        )
        return
    perms = target.permissions_for(interaction.guild.me)
    missing = [
        label
        for allowed, label in (
            (perms.view_channel, "チャンネルを見る"),
            (perms.send_messages, "メッセージを送信"),
            (perms.embed_links, "埋め込みリンク"),
        )
        if not allowed
    ]
    if missing:
        await interaction.response.send_message(
            f"❌ Botに {target.mention} での権限が足りません: {'、'.join(missing)}", ephemeral=True
        )
        return

    old = bot.stickies.get(target.id)
    info = {"guild_id": interaction.guild_id, "content": text, "message_id": old["message_id"] if old else 0}
    bot.db.set_sticky(interaction.guild_id, target.id, text, info["message_id"])
    bot.stickies[target.id] = info  # 以降、このチャンネルに新しいメッセージが来るたびに一番下へ出し直す
    await interaction.response.defer(ephemeral=True)
    if await bot.repost_sticky(target.id):
        await interaction.followup.send(
            f"📌 {target.mention} の一番下に固定しました。解除は `/unpin` です。", ephemeral=True
        )
    else:
        bot.stickies.pop(target.id, None)
        bot.db.delete_sticky(target.id)
        await interaction.followup.send(
            f"❌ {target.mention} にメッセージを送信できなかったため、固定を取り消しました。Botの権限を確認してください。",
            ephemeral=True,
        )


@bot.tree.command(name="unpin", description="【管理者のみ】チャンネルの固定メッセージを解除します")
@app_commands.describe(channel="解除するチャンネル（省略するとこのチャンネル）")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def unpin_cmd(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    target = await resolve_sticky_channel(interaction, channel)
    if target is None:
        return
    info = bot.stickies.pop(target.id, None)
    if info is None:
        await interaction.response.send_message(
            f"❌ {target.mention} に固定メッセージはありません。", ephemeral=True
        )
        return
    bot.db.delete_sticky(target.id)
    if info["message_id"]:
        try:
            await target.get_partial_message(info["message_id"]).delete()
        except discord.HTTPException:
            pass
    await interaction.response.send_message(f"✅ {target.mention} の固定メッセージを解除しました。", ephemeral=True)


# ===== 認証パネル(/verify) =====
VERIFY_BUTTON_ID = "verify:button"
# 認証ボタンを押すだけで誰でも付与されるので、強い権限を持つロールは認証ロールにできない
VERIFY_FORBIDDEN_PERMS = (
    ("administrator", "管理者"),
    ("manage_guild", "サーバーの管理"),
    ("manage_roles", "ロールの管理"),
    ("manage_channels", "チャンネルの管理"),
    ("kick_members", "メンバーをキック"),
    ("ban_members", "メンバーをBAN"),
)


class VerifyView(discord.ui.View):
    """認証パネルのボタン。再起動後も動くよう、timeout なし + 固定の custom_id にしている"""

    def __init__(self, label: str = "認証する"):
        super().__init__(timeout=None)
        button = discord.ui.Button(
            label=label, style=discord.ButtonStyle.success, emoji="✅", custom_id=VERIFY_BUTTON_ID
        )
        button.callback = self.on_verify
        self.add_item(button)

    async def on_verify(self, interaction: discord.Interaction):
        panel = bot.db.get_verify_panel(interaction.message.id)
        if panel is None:
            await interaction.response.send_message(
                "❌ このパネルは無効です。管理者に新しいパネルを作ってもらってください。", ephemeral=True
            )
            return
        role = interaction.guild.get_role(panel["role_id"])
        if role is None:
            await interaction.response.send_message(
                "❌ 付与するロールが見つかりません（削除された可能性があります）。管理者に連絡してください。",
                ephemeral=True,
            )
            return
        member = interaction.user
        if role in member.roles:
            await interaction.response.send_message(f"✅ すでに認証済みです。（{role.mention}）", ephemeral=True)
            return
        try:
            await member.add_roles(role, reason="認証パネル")
        except (discord.Forbidden, discord.HTTPException):
            await interaction.response.send_message(
                "❌ ロールを付与できませんでした。管理者に連絡してください。\n"
                "（Botに「ロールの管理」権限があり、Botのロールが対象ロールより上にあるか確認が必要です）",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(f"✅ 認証しました！ {role.mention} を付与しました。", ephemeral=True)


@bot.tree.command(name="verify", description="【管理者のみ】認証パネルを作成します（ボタンを押すとロールが付与されます）")
@app_commands.describe(
    role="認証したときに付与するロール",
    channel="パネルを送るチャンネル（省略するとこのチャンネル）",
    title="パネルのタイトル",
    description="パネルの説明（`\\n` で改行できます）",
    button="ボタンの文字",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def verify_cmd(
    interaction: discord.Interaction,
    role: discord.Role,
    channel: discord.TextChannel | None = None,
    title: str = "認証",
    description: str = "下のボタンを押して認証してください。",
    button: str = "認証する",
):
    async def fail(text: str):
        await interaction.response.send_message(f"❌ {text}", ephemeral=True)

    if not is_admin(interaction):
        await fail("このコマンドは管理者のみ実行できます。")
        return
    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel):
        await fail("テキストチャンネルで実行するか、`channel` でテキストチャンネルを指定してください。")
        return
    title, button = title.strip(), button.strip()
    text = description.replace("\\n", "\n").strip()
    if not title or not button or not text:
        await fail("タイトル・説明・ボタンの文字は、空にできません。")
        return
    if len(title) > 256 or len(text) > 4000 or len(button) > 80:
        await fail("文字数が多すぎます。（タイトル256文字、説明4000文字、ボタン80文字まで）")
        return

    # 付与できるロールか
    me = interaction.guild.me
    if role.is_default() or role.managed:
        await fail("このロールは付与できません。（@everyone・Botや連携サービス用のロールは使えません）")
        return
    if not me.guild_permissions.manage_roles:
        await fail("Botに「ロールの管理」権限がありません。付与してから、もう一度お試しください。")
        return
    if role >= me.top_role:
        await fail(
            f"{role.mention} はBotの最上位ロール以上の位置にあるため、付与できません。"
            "サーバー設定でBotのロールを、このロールより上に移動してください。"
        )
        return
    dangerous = [label for attr, label in VERIFY_FORBIDDEN_PERMS if getattr(role.permissions, attr)]
    if dangerous:
        await fail(
            f"{role.mention} は強い権限（{'、'.join(dangerous)}）を持っているため、認証ロールにできません。"
            "ボタンを押した人が誰でも付与されてしまうためです。"
        )
        return
    perms = target.permissions_for(me)
    missing = [
        label
        for allowed, label in (
            (perms.view_channel, "チャンネルを見る"),
            (perms.send_messages, "メッセージを送信"),
            (perms.embed_links, "埋め込みリンク"),
        )
        if not allowed
    ]
    if missing:
        await fail(f"Botに {target.mention} での権限が足りません: {'、'.join(missing)}")
        return

    embed = discord.Embed(title=title, description=text, color=discord.Color.green())
    try:
        msg = await target.send(embed=embed, view=VerifyView(button))
    except discord.HTTPException:
        await fail(f"{target.mention} にパネルを送信できませんでした。Botの権限を確認してください。")
        return
    bot.db.add_verify_panel(interaction.guild_id, target.id, msg.id, role.id)
    await interaction.response.send_message(
        f"✅ {target.mention} に認証パネルを作成しました。{role.mention} が付与されます。\n{msg.jump_url}",
        ephemeral=True,
    )


# ===== 会社(/company) =====
company_group = app_commands.Group(
    name="company",
    description=f"会社の設立・削除・招待・参加・脱退（会社に入ると /work の報酬が{COMPANY_BONUS_PERCENT}%アップ）",
    guild_only=True,
)
bot.tree.add_command(company_group)


async def company_error(interaction: discord.Interaction, error: EconomyError):
    await interaction.response.send_message(f"❌ {error}", ephemeral=True)


def company_members_text(member_ids: list[int], owner_id: int, limit: int = 20) -> str:
    shown = [f"<@{uid}>" + (" 👑" if uid == owner_id else "") for uid in member_ids[:limit]]
    rest = len(member_ids) - limit
    return "\n".join(shown) + (f"\nほか{rest}人" if rest > 0 else "")


@company_group.command(name="create", description="会社を設立します（社長になります）")
@app_commands.describe(name=f"会社名（{COMPANY_NAME_MIN}〜{COMPANY_NAME_MAX}文字）")
async def company_create(interaction: discord.Interaction, name: str):
    try:
        company = bot.db.create_company(interaction.guild_id, interaction.user.id, name)
    except EconomyError as e:
        await company_error(interaction, e)
        return
    embed = discord.Embed(
        title="🏢 会社を設立しました！",
        description=(
            f"**{company['name']}**\n社長: {interaction.user.mention}\n\n"
            f"会社のメンバーは `/work` の報酬が **+{COMPANY_BONUS_PERCENT}%** になります。\n"
            "`/company invite` で仲間を招待しましょう。"
        ),
        color=discord.Color.blue(),
    )
    await interaction.response.send_message(embed=embed)


class CompanyDeleteConfirm(discord.ui.View):
    """会社の削除は取り消せないので、確認のボタンを出す"""

    def __init__(self, owner_id: int):
        super().__init__(timeout=60)
        self.owner_id = owner_id
        self.source: discord.Interaction | None = None
        confirm = discord.ui.Button(label="削除する", style=discord.ButtonStyle.danger, emoji="🗑️")
        confirm.callback = self.on_confirm
        cancel = discord.ui.Button(label="キャンセル", style=discord.ButtonStyle.secondary)
        cancel.callback = self.on_cancel
        self.add_item(confirm)
        self.add_item(cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("これはあなたの操作ではありません。", ephemeral=True)
            return False
        return True

    async def on_confirm(self, interaction: discord.Interaction):
        try:
            company, members = bot.db.delete_company(interaction.guild_id, interaction.user.id)
        except EconomyError as e:
            await interaction.response.edit_message(content=f"❌ {e}", view=None)
            return
        self.stop()
        await interaction.response.edit_message(
            content=f"🗑️ **{company['name']}** を削除しました。（所属していた{len(members)}人は、全員脱退した扱いになります）",
            view=None,
        )

    async def on_cancel(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(content="キャンセルしました。", view=None)

    async def on_timeout(self):
        if self.source is not None:
            try:
                await self.source.edit_original_response(content="時間切れのため、キャンセルしました。", view=None)
            except discord.HTTPException:
                pass


@company_group.command(name="delete", description="会社を削除します（社長のみ。全員が脱退した扱いになります）")
async def company_delete(interaction: discord.Interaction):
    company = bot.db.get_company_of(interaction.guild_id, interaction.user.id)
    if company is None:
        await company_error(interaction, EconomyError("会社に入っていません。"))
        return
    if company["owner_id"] != interaction.user.id:
        await company_error(interaction, EconomyError("会社を削除できるのは社長だけです。"))
        return
    count = len(bot.db.company_members(company["id"]))
    view = CompanyDeleteConfirm(interaction.user.id)
    await interaction.response.send_message(
        f"⚠️ 本当に **{company['name']}**（メンバー{count}人）を削除しますか？ この操作は取り消せません。",
        view=view,
        ephemeral=True,
    )
    view.source = interaction


@company_group.command(name="invite", description="ユーザーを会社に招待します（社長のみ）")
@app_commands.describe(user="招待する相手")
async def company_invite(interaction: discord.Interaction, user: discord.Member):
    if user.bot:
        await company_error(interaction, EconomyError("Botは招待できません。"))
        return
    try:
        company = bot.db.invite_to_company(interaction.guild_id, interaction.user.id, user.id)
    except EconomyError as e:
        await company_error(interaction, e)
        return
    embed = discord.Embed(
        description=(
            f"📨 {interaction.user.mention} が {user.mention} を **{company['name']}** に招待しました。\n"
            f"参加するには `/company join` を実行してください。（{COMPANY_INVITE_TTL // 3600}時間有効）"
        ),
        color=discord.Color.blue(),
    )
    await interaction.response.send_message(content=user.mention, embed=embed)


@company_group.command(name="join", description="招待された会社に参加します")
@app_commands.describe(name="参加する会社名（招待されている会社が1つだけなら省略できます）")
async def company_join(interaction: discord.Interaction, name: str | None = None):
    try:
        company = bot.db.join_company(interaction.guild_id, interaction.user.id, name)
    except EconomyError as e:
        await company_error(interaction, e)
        return
    embed = discord.Embed(
        description=(
            f"🤝 {interaction.user.mention} が **{company['name']}** に参加しました！\n"
            f"`/work` の報酬が **+{COMPANY_BONUS_PERCENT}%** になります。"
        ),
        color=discord.Color.green(),
    )
    await interaction.response.send_message(embed=embed)


@company_group.command(name="leave", description="会社を脱退します（社長は脱退できません）")
async def company_leave(interaction: discord.Interaction):
    try:
        company = bot.db.leave_company(interaction.guild_id, interaction.user.id)
    except EconomyError as e:
        await company_error(interaction, e)
        return
    embed = discord.Embed(
        description=f"👋 {interaction.user.mention} が **{company['name']}** を脱退しました。",
        color=discord.Color.light_grey(),
    )
    await interaction.response.send_message(embed=embed)


@company_group.command(name="info", description="会社の情報を表示します（省略すると自分の会社）")
@app_commands.describe(name="会社名（省略すると自分の会社）")
async def company_info(interaction: discord.Interaction, name: str | None = None):
    if name is not None and name.strip():
        company = bot.db.find_company(interaction.guild_id, name)
        if company is None:
            await company_error(interaction, EconomyError("その名前の会社はありません。"))
            return
    else:
        company = bot.db.get_company_of(interaction.guild_id, interaction.user.id)
        if company is None:
            await company_error(
                interaction,
                EconomyError("会社に入っていません。`/company create` で設立するか、招待されたら `/company join` で参加できます。"),
            )
            return
    members = bot.db.company_members(company["id"])
    embed = discord.Embed(title=f"🏢 {company['name']}", color=discord.Color.blue())
    embed.add_field(name="社長", value=f"<@{company['owner_id']}>")
    embed.add_field(name="メンバー数", value=f"{len(members)}人")
    embed.add_field(name="設立", value=f"<t:{company['created_at']}:D>")
    embed.add_field(name="メンバー", value=company_members_text(members, company["owner_id"]), inline=False)
    embed.set_footer(text=f"メンバーは /work の報酬が+{COMPANY_BONUS_PERCENT}%")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="shop-setting", description="【管理者のみ】ショップのアイテムを追加・変更・削除します")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def shop_setting(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ このコマンドは管理者のみ実行できます。", ephemeral=True)
        return
    items = bot.db.list_items(interaction.guild_id)
    embed = build_shop_embed(interaction.guild, items)
    embed.title = "⚙️ ショップ設定"
    embed.description = "ボタンでアイテムの追加・変更・削除ができます。"
    await interaction.response.send_message(embed=embed, view=AdminView(), ephemeral=True)


@bot.tree.command(name="shop", description="ショップを表示します")
@app_commands.guild_only()
async def shop(interaction: discord.Interaction):
    items = bot.db.list_items(interaction.guild_id)
    embed = build_shop_embed(interaction.guild, items)
    if items:
        view = ShopView(items)
        await interaction.response.send_message(embed=embed, view=view)
        view.source = interaction
    else:
        await interaction.response.send_message(embed=embed)


@bot.tree.command(name="inventory", description="所持しているアイテムを確認します")
@app_commands.guild_only()
async def inventory(interaction: discord.Interaction):
    rows = bot.db.inventory(interaction.guild_id, interaction.user.id)
    embed = discord.Embed(title="🎒 インベントリ", color=discord.Color.orange())
    embed.description = (
        "\n".join(f"・{r['item_name']} × {r['quantity']}" for r in rows)
        if rows
        else "まだアイテムを持っていません。`/shop` で購入できます。"
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="leaderboard", description="サーバー内の金持ちランキングを表示します")
@app_commands.guild_only()
async def leaderboard(interaction: discord.Interaction):
    rows = bot.db.leaderboard(interaction.guild_id, 10)
    embed = discord.Embed(title=f"🏆 {interaction.guild.name} 金持ちランキング", color=discord.Color.gold())
    if not rows:
        embed.description = "まだ誰もお金を持っていません。`/work` で稼ぎましょう！"
    else:
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, r in enumerate(rows):
            mark = medals[i] if i < 3 else f"`{i + 1}.`"
            lines.append(f"{mark} <@{r['user_id']}> — **{fmt(r['total'])}**")
        embed.description = "\n".join(lines)
    rank, total = bot.db.rank_of(interaction.guild_id, interaction.user.id)
    embed.set_footer(text=f"あなたの順位: {rank}位 ({fmt(total)}) / 順位は 現金＋銀行−借金 で決まります")
    await interaction.response.send_message(embed=embed)


# ===== レベル機能 =====
async def admin_guard(interaction: discord.Interaction) -> bool:
    if not is_admin(interaction):
        await interaction.response.send_message("管理者のみ実行できます。", ephemeral=True)
        return False
    return True


async def handle_level_up(message: discord.Message, new_level: int):
    """ロール報酬の付与と、レベルアップ通知"""
    guild = message.guild
    member = message.author
    settings = bot.db.get_level_settings(guild.id)

    granted: list[discord.Role] = []
    for role_id in bot.db.level_role_ids_up_to(guild.id, new_level):
        role = guild.get_role(role_id)
        if role is None or role in member.roles:
            continue
        try:
            await member.add_roles(role, reason=f"レベル{new_level}に到達")
            granted.append(role)
        except (discord.Forbidden, discord.HTTPException):
            log.warning("ロール %s (%s) を付与できませんでした", role.name, role.id)

    if settings["notify_mode"] == "off":
        return
    target = message.channel
    if settings["notify_mode"] == "channel" and settings["channel_id"]:
        ch = guild.get_channel(settings["channel_id"])
        if isinstance(ch, discord.abc.Messageable):
            target = ch

    text = f"🎉 {member.mention} がレベル **{new_level}** に上がりました！"
    if granted:
        text += "\n🎭 ロールを獲得: " + " ".join(r.mention for r in granted)
    try:
        await target.send(
            text, allowed_mentions=discord.AllowedMentions(users=[member], roles=False)
        )
    except (discord.Forbidden, discord.HTTPException):
        log.warning("レベルアップ通知を送信できませんでした (channel=%s)", getattr(target, "id", None))


def build_level_settings_embed(guild: discord.Guild) -> discord.Embed:
    s = bot.db.get_level_settings(guild.id)
    if s["notify_mode"] == "off":
        notify = "通知しない"
    elif s["notify_mode"] == "channel" and s["channel_id"]:
        notify = f"<#{s['channel_id']}>"
    else:
        notify = "メッセージを送ったチャンネル"
    rewards = bot.db.list_level_roles(guild.id)
    reward_text = "\n".join(f"Lv{r['level']} → <@&{r['role_id']}>" for r in rewards) or "未設定"

    embed = discord.Embed(title="⚙️ レベル設定", color=discord.Color.purple())
    embed.add_field(name="📢 レベルアップ通知", value=notify, inline=False)
    embed.add_field(name="🎭 ロール報酬", value=reward_text, inline=False)
    embed.set_footer(text=f"{XP_PER_LEVEL}XPで1レベルアップ / 指定レベル以上になると、そのロールがすべて付与されます")
    return embed


class NotifyChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(
            placeholder="通知を出すチャンネルを選択…",
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        if not await admin_guard(interaction):
            return
        channel_id = self.values[0].id
        bot.db.set_notify(interaction.guild_id, "channel", channel_id)
        await interaction.response.edit_message(
            embed=build_level_settings_embed(interaction.guild), view=self.view
        )
        ch = interaction.guild.get_channel(channel_id)
        if ch is not None and not ch.permissions_for(interaction.guild.me).send_messages:
            await interaction.followup.send(
                f"⚠️ Botが {ch.mention} にメッセージを送る権限を持っていません。権限を付与してください。",
                ephemeral=True,
            )


class RewardModal(discord.ui.Modal):
    def __init__(self, role: discord.Role, panel: "LevelSettingView"):
        super().__init__(title=f"ロール報酬: {role.name}"[:45])
        self.role = role
        self.panel = panel
        self.level_in = discord.ui.TextInput(
            label="このロールを付与するレベル", placeholder="例: 5", max_length=4
        )
        self.add_item(self.level_in)

    async def on_submit(self, interaction: discord.Interaction):
        if not await admin_guard(interaction):
            return
        guild = interaction.guild
        try:
            level = int(self.level_in.value.strip())
        except ValueError:
            await interaction.response.send_message("❌ レベルは数字で入力してください。", ephemeral=True)
            return
        if not 1 <= level <= 1000:
            await interaction.response.send_message("❌ レベルは1〜1000で入力してください。", ephemeral=True)
            return
        role = guild.get_role(self.role.id)
        if role is None or role.is_default() or role.managed:
            await interaction.response.send_message("❌ このロールは報酬に設定できません。", ephemeral=True)
            return
        try:
            bot.db.set_level_role(guild.id, level, role.id)
        except EconomyError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        text = f"✅ Lv{level} に到達したら {role.mention} を付与するよう設定しました。"
        if role >= guild.me.top_role:
            text += "\n⚠️ Botの最上位ロール以上の位置にあるロールです。Botのロールをこのロールより上に移動しないと付与できません。"
        await interaction.response.send_message(text, ephemeral=True)
        await self.panel.refresh()


class RewardRoleSelect(discord.ui.RoleSelect):
    def __init__(self, panel: "LevelSettingView"):
        super().__init__(placeholder="報酬にするロールを選択…")
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        if not await admin_guard(interaction):
            return
        await interaction.response.send_modal(RewardModal(self.values[0], self.panel))


class RewardRoleView(discord.ui.View):
    def __init__(self, panel: "LevelSettingView"):
        super().__init__(timeout=120)
        self.add_item(RewardRoleSelect(panel))


class DeleteRewardSelect(discord.ui.Select):
    def __init__(self, panel: "LevelSettingView", guild: discord.Guild, rewards: list[dict]):
        self.panel = panel
        options = []
        for r in rewards[:MAX_LEVEL_ROLES]:
            role = guild.get_role(r["role_id"])
            name = role.name if role else "(削除されたロール)"
            options.append(
                discord.SelectOption(label=f"Lv{r['level']} → {name}"[:100], value=str(r["role_id"]))
            )
        super().__init__(placeholder="削除するロール報酬を選択…", options=options)

    async def callback(self, interaction: discord.Interaction):
        if not await admin_guard(interaction):
            return
        bot.db.delete_level_role(interaction.guild_id, int(self.values[0]))
        await interaction.response.edit_message(content="🗑️ ロール報酬を削除しました。", view=None)
        await self.panel.refresh()


class DeleteRewardView(discord.ui.View):
    def __init__(self, panel: "LevelSettingView", guild: discord.Guild, rewards: list[dict]):
        super().__init__(timeout=120)
        self.add_item(DeleteRewardSelect(panel, guild, rewards))


class LevelSettingView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=600)
        self.source: discord.Interaction | None = None
        self.add_item(NotifyChannelSelect())

    async def refresh(self):
        if self.source:
            try:
                await self.source.edit_original_response(
                    embed=build_level_settings_embed(self.source.guild), view=self
                )
            except discord.HTTPException:
                pass

    @discord.ui.button(label="送信チャンネルに通知", style=discord.ButtonStyle.secondary, emoji="💬", row=1)
    async def same_channel_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not await admin_guard(interaction):
            return
        bot.db.set_notify(interaction.guild_id, "current", None)
        await interaction.response.edit_message(
            embed=build_level_settings_embed(interaction.guild), view=self
        )

    @discord.ui.button(label="通知オフ", style=discord.ButtonStyle.secondary, emoji="🔕", row=1)
    async def off_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not await admin_guard(interaction):
            return
        bot.db.set_notify(interaction.guild_id, "off", None)
        await interaction.response.edit_message(
            embed=build_level_settings_embed(interaction.guild), view=self
        )

    @discord.ui.button(label="ロール報酬を追加", style=discord.ButtonStyle.success, emoji="➕", row=1)
    async def add_role_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not await admin_guard(interaction):
            return
        await interaction.response.send_message(
            "報酬にするロールを選んでください。選んだあとにレベルを入力します。",
            view=RewardRoleView(self),
            ephemeral=True,
        )

    @discord.ui.button(label="ロール報酬を削除", style=discord.ButtonStyle.danger, emoji="🗑️", row=1)
    async def delete_role_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not await admin_guard(interaction):
            return
        rewards = bot.db.list_level_roles(interaction.guild_id)
        if not rewards:
            await interaction.response.send_message("削除できるロール報酬がありません。", ephemeral=True)
            return
        await interaction.response.send_message(
            view=DeleteRewardView(self, interaction.guild, rewards), ephemeral=True
        )


@bot.tree.command(name="level-setting", description="【管理者のみ】レベルアップ通知とロール報酬を設定します")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def level_setting(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ このコマンドは管理者のみ実行できます。", ephemeral=True)
        return
    view = LevelSettingView()
    await interaction.response.send_message(
        embed=build_level_settings_embed(interaction.guild), view=view, ephemeral=True
    )
    view.source = interaction


@bot.tree.command(name="level", description="レベルとXPを確認します")
@app_commands.describe(user="確認したい相手（省略すると自分）")
@app_commands.guild_only()
async def level_cmd(interaction: discord.Interaction, user: discord.Member | None = None):
    target = user or interaction.user
    row = bot.db.get_user(interaction.guild_id, target.id)
    xp = row["xp"]
    into = xp % XP_PER_LEVEL
    filled = into * 10 // XP_PER_LEVEL
    bar = "▰" * filled + "▱" * (10 - filled)
    embed = discord.Embed(
        description=f"次のレベルまで: {bar} {into}/{XP_PER_LEVEL}XP", color=discord.Color.purple()
    )
    embed.set_author(name=target.display_name, icon_url=target.display_avatar.url)
    embed.add_field(name="⭐ レベル", value=f"Lv {xp // XP_PER_LEVEL}")
    embed.add_field(name="累計XP", value=f"{xp:,}XP")
    embed.add_field(name="変換可能XP", value=f"{xp - row['xp_spent']:,}XP")
    embed.set_footer(text=f"XPランク: {bot.db.xp_rank(interaction.guild_id, target.id)}位")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="xp-convert",
    description=f"XPをお金に変換します（1XP = {fmt(XP_TO_MONEY)} / クールダウン{XP_CONVERT_COOLDOWN // 3600}時間）",
)
@app_commands.describe(amount="変換するXP（`all` で全部）")
@app_commands.guild_only()
async def xp_convert(interaction: discord.Interaction, amount: str):
    try:
        parsed = parse_amount(amount, "XP")
        used, money, cash, remaining = bot.db.convert_xp(
            interaction.guild_id, interaction.user.id, parsed
        )
    except EconomyError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return
    embed = discord.Embed(
        description=f"✨ **{used:,}XP** を **{fmt(money)}** に変換しました。",
        color=discord.Color.green(),
    )
    embed.add_field(name="💴 所持金", value=fmt(cash))
    embed.add_field(name="残りの変換可能XP", value=f"{remaining:,}XP")
    embed.set_footer(text="レベルは下がりません")
    await interaction.response.send_message(embed=embed)


# ===== カジノ(Discordの画面) =====
def resolve_bet(guild_id: int, user_id: int, text: str) -> int:
    """賭け金の入力を検証して金額にする。`all` なら所持金すべて"""
    parsed = parse_amount(text, "賭け金")
    cash = bot.db.get_user(guild_id, user_id)["cash"]
    bet = cash if parsed is None else parsed
    if bet < MIN_BET:
        raise EconomyError(f"賭け金は{fmt(MIN_BET)}以上で指定してください。(所持金: {fmt(cash)})")
    if bet > cash:
        raise EconomyError(f"所持金が足りません。(所持金: {fmt(cash)})")
    return bet


class CasinoView(discord.ui.View):
    """カジノ用の共通部分: コマンドを実行した本人だけが操作でき、時間切れでボタンを無効化する"""

    def __init__(self, owner_id: int, bet: int, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.bet = bet
        self.source: discord.Interaction | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "これは他の人のゲームです。自分で `/roulette` `/slots` `/blackjack` `/poker` を実行してください。",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.source:
            try:
                await self.source.edit_original_response(view=self)
            except discord.HTTPException:
                pass

    def _add_button(self, label: str, style: discord.ButtonStyle, callback, emoji: str | None = None, row: int | None = None):
        button = discord.ui.Button(label=label, style=style, emoji=emoji, row=row)
        button.callback = callback
        self.add_item(button)

    async def _update(self, interaction: discord.Interaction | None):
        """画面(embed と ボタン)を更新する。interaction が無い(時間切れ)ときは元のメッセージを直接編集する"""
        if interaction is not None:
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
        elif self.source is not None:
            try:
                await self.source.edit_original_response(embed=self.build_embed(), view=self)
            except discord.HTTPException:
                pass


# --- ルーレット ---
def build_roulette_embed(bet: int, result: dict | None = None) -> discord.Embed:
    embed = discord.Embed(title="🎡 ルーレット", color=discord.Color.red())
    head = f"賭け金: **{fmt(bet)}**\n"
    if result is None:
        embed.description = (
            head + "下のメニューから賭け先を選ぶと、**その瞬間に賭けが確定**します。"
            "（選ぶまでお金は引かれません）"
        )
    else:
        if result["win"]:
            outcome = (
                f"🎉 **的中！** {result['label']} ×{result['mult']}\n"
                f"**+{fmt(result['payout'] - bet)}**（受取 {fmt(result['payout'])}）"
            )
        else:
            outcome = f"💸 **ハズレ…** （{result['label']}） **-{fmt(bet)}**"
        embed.description = (
            f"{head}\n出目: {roulette_color(result['n'])} **{result['n']}**\n{outcome}\n\n"
            "続けて遊ぶ場合は、もう一度メニューから選んでください。"
        )
        embed.add_field(name="💴 所持金", value=fmt(result["cash"]), inline=False)
    lines = [
        f"{label}　×{mult}　→ 当たれば {fmt(bet * mult)}　（確率 {fmt_pct(count / 37)}）"
        for label, mult, count in ROULETTE_BETS.values()
    ]
    embed.add_field(name="📋 倍率表（受取額は賭け金込み）", value="\n".join(lines), inline=False)
    embed.set_footer(text="0（緑）は数字指定以外すべてハズレ / 還元率 97.3%")
    return embed


async def run_roulette(
    interaction: discord.Interaction, view: "RouletteView", key: str, number: int | None = None
):
    n = random.randint(0, 36)
    label, mult, _count = ROULETTE_BETS[key]
    if key == "number":
        label = f"🎯 数字 {number}"
    win = roulette_wins(key, n, number)
    payout = view.bet * mult if win else 0
    try:
        cash = bot.db.play(interaction.guild_id, interaction.user.id, view.bet, payout)
    except EconomyError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return
    result = {"n": n, "label": label, "mult": mult, "win": win, "payout": payout, "cash": cash}
    await interaction.response.edit_message(embed=build_roulette_embed(view.bet, result), view=view)


class NumberModal(discord.ui.Modal):
    def __init__(self, view: "RouletteView"):
        super().__init__(title="賭ける数字を指定")
        self.view = view
        self.number_in = discord.ui.TextInput(label="数字 (0〜36)", max_length=2, placeholder="例: 17")
        self.add_item(self.number_in)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            number = int(self.number_in.value.strip())
        except ValueError:
            number = -1
        if not 0 <= number <= 36:
            await interaction.response.send_message("❌ 0〜36の数字を入力してください。", ephemeral=True)
            return
        await run_roulette(interaction, self.view, "number", number)


class RouletteSelect(discord.ui.Select):
    def __init__(self, bet: int):
        options = [
            discord.SelectOption(
                label=f"{label} ×{mult}",
                description=f"当たれば{fmt(bet * mult)} / 確率{fmt_pct(count / 37)}"[:100],
                value=key,
            )
            for key, (label, mult, count) in ROULETTE_BETS.items()
        ]
        super().__init__(placeholder="賭け先を選択…（選ぶと賭けが確定）", options=options)

    async def callback(self, interaction: discord.Interaction):
        view: RouletteView = self.view
        key = self.values[0]
        if key == "number":
            await interaction.response.send_modal(NumberModal(view))
            return
        await run_roulette(interaction, view, key)


class RouletteView(CasinoView):
    def __init__(self, owner_id: int, bet: int):
        super().__init__(owner_id, bet)
        self.add_item(RouletteSelect(bet))


# --- スロット ---
def build_slots_embed(bet: int, result: dict | None = None) -> discord.Embed:
    embed = discord.Embed(title="🎰 スロット", color=discord.Color.gold())
    head = f"賭け金: **{fmt(bet)}**\n"
    if result is None:
        embed.description = head + "「回す」を押すと賭け金が引かれて回転します。（押すまでお金は引かれません）"
    else:
        reels = " | ".join(result["reels"])
        if result["mult"] > 0:
            outcome = (
                f"🎉 **当たり！ ×{result['mult']}**\n"
                f"**+{fmt(result['payout'] - bet)}**（受取 {fmt(result['payout'])}）"
            )
        else:
            outcome = f"💸 **ハズレ…** **-{fmt(bet)}**"
        embed.description = f"{head}\n# [ {reels} ]\n{outcome}"
        embed.add_field(name="💴 所持金", value=fmt(result["cash"]), inline=False)
    total = sum(w for _s, w, _m in SLOT_SYMBOLS)
    lines = []
    for s, w, m in reversed(SLOT_SYMBOLS):
        lines.append(f"{s}{s}{s}　×{m}　→ {fmt(bet * m)}　（確率 {fmt_pct((w / total) ** 3)}）")
    pc = SLOT_SYMBOLS[0][1] / total
    pair_p = 3 * pc * pc * (1 - pc)
    lines.append(
        f"🍒🍒（ちょうど2つ）　×{SLOT_CHERRY_PAIR}　→ {fmt(bet * SLOT_CHERRY_PAIR)}　（確率 {fmt_pct(pair_p)}）"
    )
    embed.add_field(name="📋 配当表（受取額は賭け金込み）", value="\n".join(lines), inline=False)
    embed.set_footer(text=f"上記以外はハズレ / 還元率 {fmt_pct(slot_rtp())}")
    return embed


class SlotsView(CasinoView):
    def __init__(self, owner_id: int, bet: int):
        super().__init__(owner_id, bet)
        self.spin_btn = discord.ui.Button(label="回す", emoji="🎰", style=discord.ButtonStyle.success)
        self.spin_btn.callback = self.spin
        self.add_item(self.spin_btn)

    async def spin(self, interaction: discord.Interaction):
        reels = slot_spin()
        mult = slot_multiplier(reels)
        payout = self.bet * mult
        try:
            cash = bot.db.play(interaction.guild_id, interaction.user.id, self.bet, payout)
        except EconomyError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        self.spin_btn.label = "もう一度回す"
        result = {"reels": reels, "mult": mult, "payout": payout, "cash": cash}
        await interaction.response.edit_message(embed=build_slots_embed(self.bet, result), view=self)


# --- ブラックジャック ---
class BlackjackView(CasinoView):
    def __init__(self, owner_id: int, bet: int):
        super().__init__(owner_id, bet, timeout=180)
        self.guild_id = 0
        self.phase = "intro"  # intro / playing / done
        self.deck: list = []
        self.player: list = []
        self.dealer: list = []
        self.result: tuple[str, int, int] | None = None  # (勝敗, 受取額, 所持金)
        self._set_buttons()

    def _set_buttons(self):
        self.clear_items()
        if self.phase == "intro":
            self._add_button("スタート（賭け金を支払う）", discord.ButtonStyle.success, self.on_start, "▶️")
        elif self.phase == "playing":
            self._add_button("ヒット", discord.ButtonStyle.primary, self.on_hit, "🃏")
            self._add_button("スタンド", discord.ButtonStyle.secondary, self.on_stand, "✋")
        else:
            self._add_button("もう一度（同じ賭け金）", discord.ButtonStyle.primary, self.on_start, "🔁")

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title="🃏 ブラックジャック", color=discord.Color.dark_green())
        bet = self.bet
        table = (
            f"勝ち　×2　→ {fmt(bet * 2)}\n"
            f"ブラックジャック（最初の2枚で21）　×2.5　→ {fmt(bet * 5 // 2)}\n"
            f"引き分け　×1　→ {fmt(bet)}（賭け金が戻る）\n"
            "負け　×0"
        )
        if self.phase == "intro":
            embed.description = (
                f"賭け金: **{fmt(bet)}**\n「スタート」を押すと賭け金が引かれてゲームが始まります。"
                "（押すまでお金は引かれません）"
            )
            embed.add_field(name="📋 倍率表（受取額は賭け金込み）", value=table, inline=False)
            embed.add_field(
                name="ルール",
                value=(
                    "手札の合計を21に近づけます（超えると負け）。J・Q・Kは10、Aは1か11。\n"
                    "ディーラーは17以上になるまで引きます。ヒットとスタンドのみ（ダブル・スプリットなし）。"
                ),
                inline=False,
            )
            return embed

        playing = self.phase == "playing"
        embed.add_field(
            name="ディーラー",
            value=hand_text(self.dealer, hide_second=playing)
            + ("" if playing else f"　（合計 {hand_value(self.dealer)}）"),
            inline=False,
        )
        embed.add_field(
            name="あなた", value=f"{hand_text(self.player)}　（合計 {hand_value(self.player)}）", inline=False
        )
        if playing:
            embed.description = f"賭け金: **{fmt(bet)}**　「ヒット」でもう1枚、「スタンド」で勝負します。"
            embed.set_footer(text="勝ち ×2 / ブラックジャック ×2.5 / 引き分け ×1")
        else:
            outcome, payout, cash = self.result
            busted = hand_value(self.player) > 21
            texts = {
                "blackjack": f"🎉 **ブラックジャック！** ×2.5　**+{fmt(payout - bet)}**",
                "win": f"🎉 **勝ち！** ×2　**+{fmt(payout - bet)}**",
                "push": "🤝 **引き分け。** 賭け金が戻ります。",
                "lose": f"💸 **{'バースト…' if busted else '負け…'}**　**-{fmt(bet)}**",
            }
            embed.description = f"賭け金: **{fmt(bet)}**\n{texts[outcome]}"
            embed.add_field(name="💴 所持金", value=fmt(cash), inline=False)
            embed.color = (
                discord.Color.green() if outcome in ("win", "blackjack")
                else discord.Color.light_grey() if outcome == "push"
                else discord.Color.red()
            )
        return embed

    async def _finish(self, interaction: discord.Interaction | None, timed_out: bool = False):
        """ディーラーの手番と精算。二重に精算されないよう、先に phase を done にする"""
        if self.phase == "done":
            return
        self.phase = "done"
        natural = is_blackjack(self.player) or is_blackjack(self.dealer)
        if hand_value(self.player) > 21:
            outcome = "lose"
        else:
            if not natural:
                dealer_play(self.dealer, self.deck)
            outcome = blackjack_outcome(self.player, self.dealer)
        payout = blackjack_payout(self.bet, outcome)
        user_id = self.owner_id
        if payout:
            cash = bot.db.bet_give(self.guild_id, user_id, payout)
        else:
            cash = bot.db.get_user(self.guild_id, user_id)["cash"]
        self.result = (outcome, payout, cash)
        self._set_buttons()
        if timed_out:  # 時間切れの後はボタンが使えないので無効化しておく
            for child in self.children:
                child.disabled = True
        await self._update(interaction)

    async def on_start(self, interaction: discord.Interaction):
        if self.phase == "playing":
            await interaction.response.defer()
            return
        try:
            bot.db.bet_take(interaction.guild_id, interaction.user.id, self.bet)
        except EconomyError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        self.guild_id = interaction.guild_id
        self.deck = new_deck()
        self.player = [self.deck.pop(), self.deck.pop()]
        self.dealer = [self.deck.pop(), self.deck.pop()]
        self.result = None
        self.phase = "playing"
        if is_blackjack(self.player) or is_blackjack(self.dealer):
            await self._finish(interaction)
            return
        self._set_buttons()
        await self._update(interaction)

    async def on_hit(self, interaction: discord.Interaction):
        if self.phase != "playing":
            await interaction.response.defer()
            return
        self.player.append(self.deck.pop())
        if hand_value(self.player) >= 21:  # バーストまたは21で自動的にスタンド
            await self._finish(interaction)
        else:
            await self._update(interaction)

    async def on_stand(self, interaction: discord.Interaction):
        if self.phase != "playing":
            await interaction.response.defer()
            return
        await self._finish(interaction)

    async def on_timeout(self):
        if self.phase == "playing":
            await self._finish(None, timed_out=True)  # 放置された場合は自動でスタンドして精算する
        else:
            await super().on_timeout()


# --- ポーカー(ビデオポーカー) ---
class PokerView(CasinoView):
    def __init__(self, owner_id: int, bet: int):
        super().__init__(owner_id, bet, timeout=180)
        self.guild_id = 0
        self.phase = "intro"  # intro / playing / done
        self.deck: list = []
        self.hand: list = []
        self.held: set[int] = set()
        self.result: tuple[str | None, int, int, int] | None = None  # (役, 倍率, 受取額, 所持金)
        self._set_buttons()

    def _set_buttons(self):
        self.clear_items()
        if self.phase == "intro":
            self._add_button("ディール（賭け金を支払う）", discord.ButtonStyle.success, self.on_deal, "🃏")
        elif self.phase == "playing":
            for i, (rank, suit) in enumerate(self.hand):  # カードを押すとホールド(残す)/解除
                self._add_button(
                    f"{rank}{suit}",
                    discord.ButtonStyle.success if i in self.held else discord.ButtonStyle.secondary,
                    self._make_toggle(i),
                    row=0,
                )
            self._add_button("ドロー（残り以外を引き換える）", discord.ButtonStyle.primary, self.on_draw, "🔄", row=1)
        else:
            self._add_button("もう一度（同じ賭け金）", discord.ButtonStyle.primary, self.on_deal, "🔁")

    def _make_toggle(self, index: int):
        async def toggle(interaction: discord.Interaction):
            if self.phase != "playing":
                await interaction.response.defer()
                return
            self.held.symmetric_difference_update({index})
            self._set_buttons()
            await self._update(interaction)

        return toggle

    def _paytable_text(self) -> str:
        return "\n".join(
            f"{name}　×{mult}　→ {fmt(self.bet * mult)}" + ("（賭け金が戻る）" if mult == 1 else "")
            for _key, name, mult in POKER_PAYTABLE
        )

    def _hand_lines(self, mark_hold: bool) -> str:
        nums = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
        return "\n".join(
            f"{nums[i]} **{rank}{suit}**" + ("　✋ ホールド" if mark_hold and i in self.held else "")
            for i, (rank, suit) in enumerate(self.hand)
        )

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title="🃏 ポーカー（ジャックス・オア・ベター）", color=discord.Color.dark_blue())
        bet = self.bet
        if self.phase == "intro":
            embed.description = (
                f"賭け金: **{fmt(bet)}**\n「ディール」を押すと賭け金が引かれて、5枚が配られます。"
                "（押すまでお金は引かれません）"
            )
            embed.add_field(name="📋 配当表（受取額は賭け金込み）", value=self._paytable_text(), inline=False)
            embed.add_field(
                name="ルール",
                value=(
                    "5枚が配られたら、残したいカードを押して✋ホールドし、「ドロー」で残りを1回だけ引き換えます。\n"
                    "完成した役に応じた配当がもらえます。役が無ければ賭け金は没収です。"
                ),
                inline=False,
            )
            embed.set_footer(text="最適にプレイした場合の還元率: 約97.3%（8/5 ジャックス・オア・ベター）")
            return embed

        if self.phase == "playing":
            now = POKER_NAMES.get(poker_hand_key(self.hand))
            embed.description = (
                f"賭け金: **{fmt(bet)}**\n残したいカードを押して✋ホールド → 「ドロー」で残りを引き換えます。\n\n"
                f"{self._hand_lines(mark_hold=True)}\n\n"
                f"現在の役: **{now or '役なし'}**"
            )
            embed.add_field(name="📋 配当表", value=self._paytable_text(), inline=False)
            return embed

        key, mult, payout, cash = self.result
        if mult > 1:
            outcome = f"🎉 **{POKER_NAMES[key]}！** ×{mult}　**+{fmt(payout - bet)}**（受取 {fmt(payout)}）"
            embed.color = discord.Color.green()
        elif mult == 1:
            outcome = f"↩️ **{POKER_NAMES[key]}** ×1　賭け金が戻ります。"
            embed.color = discord.Color.light_grey()
        else:
            outcome = f"💸 **役なし…**　**-{fmt(bet)}**"
            embed.color = discord.Color.red()
        embed.description = f"賭け金: **{fmt(bet)}**\n\n{self._hand_lines(mark_hold=False)}\n\n{outcome}"
        embed.add_field(name="💴 所持金", value=fmt(cash), inline=False)
        embed.add_field(name="📋 配当表", value=self._paytable_text(), inline=False)
        return embed

    async def _finish(self, interaction: discord.Interaction | None, draw: bool = True, timed_out: bool = False):
        """ドロー(引き換え)と精算。二重に精算されないよう、先に phase を done にする"""
        if self.phase != "playing":
            return
        self.phase = "done"
        if draw:
            for i in range(5):
                if i not in self.held:
                    self.hand[i] = self.deck.pop()
        key = poker_hand_key(self.hand)
        mult = poker_multiplier(key)
        payout = self.bet * mult
        if payout:
            cash = bot.db.bet_give(self.guild_id, self.owner_id, payout)
        else:
            cash = bot.db.get_user(self.guild_id, self.owner_id)["cash"]
        self.result = (key, mult, payout, cash)
        self._set_buttons()
        if timed_out:
            for child in self.children:
                child.disabled = True
        await self._update(interaction)

    async def on_deal(self, interaction: discord.Interaction):
        if self.phase == "playing":
            await interaction.response.defer()
            return
        try:
            bot.db.bet_take(interaction.guild_id, interaction.user.id, self.bet)
        except EconomyError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        self.guild_id = interaction.guild_id
        self.deck = new_deck()
        self.hand = [self.deck.pop() for _ in range(5)]
        self.held = set()
        self.result = None
        self.phase = "playing"
        self._set_buttons()
        await self._update(interaction)

    async def on_draw(self, interaction: discord.Interaction):
        if self.phase != "playing":
            await interaction.response.defer()
            return
        await self._finish(interaction)

    async def on_timeout(self):
        if self.phase == "playing":
            await self._finish(None, draw=False, timed_out=True)  # 放置された場合は、引き換えずに今の5枚で精算する
        else:
            await super().on_timeout()


async def start_casino(interaction: discord.Interaction, bet_text: str, make_view, make_embed):
    try:
        bet = resolve_bet(interaction.guild_id, interaction.user.id, bet_text)
    except EconomyError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return
    view = make_view(interaction.user.id, bet)
    if hasattr(view, "guild_id"):  # ブラックジャックとポーカーは、精算のために必要
        view.guild_id = interaction.guild_id
    await interaction.response.send_message(embed=make_embed(view), view=view)
    view.source = interaction


@bot.tree.command(name="roulette", description="ルーレットで賭けます（倍率は賭ける前に確認できます）")
@app_commands.describe(bet=f"賭け金（{MIN_BET}以上。`all` で全額）")
@app_commands.guild_only()
async def roulette_cmd(interaction: discord.Interaction, bet: str):
    await start_casino(interaction, bet, RouletteView, lambda v: build_roulette_embed(v.bet))


@bot.tree.command(name="slots", description="スロットで賭けます（配当表は賭ける前に確認できます）")
@app_commands.describe(bet=f"賭け金（{MIN_BET}以上。`all` で全額）")
@app_commands.guild_only()
async def slots_cmd(interaction: discord.Interaction, bet: str):
    await start_casino(interaction, bet, SlotsView, lambda v: build_slots_embed(v.bet))


@bot.tree.command(name="blackjack", description="ブラックジャックで賭けます（倍率は賭ける前に確認できます）")
@app_commands.describe(bet=f"賭け金（{MIN_BET}以上。`all` で全額）")
@app_commands.guild_only()
async def blackjack_cmd(interaction: discord.Interaction, bet: str):
    await start_casino(interaction, bet, BlackjackView, lambda v: v.build_embed())


@bot.tree.command(name="poker", description="ポーカーで賭けます（配当表は賭ける前に確認できます）")
@app_commands.describe(bet=f"賭け金（{MIN_BET}以上。`all` で全額）")
@app_commands.guild_only()
async def poker_cmd(interaction: discord.Interaction, bet: str):
    await start_casino(interaction, bet, PokerView, lambda v: v.build_embed())


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    log.exception("Command error", exc_info=error)
    msg = "❌ エラーが発生しました。しばらくしてからもう一度お試しください。"
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("環境変数 DISCORD_TOKEN が設定されていません。")
    bot.run(token)
