# 経済機能 Discord Bot

`/work` で稼いで、`/shop` で買い物ができる経済Botです。

## コマンド

| コマンド | 内容 |
| --- | --- |
| `/work` | 1000〜3000円をランダムで獲得（クールダウン1時間） |
| `/bank 預ける/引き出す 金額` | 銀行に預ける・引き出す（金額に `all` で全額） |
| `/shop` | ショップを埋め込みパネルで表示。メニューから購入 |
| `/shop-setting` | 【管理者のみ】アイテムの追加・変更・削除 |
| `/leaderboard` | サーバー内の資産ランキング（現金＋銀行） |
| `/balance` | 所持金・銀行残高の確認 |
| `/inventory` | 購入したアイテムの確認 |

## Render へのデプロイ

- Runtime: Python 3
- Build Command: `pip install -r requirements.txt`
- Start Command: `python bot.py`
- Service Type: Web Service（`PORT` で待ち受ける HTTP サーバーを内蔵。`/` と `/healthz` が `OK` を返します）
- 環境変数
  - `DISCORD_TOKEN` : Botのトークン（必須）
  - `DB_PATH` : SQLiteの保存先（省略時 `economy.db`）
  - `DEV_GUILD_ID` : 指定するとそのサーバーにコマンドが即時反映（開発用・任意）

### データの永続化について

RenderのWeb Serviceはデフォルトだと再デプロイ・再起動でファイルが消えます。
所持金などを保持するには、Persistent Disk（有料）を付けて `DB_PATH=/data/economy.db` のようにディスク内を指定するか、外部DB（PostgreSQLなど）へ移行してください。

### Free プランのスリープについて

Renderの無料Web Serviceは15分アクセスが無いとスリープします。
UptimeRobot などで定期的に `https://<サービス名>.onrender.com/healthz` を叩くとBotが落ちにくくなります。

## Discord Developer Portal

- Botを作成し、`bot` と `applications.commands` スコープで招待してください。
- 特権インテントは不要です。
- ショップのロール付与機能を使う場合は、Botに「ロールの管理」権限を与え、Botのロールを付与対象ロールより上に配置してください。
