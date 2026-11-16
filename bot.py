import logging
import os
import random
import sqlite3
import time

import discord
from aiohttp import web
from discord import app_commands

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("economy-bot")

# ===== 設定 =====
CURRENCY = "円"
WORK_COOLDOWN = 60 * 60  # 1時間
WORK_MIN, WORK_MAX = 1000, 3000
MAX_ITEMS = 25  # セレクトメニュー/埋め込みの上限に合わせる
DB_PATH = os.environ.get("DB_PATH", "economy.db")
DEV_GUILD_ID = os.environ.get("DEV_GUILD_ID")  # 指定するとそのサーバーに即時反映

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


def fmt(n: int) -> str:
    return f"{n:,}{CURRENCY}"


class EconomyError(Exception):
    """ユーザーに表示するエラー"""


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
                """
            )

    # --- ユーザー ---
    def _ensure(self, g: int, u: int) -> sqlite3.Row:
        self.conn.execute(
            "INSERT OR IGNORE INTO users (guild_id, user_id) VALUES (?, ?)", (g, u)
        )
        return self.conn.execute(
            "SELECT * FROM users WHERE guild_id=? AND user_id=?", (g, u)
        ).fetchone()

    def get_user(self, g: int, u: int) -> dict:
        with self.conn:
            return dict(self._ensure(g, u))

    def work(self, g: int, u: int):
        """(成功したか, 稼いだ額 or 次に働ける時刻)"""
        now = int(time.time())
        with self.conn:
            row = self._ensure(g, u)
            ready_at = row["last_work"] + WORK_COOLDOWN
            if now < ready_at:
                return False, ready_at
            amount = random.randint(WORK_MIN, WORK_MAX)
            self.conn.execute(
                "UPDATE users SET cash=cash+?, last_work=? WHERE guild_id=? AND user_id=?",
                (amount, now, g, u),
            )
            return True, amount

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
        return self.conn.execute(
            "SELECT user_id, cash, bank, cash+bank AS total FROM users "
            "WHERE guild_id=? AND cash+bank>0 ORDER BY total DESC LIMIT ?",
            (g, limit),
        ).fetchall()

    def rank_of(self, g: int, u: int):
        row = self.get_user(g, u)
        total = row["cash"] + row["bank"]
        higher = self.conn.execute(
            "SELECT COUNT(*) FROM users WHERE guild_id=? AND cash+bank>?", (g, total)
        ).fetchone()[0]
        return higher + 1, total

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


# ===== Bot本体 =====
class EconomyBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.db = Database(DB_PATH)

    async def setup_hook(self):
        await start_web_server()
        if DEV_GUILD_ID:
            guild = discord.Object(id=int(DEV_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id)


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


def parse_amount(text: str):
    """'all'/'全額' なら None、数値ならint。不正なら EconomyError"""
    s = text.strip().replace(",", "").replace("円", "").lower()
    if s in ("all", "max", "全額", "全部", "すべて"):
        return None
    try:
        value = int(s)
    except ValueError:
        raise EconomyError("金額は数字、または `all`（全額）で指定してください。")
    if value <= 0:
        raise EconomyError("金額は1以上で指定してください。")
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
            label="価格（円）", max_length=12, default=str(item["price"]) if item else None
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
    ok, value = bot.db.work(interaction.guild_id, interaction.user.id)
    if not ok:
        await interaction.response.send_message(
            f"⏳ まだ疲れています。次に働けるのは <t:{value}:R> です。", ephemeral=True
        )
        return
    embed = discord.Embed(
        description=f"{random.choice(WORK_MESSAGES)} **{fmt(value)}** 稼ぎました！",
        color=discord.Color.green(),
    )
    embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
    await interaction.response.send_message(embed=embed)


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
    embed.set_footer(text=f"あなたの順位: {rank}位 ({fmt(total)})")
    await interaction.response.send_message(embed=embed)


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
