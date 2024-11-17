import discord
from discord.ext import commands, tasks
from discord.ui import Button, View, Select
import asyncio
from datetime import datetime, timedelta
from sqlalchemy import create_engine, Column, Integer, String, desc
from sqlalchemy.ext.automap import automap_base
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker
from sqlalchemy import case
import logging
import atexit

# データベースの設定
db_path = 'db/shadowverse_bridge.db'
engine = create_engine(f'sqlite:///{db_path}', echo=True)
Base = automap_base()
Base.prepare(engine, reflect=True)

# マッピングされたクラスの取得
User = Base.classes.user  # テーブル名が 'user' だと仮定
Class = Base.classes.deck_class
MatchHistory = Base.classes.match_history  # テーブル名が 'match_history' だと仮定
Season = Base.classes.season  # テーブル名が 'season' だと仮定
UserSeasonRecord = Base.classes.user_season_record

session = Session(engine)
valid_classes = [cls.class_name for cls in session.query(Class.class_name).all()]

current_season_name = None

# 現在のシーズン名を取得
def get_current_season_name(session):
    current_season = session.query(Season).filter(Season.end_date == None).order_by(desc(Season.id)).first()
    if current_season:
        return current_season.season_name
    return None

def get_current_season_id(session):
    current_season = session.query(Season).filter(Season.end_date == None).order_by(desc(Season.id)).first()
    if current_season:
        return current_season.id
    return None

current_season_name = get_current_season_name(session)

current_season_id = get_current_season_id(session)

class RankingView(discord.ui.View):
    def __init__(self, session):
        super().__init__(timeout=None)
        self.session = session
        self.add_item(discord.ui.Button(label="連勝数ランキング", style=discord.ButtonStyle.primary, custom_id="win_streak_ranking"))
        self.add_item(discord.ui.Button(label="勝率ランキング", style=discord.ButtonStyle.primary, custom_id="win_rate_ranking"))
        # レートランキングのボタンは削除

        # リクエストキューと処理タスク
        self.request_queue = asyncio.Queue()
        self.processing_task = asyncio.create_task(self.process_queue())

        # ランキングデータのキャッシュ
        self.cache_expiry = 300  # キャッシュの有効期限（秒）
        self.cached_rankings = {}
        self.cache_lock = asyncio.Lock()

        # セマフォで同時リクエストを制限
        self.semaphore = asyncio.Semaphore(5)

    # リクエストキューを処理するタスク
    async def process_queue(self):
        while True:
            requests = []
            while not self.request_queue.empty():
                requests.append(await self.request_queue.get())
            if requests:
                await asyncio.gather(*(self.handle_request(interaction) for interaction in requests))
            await asyncio.sleep(0.1)

    # ボタンが押されたときの処理
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        await interaction.response.defer(ephemeral=True)
        await self.request_queue.put(interaction)
        return True

    # リクエストを処理
    async def handle_request(self, interaction: discord.Interaction):
        async with self.semaphore:
            try:
                custom_id = interaction.custom_id
                if custom_id == "win_streak_ranking":
                    await self.show_win_streak_ranking(interaction)
                elif custom_id == "win_rate_ranking":
                    await self.show_win_rate_ranking(interaction)
            except Exception as e:
                print(f"Error handling request: {e}")

    # キャッシュを取得または更新
    async def get_cached_ranking(self, ranking_type):
        async with self.cache_lock:
            now = datetime.now()
            cache = self.cached_rankings.get(ranking_type)
            if cache and (now - cache['timestamp']).total_seconds() < self.cache_expiry:
                return cache['data']
            else:
                # キャッシュがないか期限切れの場合、データを再取得
                data = await self.fetch_ranking_data(ranking_type)
                self.cached_rankings[ranking_type] = {'data': data, 'timestamp': now}
                return data

    # ランキングデータを取得
    async def fetch_ranking_data(self, ranking_type):
        if ranking_type == "win_streak":
            ranking = self.session.query(User).filter(User.latest_season_matched == True).order_by(desc(User.max_win_streak)).limit(100).all()
            return ranking
        elif ranking_type == "win_rate":
            users = self.session.query(User).filter(User.latest_season_matched == True).all()
            ranking_with_win_rate = []
            for user in users:
                win_count = user.win_count
                total_matches = user.total_matches
                loss_count = user.loss_count
                if total_matches >= 50:
                    win_rate = (win_count / total_matches) * 100
                    ranking_with_win_rate.append((user, win_rate, win_count, loss_count))
            ranking_with_win_rate.sort(key=lambda x: x[1], reverse=True)
            return ranking_with_win_rate[:16]
        elif ranking_type == "rating":
            effective_rating = case(
                (User.stayed_rating > User.rating, User.stayed_rating),
                else_=User.rating
            ).label('effective_rating')
            ranking = self.session.query(
                User.user_name,
                effective_rating,
                User.rating,
                User.stayed_rating
            ).filter(User.latest_season_matched == True).order_by(desc('effective_rating')).limit(100).all()
            return ranking

    # 連勝数ランキングを表示
    async def show_win_streak_ranking(self, interaction: discord.Interaction):
        ranking = await self.get_cached_ranking("win_streak")
        current_season_name = get_current_season_name(self.session)
        embed = discord.Embed(title=f"【{current_season_name}】連勝数ランキング", color=discord.Color.red())
        await self.send_ranking_embed(embed, ranking, interaction=interaction, ranking_type="win_streak")

    # 勝率ランキングを表示
    async def show_win_rate_ranking(self, interaction: discord.Interaction):
        ranking_with_win_rate = await self.get_cached_ranking("win_rate")
        current_season_name = get_current_season_name(self.session)
        embed = discord.Embed(title=f"【{current_season_name}】勝率ランキングTOP16", color=discord.Color.green())
        await self.send_ranking_embed(embed, ranking_with_win_rate, interaction=interaction, ranking_type="win_rate")

    # レートランキングを表示（チャンネルに送信）
    async def show_rate_ranking(self, channel: discord.TextChannel):
        ranking = await self.get_cached_ranking("rating")
        current_season_name = get_current_season_name(self.session)
        embed = discord.Embed(title=f"【{current_season_name}】レーティングランキング", color=discord.Color.blue())
        await self.send_ranking_embed(embed, ranking, channel=channel, ranking_type="rating")

    # Embedを送信
    async def send_ranking_embed(self, embed, ranking, interaction=None, channel=None, ranking_type="win_streak"):
        messages = []
        for i, record in enumerate(ranking, start=1):
            if ranking_type == "win_streak":
                embed.add_field(name=f"**``` {i}位 ```**", value=f"{record.user_name} - 連勝数 : {record.max_win_streak}", inline=False)
            elif ranking_type == "win_rate":
                user, win_rate, win_count, loss_count = record
                embed.add_field(name=f"**``` {i}位 ```**", value=f"{user.user_name} - 勝率 : {win_rate:.2f}% ({win_count}勝 {loss_count}敗)", inline=False)
            elif ranking_type == "rating":
                user_name, effective_rating_value, rating_value, stayed_rating_value = record
                rounded_rating = round(effective_rating_value or 1500, 3)
                rate_display = f"{rounded_rating} (stayed)" if stayed_rating_value and stayed_rating_value > rating_value else f"{rounded_rating}"
                embed.add_field(name=f"**``` {i}位 ```**", value=f"{user_name} - レート : {rate_display}", inline=False)
            if len(embed.fields) == 25:
                if interaction:
                    message = await interaction.followup.send(embed=embed, ephemeral=True)
                elif channel:
                    message = await channel.send(embed=embed)
                messages.append(message)
                embed.clear_fields()

        if len(embed.fields) > 0:
            if interaction:
                message = await interaction.followup.send(embed=embed, ephemeral=True)
            elif channel:
                message = await channel.send(embed=embed)
            messages.append(message)

        # メッセージを削除するタスクを別で実行（必要に応じて）
        if interaction:
            asyncio.create_task(self.delete_messages_after_delay(messages))

    # メッセージを一定時間後に削除
    async def delete_messages_after_delay(self, messages):
        await asyncio.sleep(300)
        for msg in messages:
            try:
                await msg.delete()
            except discord.errors.NotFound:
                pass



#過去シーズン関連
class RankingButtonView(discord.ui.View):
    def __init__(self, session):
        super().__init__(timeout=None)
        self.session = session
        # レーティング、連勝数、勝率のボタンを追加
        self.add_item(RankingButton(session, "レーティングランキング", "rate"))
        self.add_item(RankingButton(session, "連勝数ランキング", "win_streak"))
        self.add_item(RankingButton(session, "勝率ランキング", "win_rate"))


class RankingButton(discord.ui.Button):
    def __init__(self, session, label, ranking_type):
        super().__init__(label=label, style=discord.ButtonStyle.primary)
        self.ranking_type = ranking_type
        self.session = session

    async def callback(self, interaction: discord.Interaction):
        # ユーザーがボタンを押したらシーズン選択ビューを表示
        view = PastRankingSelectView(self.session, self.ranking_type)
        await interaction.response.send_message("シーズンを選択してください:", view=view, ephemeral=True)

class PastRankingSelectView(discord.ui.View):
    def __init__(self, session, ranking_type):
        super().__init__(timeout=None)
        self.session = session
        self.add_item(PastRankingSelect(session, ranking_type))

class PastRankingSelect(discord.ui.Select):
    def __init__(self, session, ranking_type):
        self.session = session
        self.ranking_type = ranking_type

        # 過去のシーズンを取得
        seasons = self.session.query(Season).filter(Season.end_date.isnot(None)).order_by(Season.id.desc()).all()

        # 選択肢を作成
        if seasons:
            options = [
                discord.SelectOption(label=season.season_name, value=str(season.id)) for season in seasons
            ]
            placeholder = "過去のシーズンを選択してください..."
            disabled = False
        else:
            # 過去のシーズンがない場合はセレクトメニューを無効化
            options = [
                discord.SelectOption(label="過去のシーズンはありません", value="no_season")
            ]
            placeholder = "過去のシーズンはありません"
            disabled = True

        # 親クラスの初期化を必ず呼び出す
        super().__init__(placeholder=placeholder, options=options, disabled=disabled)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "no_season":
            await interaction.response.send_message("過去のシーズンはありません。", ephemeral=True)
            return

        season_id = int(self.values[0])
        season_name = self.session.query(Season).filter_by(id=season_id).first().season_name
        await interaction.response.defer(ephemeral=True)

        # 選択されたシーズンのランキングを表示
        if self.ranking_type == "rate":
            await self.show_rate_ranking(interaction, season_id, season_name)
        elif self.ranking_type == "win_streak":
            await self.show_win_streak_ranking(interaction, season_id, season_name)
        elif self.ranking_type == "win_rate":
            await self.show_win_rate_ranking(interaction, season_id, season_name)

        # メッセージを削除
        await asyncio.sleep(10)
        try:
            await interaction.delete_original_response()
        except discord.errors.NotFound:
            pass  # メッセージが見つからない場合は無視

    async def show_rate_ranking(self, interaction, season_id, season_name):
        ranking = self.session.query(UserSeasonRecord).filter_by(season_id=season_id).order_by(desc(UserSeasonRecord.rating)).limit(100).all()
        embed = discord.Embed(title=f"【{season_name}】レーティングランキング", color=discord.Color.blue())
        await self.send_ranking_embed(embed, ranking, interaction, "rating")

    async def show_win_rate_ranking(self, interaction, season_id, season_name):
        ranking = self.session.query(UserSeasonRecord).filter_by(season_id=season_id).filter(UserSeasonRecord.total_matches >= 50).all()
        ranking = sorted(ranking, key=lambda record: (record.win_count / record.total_matches) * 100 if record.total_matches > 0 else 0, reverse=True)
        embed = discord.Embed(title=f"【{season_name}】勝率ランキング", color=discord.Color.green())
        await self.send_ranking_embed(embed, ranking, interaction, "win_rate")

    async def show_win_streak_ranking(self, interaction, season_id, season_name):
        ranking = self.session.query(UserSeasonRecord).filter_by(season_id=season_id).order_by(desc(UserSeasonRecord.max_win_streak)).limit(100).all()
        embed = discord.Embed(title=f"【{season_name}】連勝数ランキング", color=discord.Color.red())
        await self.send_ranking_embed(embed, ranking, interaction, "win_streak")

    async def send_ranking_embed(self, embed, ranking, interaction, ranking_type):
        """ランキングをEmbedで表示し、25人ずつ送信する"""
        messages = []  # 送信したメッセージを保持
        for i, record in enumerate(ranking, start=1):
            user = self.session.query(User).filter_by(id=record.user_id).first()
            if user:
                if ranking_type == "rating":
                    # レートを小数点第3位まで表示
                    embed.add_field(
                        name=f"{i}. {user.user_name}",
                        value=f"レート: {record.rating:.3f}",
                        inline=False
                    )
                elif ranking_type == "win_rate":
                    win_rate = (record.win_count / record.total_matches) * 100 if record.total_matches > 0 else 0
                    embed.add_field(
                        name=f"{i}. {user.user_name}",
                        value=f"勝率: {win_rate:.3f}% ({record.total_matches}戦 {record.win_count}勝-{record.loss_count}敗)",
                        inline=False
                    )
                elif ranking_type == "win_streak":
                    embed.add_field(
                        name=f"{i}. {user.user_name}",
                        value=f"連勝数: {record.max_win_streak}",
                        inline=False
                    )

            # Embedのフィールドが25個になったら送信
            if len(embed.fields) == 25:
                message = await interaction.followup.send(embed=embed, ephemeral=True)
                messages.append(message)  # 送信したメッセージを保存
                embed.clear_fields()

        # 残りのフィールドがある場合送信
        if len(embed.fields) > 0:
            message = await interaction.followup.send(embed=embed, ephemeral=True)
            messages.append(message)

        # 5分後にすべてのメッセージを削除
        await asyncio.sleep(300)
        for msg in messages:
            try:
                await msg.delete()  # メッセージが存在するか確認してから削除
            except discord.errors.NotFound:
                pass  # メッセージが見つからない場合は無視

