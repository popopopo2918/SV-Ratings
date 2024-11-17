import discord
from discord.ext import commands, tasks
from discord.ui import Button, View, Select
import asyncio
from asyncio import Queue, Semaphore
from datetime import datetime, timedelta
from sqlalchemy import create_engine, Column, Integer, String, desc
from sqlalchemy.ext.automap import automap_base
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import Session
from sqlalchemy.orm import scoped_session, sessionmaker
import logging
from win_record import WinRecord, CurrentSeasonRecordView, PastSeasonRecordView, Last50RecordView, session
from ranking import RankingView, RankingButtonView  # ranking.py をインポート
import atexit
from dotenv import load_dotenv
import os
import heapq
from collections import defaultdict
from datetime import datetime, timedelta
import unicodedata
import random
import traceback
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


api_call_semaphore = Semaphore(5)  # 一度に処理できるリクエスト数を制限
request_queue = Queue()
load_dotenv()
bot_token = os.getenv('TOKEN')

# PROFILE_CHANNEL_ID の設定
WELCOME_CHANNEL_ID = 1273994235422572574
PROFILE_CHANNEL_ID = 1283799021353173083  
RANKING_CHANNEL_ID = 1271883030243311817
PAST_RANING_CHANNEL_ID = 1277257948753563679
RECORD_CHANNEL_ID = 1276177240396271657
PAST_RECORD_CHANNEL_ID = 1278348616918110432
LAST_50_MATCHES_RECORD_CHANNEL_ID = 1278348685696307312
MATCHING_CHANNEL_ID = 1271905507845734431
BATTLE_CHANNEL_ID = 1275436900055912508


# Battleガイドの文章を常に表示する
BATTLE_GUIDE_TEXT = "「トラブルの際は、必ず対戦相手とのチャットでコミュニケーションを取って下さい。細かいルールは「battle-guide」を参照して下さい。」"

# データベースの設定
db_path = 'db/shadowverse_bridge.db'
engine = create_engine(f'sqlite:///{db_path}', echo=False)
# 自動マッピング用のベースクラスの準備
Base = automap_base()

# ログ設定
logging.basicConfig(
    level=logging.INFO,  # 開発中はDEBUG、本番ではINFOに変更
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)

# データベース内のテーブルをすべて反映
Base.prepare(engine, reflect=True)

# マッピングされたクラスの取得
User = Base.classes.user  # テーブル名が 'user' だと仮定
Class = Base.classes.deck_class
MatchHistory = Base.classes.match_history  # テーブル名が 'match_history' だと仮定
Season = Base.classes.season  # テーブル名が 'season' だと仮定
UserSeasonRecord = Base.classes.user_season_record
# 他のテーブルも同様にマッピングできます)
session = Session(engine)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents)

current_season_name = None

valid_classes = [cls.class_name for cls in session.query(Class.class_name).all()]

SessionLocal = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))

# 安全にスレッドを作成する関数
async def safe_create_thread(channel, user1, user2):
    retries = 5
    for attempt in range(retries):
        try:
            async with api_call_semaphore:
                game_thread = await channel.create_thread(
                    name=f"{user1.display_name}_vs_{user2.display_name}",
                    type=discord.ChannelType.private_thread,
                    invitable=False
                )
            return game_thread
        except discord.HTTPException as e:
            if e.status == 429:
                retry_after = getattr(e, 'retry_after', None)
                if retry_after is None:
                    retry_after = 5  # デフォルトの待機時間（秒）
                else:
                    retry_after += 1  # バッファを追加
                logging.warning(f"Rate limited while creating thread. Retrying after {retry_after} seconds.")
                await asyncio.sleep(retry_after)
            else:
                logging.error(f"Thread creation failed: {e}")
                if attempt < retries - 1:
                    await asyncio.sleep(2 ** attempt)
                else:
                    raise  # 最大リトライ回数を超えた場合は例外を再スロー

# 安全にユーザーをスレッドに追加する関数
async def safe_add_user_to_thread(thread, user):
    retries = 5
    for attempt in range(retries):
        try:
            async with api_call_semaphore:
                await thread.add_user(user)
            return
        except discord.HTTPException as e:
            if e.status == 429:
                retry_after = getattr(e, 'retry_after', None)
                if retry_after is None:
                    retry_after = 5  # デフォルトの待機時間（秒）
                else:
                    retry_after += 1  # バッファを追加
                logging.warning(f"Rate limited while adding user to thread. Retrying after {retry_after} seconds.")
                await asyncio.sleep(retry_after)
            else:
                logging.error(f"Failed to add user {user.display_name} to thread: {e}")
                if attempt < retries - 1:
                    await asyncio.sleep(2 ** attempt)
                else:
                    raise


# 安全にメッセージを送信する関数
async def safe_send_message(channel, content, **kwargs):
    retries = 5
    for attempt in range(retries):
        try:
            async with api_call_semaphore:
                await channel.send(content, **kwargs)
            return  # 成功した場合は関数を終了
        except discord.HTTPException as e:
            if e.status == 429:
                retry_after = getattr(e, 'retry_after', None)
                if retry_after is None:
                    retry_after = 5  # デフォルトの待機時間（秒）
                else:
                    retry_after = retry_after + 1  # バッファを追加
                logging.warning(f"Rate limited. Retrying after {retry_after} seconds.")
                await asyncio.sleep(retry_after)
            else:
                logging.error(f"Failed to send message: {e}")
                if attempt < retries - 1:
                    await asyncio.sleep(2 ** attempt)  # エクスポネンシャルバックオフ
                else:
                    raise  # 最大リトライ回数を超えた場合は例外を再スロー


# 安全にロールを追加する関数
async def assign_role(user: discord.Member, role_name: str):
    """ユーザーに特定のロールを安全に付与します。"""
    retries = 5
    for attempt in range(retries):
        try:
            role = discord.utils.get(user.guild.roles, name=role_name)
            if role and role not in user.roles:
                async with api_call_semaphore:
                    await user.add_roles(role)
                    logging.info(f"ロール {role_name} を {user.display_name} に付与しました。")
            else:
                logging.info(f"ロール {role_name} が見つからないか、{user.display_name} は既にそのロールを持っています。")
            return
        except discord.HTTPException as e:
            if e.status == 429:
                retry_after = getattr(e, 'retry_after', None)
                if retry_after is None:
                    retry_after = 5  # デフォルトの待機時間（秒）
                else:
                    retry_after += 1  # バッファを追加
                logging.warning(f"Rate limited while assigning role. Retrying after {retry_after} seconds.")
                await asyncio.sleep(retry_after)
            else:
                logging.error(f"Failed to assign role {role_name} to {user.display_name}: {e}")
                if attempt < retries - 1:
                    await asyncio.sleep(2 ** attempt)  # 指数バックオフ
                else:
                    raise


# 安全にロールを削除する関数
async def remove_role(user: discord.Member, role_name: str):
    """ユーザーから特定のロールを安全に削除します。"""
    retries = 5
    for attempt in range(retries):
        try:
            role = discord.utils.get(user.guild.roles, name=role_name)
            if role and role in user.roles:
                async with api_call_semaphore:
                    await user.remove_roles(role)
                    logging.info(f"ロール {role_name} を {user.display_name} から削除しました。")
            else:
                logging.info(f"ロール {role_name} が見つからないか、{user.display_name} はそのロールを持っていません。")
            return
        except discord.HTTPException as e:
            if e.status == 429:
                retry_after = getattr(e, 'retry_after', None)
                if retry_after is None:
                    retry_after = 5  # デフォルトの待機時間（秒）
                else:
                    retry_after += 1  # バッファを追加
                logging.warning(f"Rate limited while removing role. Retrying after {retry_after} seconds.")
                await asyncio.sleep(retry_after)
            else:
                logging.error(f"Failed to remove role {role_name} from {user.display_name}: {e}")
                if attempt < retries - 1:
                    await asyncio.sleep(2 ** attempt)  # 指数バックオフ
                else:
                    raise


def calculate_rating_change(player_rating, opponent_rating, player_wins, opponent_wins):
    """
    レートの増減を計算します。
    :param player_rating: 自分のレート
    :param opponent_rating: 相手のレート
    :param player_wins: 自分の勝利数（0, 1, 2）
    :param opponent_wins: 相手の勝利数（0, 1, 2）
    :return: レートの増減量
    """
    base_change = 20  # 基本のレート増減量
    rating_diff = player_rating - opponent_rating
    increment_per_win = 0.025 * abs(rating_diff)  # レート差に基づく増分

    if player_rating > opponent_rating:
        if player_wins > opponent_wins:
            rating_change = base_change - increment_per_win
        else:
            rating_change = -(base_change + increment_per_win)
    else:
        if player_wins > opponent_wins:
            rating_change = base_change + increment_per_win
        else:
            rating_change = -(base_change - increment_per_win)

    return rating_change

def update_current_season_name():
    global current_season_name
    latest_season = session.query(Season).filter(Season.end_date == None).order_by(desc(Season.id)).first()
    if latest_season:
        current_season_name = latest_season.season_name
    else:
        current_season_name = None

class RegisterView(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(RegisterButton())

class RegisterButton(Button):
    def __init__(self):
        super().__init__(label="Register", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction):
        # スレッドを作成
        thread = await interaction.channel.create_thread(
            name=f"{interaction.user.display_name}-registration",
            type=discord.ChannelType.private_thread,
            invitable=False
        )
        await interaction.response.defer()
        await thread.add_user(interaction.user)
        
        # スレッド内で register_user の処理を行う
        await register_user(interaction, thread)

waiting_list = []
active_result_views = {}

class CancelConfirmationView(discord.ui.View):
    def __init__(self, user1, user2, thread):
        super().__init__(timeout=None)
        self.user1 = user1  # キャンセルを提案したユーザー
        self.user2 = user2  # 対戦相手
        self.thread = thread
        self.accept_timer_task = asyncio.create_task(self.accept_timer())  # 48時間後に自動的に中止を受け入れ

    @discord.ui.button(label="はい", style=discord.ButtonStyle.success)
    async def yes_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        if interaction.user.id == self.user2.id:
            # インタラクションにエフェメラルな応答を返す
            await interaction.response.send_message("回答が完了しました。次の試合を開始できます。", ephemeral=True)

            self.accept_timer_task.cancel()  # タイマータスクをキャンセル
            await self.increment_cancelled_count(self.user1, self.user2)
            await self.thread.send(f"{interaction.user.mention} が中止を受け入れ、対戦が無効になりました。このスレッドを削除します。")
            await remove_role(self.user2, "試合中")  # ロールを削除

            # active_result_viewsから削除
            if self.thread.id in active_result_views:
                del active_result_views[self.thread.id]

            await asyncio.sleep(6)
            await self.thread.delete()
        else:
            await interaction.response.send_message("対戦相手のみがこのボタンを使用できます。", ephemeral=True)

    @discord.ui.button(label="いいえ", style=discord.ButtonStyle.danger)
    async def no_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        if interaction.user.id == self.user2.id:
            # インタラクションにエフェメラルな応答を返す
            self.accept_timer_task.cancel()  # タイマータスクをキャンセル
            await remove_role(self.user2, "試合中")  # ロールを削除
            await interaction.response.send_message("回答が完了しました。次の試合を開始できます。", ephemeral=True)

            await self.thread.send("チャットで状況を説明し、必要であれば関連する画像をアップロードしてください。スタッフが対応します。")
            staff_role = discord.utils.get(interaction.guild.roles, name="staff")
            await self.thread.send(f"{staff_role.mention}")

            # active_result_viewsから削除
            if self.thread.id in active_result_views:
                del active_result_views[self.thread.id]
        else:
            await interaction.response.send_message("対戦相手のみがこのボタンを使用できます。", ephemeral=True)

    async def increment_cancelled_count(self, user1, user2):
        # データベースでキャンセル回数を増加
        user1_db = session.query(User).filter_by(discord_id=str(user1.id)).first()
        user2_db = session.query(User).filter_by(discord_id=str(user2.id)).first()
        if user1_db and user2_db:
            user1_db.cancelled_matches_count += 1
            user2_db.cancelled_matches_count += 1
            session.commit()

    async def accept_timer(self):
        # 48時間後に自動的に「はい」とみなす
        await asyncio.sleep(48 * 60 * 60)  # 48時間待機
        await self.thread.send(f"48時間が経過しました。{self.user2.mention} が応答しなかったため、対戦中止を受け入れたとみなします。このスレッドを削除します。")
        await self.increment_cancelled_count(self.user1, self.user2)
        await remove_role(self.user2, "試合中")  # ロールを削除

        # active_result_viewsから削除
        if self.thread.id in active_result_views:
            del active_result_views[self.thread.id]

        await asyncio.sleep(6)
        await self.thread.delete()



@bot.slash_command(name="cancel", description="試合の中止の提案をします。")
async def cancel(ctx: discord.ApplicationContext):
    if isinstance(ctx.channel, discord.Thread) and ctx.channel.parent_id == BATTLE_CHANNEL_ID:
        user1 = ctx.author
        thread_id = ctx.channel.id

        # active_result_viewsからResultViewを取得
        result_view = active_result_views.get(thread_id)

        if result_view:
            # 試合中ロールを削除
            await remove_role(user1, "試合中")

            # ResultViewのタイマータスクをキャンセル
            result_view.cancel_timeout()

            # 対戦相手を取得
            user2_id = result_view.player1_id if result_view.player2_id == user1.id else result_view.player2_id
            user2 = ctx.guild.get_member(user2_id)
            if user2 is None:
                user2 = await ctx.guild.fetch_member(user2_id)

            # エフェメラルな応答を返して「考え中」の表示を消す
            await ctx.respond(f"対戦中止のリクエストを送信しました。{user1.mention}は次の試合を開始できます。", ephemeral=True)

            await ctx.channel.send(
                f"{user1.mention}により対戦が中止されました。{user2.mention}は中止を受け入れるか回答してください。回答するまで次の試合を開始することはできません。問題がない場合は「はい」を押してください。問題がある場合は「いいえ」を押してスタッフに説明してください。回答期限は48時間です。",
                view=CancelConfirmationView(user1, user2, ctx.channel)
            )
        else:
            await ctx.respond("このスレッドでは試合が行われていません。", ephemeral=True)
    else:
        await ctx.respond("このコマンドは対戦スレッド内でのみ使用できます。", ephemeral=True)




@bot.slash_command(name="report", description="対戦中に問題が発生した際に報告します。")
async def report(ctx: discord.ApplicationContext):
    if isinstance(ctx.channel, discord.Thread) and ctx.channel.parent_id == BATTLE_CHANNEL_ID:
        user1 = ctx.author
        thread_id = ctx.channel.id

        # active_result_viewsからResultViewを取得
        result_view = active_result_views.get(thread_id)

        if result_view:
            # 両方のユーザーから「試合中」ロールを削除
            await remove_role(user1, "試合中")
            user2_id = result_view.player1_id if result_view.player2_id == user1.id else result_view.player2_id
            user2 = ctx.guild.get_member(user2_id)
            if user2 is None:
                user2 = await ctx.guild.fetch_member(user2_id)
            await remove_role(user2, "試合中")

            # ResultViewのタイマータスクをキャンセル
            result_view.cancel_timeout()

            # インタラクションにエフェメラルな応答を返して「考え中」の表示を消す
            await ctx.respond("報告を受け付けました。スタッフが対応します。", ephemeral=True)

            # スタッフに通知
            staff_role = discord.utils.get(ctx.guild.roles, name="staff")
            await ctx.channel.send(
                f"報告が提出されました。状況を説明し、必要であれば画像をアップロードしてください。{staff_role.mention}に通知しました。"
            )
        else:
            await ctx.respond("このスレッドでは試合が行われていません。", ephemeral=True)
    else:
        await ctx.respond("このコマンドは対戦スレッド内でのみ使用できます。", ephemeral=True)

class MyView(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(OnlyClassSelect())

class OnlyClassSelect(discord.ui.Select):
    """クラス選択を行う処理。"""
    def __init__(self):
        valid_classes = ['エルフ', 'ロイヤル', 'ウィッチ', 'ドラゴン', 'ネクロマンサー', 'ヴァンパイア', 'ビショップ', 'ネメシス']
        options = [discord.SelectOption(label=cls, value=f"{cls}_{i}") for i, cls in enumerate(valid_classes)]
        super().__init__(placeholder="Select your classes...", min_values=2, max_values=2, options=options)

    async def callback(self, interaction: discord.Interaction):
        user_id = interaction.user.id  # 操作したユーザーのDiscord IDを取得
        
        # 選択されたクラスを取得
        selected_classes = [cls.split('_')[0] for cls in self.values]
        # "試合中"ロールがあるか確認
        role_name = "試合中"
        active_role = discord.utils.get(interaction.user.roles, name=role_name)  # ロールオブジェクトを取得
        if active_role:  # 試合中のロールが存在するか確認
            await interaction.response.send_message(f"{interaction.user.mention} 現在試合中のため、クラスを変更できません。", ephemeral=True)
            await asyncio.sleep(10)
            await interaction.delete_original_response()
            return
        if len(selected_classes) != 2:
            await interaction.response.send_message("クラスを2つ選択してください。", ephemeral=True)
            await asyncio.sleep(10)
            await interaction.delete_original_response()
            return
        
        # データベースでユーザーを検索
        user_instance = session.query(User).filter_by(discord_id=user_id).first()
        
        if user_instance:
            # クラス1とクラス2を更新
            user_instance.class1 = selected_classes[0]
            user_instance.class2 = selected_classes[1]
            session.commit()  # 変更を保存
            
            await interaction.response.send_message(f"Update selected classes: {', '.join(selected_classes)}", ephemeral=True)
            await asyncio.sleep(30)
            await interaction.delete_original_response()
        else:
            await interaction.response.send_message("ユーザー未登録です。", ephemeral=True)
            await asyncio.sleep(15)
            await interaction.delete_original_response()


class MatchmakingView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.bot = bot  # Store the bot instance
        self.session = session  # Store the database session
        self.waiting_queue = []  # 優先度付きキュー (heapq) にユーザーを格納
        self.match_lock = asyncio.Lock()  # ロックの導入
        self.previous_opponents = {}  # 各ユーザーの前回のマッチング相手を記録
        self.background_task = asyncio.create_task(self.background_match_check())  # 定期的な再評価タスクの開始
        self.request_queue = asyncio.Queue()  # Request queue for batching
        self.processing_task = asyncio.create_task(self.process_queue())  # Start batch processing
        self.user_interactions = {}
        self.monitor_tasks = {} # スレッドIDをキーとしてタスクを保存

    # Batch process requests from the queue
    async def process_queue(self):
        while True:
            batch_requests = []
            while not self.request_queue.empty():
                batch_requests.append(await self.request_queue.get())
            if batch_requests:
                # Process all requests concurrently
                await asyncio.gather(*(request() for request in batch_requests))
            await asyncio.sleep(0.1)  # Short sleep to prevent tight loop

    @discord.ui.button(label="マッチング待機", style=discord.ButtonStyle.primary)
    async def start_matching(self, button: discord.ui.Button, interaction: discord.Interaction):
        user = interaction.user
        await interaction.response.defer(ephemeral=True)
        # ユーザーが登録されているか確認
        user_data = session.query(User).filter_by(discord_id=user.id).first()
        if not user_data:
            message = await interaction.followup.send(f"{user.mention} ユーザー登録を行ってください。", ephemeral=True)
            asyncio.create_task(self.delete_messages_after_delay(message))
            return
        # シーズン期間中か確認
        latest_season = session.query(Season).order_by(Season.id.desc()).first()
        if latest_season is None or latest_season.start_date is None or latest_season.end_date is not None:
            message = await interaction.followup.send(f"{user.mention} シーズン期間外です。", ephemeral=True)
            asyncio.create_task(self.delete_messages_after_delay(message))
            return
        # クラスが設定されているか確認
        if not user_data.class1 or not user_data.class2:
            message = await interaction.followup.send(f"{user.mention} クラスを選択してください。", ephemeral=True)
            asyncio.create_task(self.delete_messages_after_delay(message))
            return

        # "試合中"ロールがあるか確認
        role_name = "試合中"
        active_role = discord.utils.get(user.roles, name=role_name)  # ロールオブジェクトを取得
        if active_role:  # 試合中のロールが存在するか確認
            message = await interaction.followup.send(f"{user.mention} 現在試合中のため、マッチング待機リストに入ることができません。", ephemeral=True)
            asyncio.create_task(self.delete_messages_after_delay(message))
            return

        # ユーザーのインタラクションを保存
        self.user_interactions[user.id] = interaction
        # Add the request to the queue
        await self.request_queue.put(lambda: self.add_to_waiting_list(user, interaction))

    # Add the user to the waiting list as part of batch processing
    async def add_to_waiting_list(self, user, interaction):
        try:
            # Add random delay to the request
            delay = random.uniform(0.1, 0.5)
            await asyncio.sleep(delay)

            user_data = self.session.query(User).filter_by(discord_id=user.id).first()
            if not user_data:
                message = await interaction.followup.send(f"{user.mention} ユーザーデータが見つかりません。", ephemeral=True)
                asyncio.create_task(self.delete_messages_after_delay(message))
                return

            # Retrieve the user's rating from the database
            user_rating = user_data.rating if user_data.rating is not None else 1500  # Default rating if not set

            async with self.match_lock:
                # 待機リストに既に存在するか確認
                if any(queued_user.id == user.id for _, _, queued_user in self.waiting_queue):
                    message = await interaction.followup.send(f"{user.mention} は既に待機リストにいます。", ephemeral=True)
                    asyncio.create_task(self.delete_messages_after_delay(message))
                    return
                # "試合中"ロールがあるか確認
                role_name = "試合中"
                active_role = discord.utils.get(user.roles, name=role_name)  # ロールオブジェクトを取得
                if active_role:  # 試合中のロールが存在するか確認
                    logging.info(f"User {user.id} is already have matching role.")
                    return
                heapq.heappush(self.waiting_queue, (user_rating, user_data.id, user))
                logging.info(f"User {user.id} added to waiting_queue with rating {user_rating}.")
                message = await interaction.followup.send(f"{user.mention} が待機リストに追加されました。", ephemeral=True)
                asyncio.create_task(self.delete_messages_after_delay(message))
                # マッチング処理はバックグラウンドタスクに任せる
            # Schedule the removal after timeout without blocking
            asyncio.create_task(self.remove_user_after_timeout(user))
        except Exception as e:
            logging.error(f"Error in add_to_waiting_list: {e}")

    async def delete_messages_after_delay(self, message):
        await asyncio.sleep(60)
        try:
            await message.delete()
        except discord.errors.NotFound:
            pass  # メッセージが既に削除されている場合は無視
        except discord.errors.Forbidden:
            pass  # エフェメラルメッセージなど、削除できないメッセージの場合は無視
        except Exception as e:
            logging.error(f"Failed to delete message: {e}")

    async def remove_user_after_timeout(self, user):
        await asyncio.sleep(60)
        async with self.match_lock:
            for i, (_, _, queued_user) in enumerate(self.waiting_queue):
                if queued_user.id == user.id:
                    del self.waiting_queue[i]
                    heapq.heapify(self.waiting_queue)  # Reconstruct the queue
                    # 保存しておいたインタラクションを取得
                    interaction = self.user_interactions.get(user.id)
                    if interaction:
                        try:
                            # エフェメラルメッセージを送信
                            await interaction.followup.send("マッチング相手が見つかりませんでした。", ephemeral=True)
                        except Exception as e:
                            logging.error(f"Failed to send ephemeral message to {user}: {e}")
                        # インタラクションを削除
                        del self.user_interactions[user.id]
                    break

    async def match_users(self):
        role_name = "試合中"
        async with self.match_lock:
            matched_users_ids = set()
            matches = []

            # waiting_queueのコピーを作成して、反復中の変更を防ぐ
            queue_copy = list(self.waiting_queue)
            for i in range(len(queue_copy) - 1):
                user1_rating, _, user1 = queue_copy[i]
                if user1.id in matched_users_ids:
                    continue  # マッチ済みのユーザーはスキップ
                for j in range(i + 1, len(queue_copy)):
                    user2_rating, _, user2 = queue_copy[j]
                    if user2.id in matched_users_ids:
                        continue  # マッチ済みのユーザーはスキップ
                    if user1.id == user2.id:
                        continue  # 同じユーザーはスキップ

                    # 連続マッチを避けるため、前回の相手を確認
                    if self.previous_opponents.get(user1.id) == user2.id or self.previous_opponents.get(user2.id) == user1.id:
                        continue

                    rating_diff = abs(user1_rating - user2_rating)
                    if rating_diff <= 300:
                        # マッチを記録
                        matched_users_ids.update([user1.id, user2.id])
                        self.previous_opponents[user1.id] = user2.id
                        self.previous_opponents[user2.id] = user1.id
                        matches.append((user1, user2))
                        # ロールの付与をマッチング確定のタイミングで実行
                        role_name = "試合中"
                        await assign_role(user1, role_name)
                        await asyncio.sleep(0.5)
                        await assign_role(user2, role_name)
                        await asyncio.sleep(0.5)
                        break  # 内側のループを抜けて次の user1 へ

            # マッチしたユーザーを waiting_queue から削除
            self.waiting_queue = [(rating, id_, user) for rating, id_, user in self.waiting_queue if user.id not in matched_users_ids]
            heapq.heapify(self.waiting_queue)
            # マッチングが成立したユーザーに通知
            for user1, user2 in matches:
                interaction1 = self.user_interactions.get(user1.id)
                interaction2 = self.user_interactions.get(user2.id)
                if interaction1:
                    message = await interaction1.followup.send(
                        "マッチングが成立しました。バトルスレッドの作成を待っています。", ephemeral=True)
                    asyncio.create_task(self.delete_messages_after_delay(message))
                if interaction2:
                    message = await interaction2.followup.send(
                        "マッチングが成立しました。バトルスレッドの作成を待っています。", ephemeral=True)
                    asyncio.create_task(self.delete_messages_after_delay(message))

            # マッチチャンネルの作成を順番に処理
            for user1, user2 in matches:
                # マッチングログの追加
                logging.info(f"{user1.id} ({user1.display_name}) と {user2.id} ({user2.display_name}) がマッチングしました")
                await self.create_match_channel(user1, user2)
                await asyncio.sleep(0.5)  # 0.5秒の遅延を挿入

    # Periodically check the waiting list
    async def background_match_check(self):
        while True:
            await asyncio.sleep(5)
            await self.match_users()

    async def create_match_channel(self, user1, user2):
        role_name = "試合中"
        await asyncio.sleep(0.5)
        # マッチング時点でのクラスを取得
        user1_instance = session.query(User).filter_by(discord_id=user1.id).first()
        user2_instance = session.query(User).filter_by(discord_id=user2.id).first()
        matching_classes = {
            user1.id: (user1_instance.class1, user1_instance.class2),
            user2.id: (user2_instance.class1, user2_instance.class2)
        }
        user1_rating = user1_instance.rating
        user2_rating = user2_instance.rating
        channel = bot.get_channel(BATTLE_CHANNEL_ID)

        # マッチング成功したユーザーのインタラクションを削除
        if user1.id in self.user_interactions:
            del self.user_interactions[user1.id]
        if user2.id in self.user_interactions:
            del self.user_interactions[user2.id]
        # スレッドの作成
        game_thread = await safe_create_thread(channel, user1, user2)

        # スレッドにユーザーを追加
        await safe_add_user_to_thread(game_thread, user1)
        await asyncio.sleep(0.5)  # 少し待機
        await safe_add_user_to_thread(game_thread, user2)
        await asyncio.sleep(0.5)  # 少し待機


        # メッセージの送信
        # 勝利数ボタンを表示するためのビューを作成
        view = ResultView(user1.id, user2.id, matching_classes, game_thread, self)
        await asyncio.sleep(0.5)  # 少し待機

        # ResultViewをactive_result_viewsに登録
        active_result_views[game_thread.id] = view

        content = (
            f"**マッチング成功!**\n\n"
            f"{user1.mention} さん、ルームマッチのルームを「ローテーションBO3」で作成し、ルームIDを入力してください。\n"
            f"{user2.mention} さん、対戦相手がルームを作成します。入力されたルームに入室してください。\n\n"
            f"{user1.display_name}さんの現在のレート: {int(user1_rating)}\n"
            f"信用ポイント: {int(user1_instance.trust_points)}\n"
            f"{user2.display_name}さんの現在のレート: {int(user2_rating)}\n"
            f"信用ポイント: {int(user2_instance.trust_points)}\n"
            f"試合が終わったら勝利数を選択してください。"
        )
        await asyncio.sleep(0.5)
        
        await safe_send_message(game_thread, content, view=view)

        # 前回の相手を更新（ここで更新することで、次回のマッチング時に連続で同じ相手とマッチングしないようにする）
        self.previous_opponents[user1.id] = user2.id
        self.previous_opponents[user2.id] = user1.id


async def start_matchmaking(channel):
    view = MatchmakingView()
    await channel.send("マッチングを開始するにはボタンをクリックしてください\nマッチングが成功したらbattleチャンネルにスレッドが作成されます そちらで対戦を行ってください", view=view)


class ResultView(discord.ui.View):
    def __init__(self, player1_id, player2_id, matching_classes, thread, matchmaking_view):
        super().__init__(timeout=None)
        self.player1_id = player1_id
        self.player2_id = player2_id
        self.matching_classes = matching_classes
        self.thread = thread
        self.player1_result = None
        self.player2_result = None
        self.results_locked = False
        self.timeout_task = None  # タイマータスクを管理
        self.matchmaking_view = matchmaking_view  # MatchmakingViewへの参照を追加

    @discord.ui.button(label="2勝", style=discord.ButtonStyle.success)
    async def two_wins(self, button: discord.ui.Button, interaction: discord.Interaction):
        await remove_role(interaction.user, "試合中")
        await self.handle_result(interaction, 2)

    @discord.ui.button(label="1勝", style=discord.ButtonStyle.primary)
    async def one_win(self, button: discord.ui.Button, interaction: discord.Interaction):
        await remove_role(interaction.user, "試合中")
        await self.handle_result(interaction, 1)

    @discord.ui.button(label="0勝", style=discord.ButtonStyle.danger)
    async def zero_wins(self, button: discord.ui.Button, interaction: discord.Interaction):
        await remove_role(interaction.user, "試合中")
        await self.handle_result(interaction, 0)
    @discord.ui.button(label="リセット", style=discord.ButtonStyle.secondary)
    async def reset(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self.handle_reset(interaction)

    async def handle_reset(self, interaction: discord.Interaction):
        user_id = interaction.user.id

        # インタラクションが未応答の場合は遅延応答を行う
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        # ユーザーが試合の参加者であるか確認
        if user_id != self.player1_id and user_id != self.player2_id:
            if not interaction.response.is_done():
                await interaction.response.send_message("この試合の参加者ではありません。", ephemeral=True)
            else:
                await interaction.followup.send("この試合の参加者ではありません。", ephemeral=True)
            return

        # 結果をリセット
        if user_id == self.player1_id:
            self.player1_result = None
        elif user_id == self.player2_id:
            self.player2_result = None

        # 「試合中」ロールを再度付与
        await assign_role(interaction.user, "試合中")

        # タイマーが存在する場合はキャンセル
        self.cancel_timeout()

        # ユーザーにリセット完了を通知
        if not interaction.response.is_done():
            await interaction.response.send_message(f"{interaction.user.display_name} さんの勝利数の入力をリセットしました。")
        else:
            await interaction.followup.send(f"{interaction.user.display_name} さんの勝利数の入力をリセットしました。")


    async def handle_result(self, interaction: discord.Interaction, result: int):
        # インタラクションが未応答の場合は遅延応答を行う
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        user_id = interaction.user.id

        # ユーザーが試合の参加者であるか確認
        if user_id != self.player1_id and user_id != self.player2_id:
            if not interaction.response.is_done():
                await interaction.response.send_message("あなたはこの試合の参加者ではありません。", ephemeral=True)
            else:
                await interaction.followup.send("あなたはこの試合の参加者ではありません。", ephemeral=True)
            return

        # 結果がロックされている場合は処理を終了
        if self.results_locked:
            if not interaction.response.is_done():
                await interaction.response.send_message("結果は既に確定しています。", ephemeral=True)
            else:
                await interaction.followup.send("結果は既に確定しています。", ephemeral=True)
            return

        # リセットの場合
        if result == -1:
            await assign_role(interaction.user, "試合中")
            if user_id == self.player1_id:
                self.player1_result = None
            else:
                self.player2_result = None
            if not interaction.response.is_done():
                await interaction.response.send_message(f"{interaction.user.display_name} が結果をリセットしました。")
            else:
                await interaction.followup.send(f"{interaction.user.display_name} が結果をリセットしました。")
            return

        # 勝利数が有効な値か確認
        if result not in [0, 1, 2]:
            if not interaction.response.is_done():
                await interaction.response.send_message("勝利数は0から2の整数で入力してください。", ephemeral=True)
            else:
                await interaction.followup.send("勝利数は0から2の整数で入力してください。", ephemeral=True)
            return

        # 結果を設定
        if user_id == self.player1_id:
            if self.player1_result is not None:
                if not interaction.response.is_done():
                    await interaction.response.send_message("あなたは既に結果を入力しています。", ephemeral=True)
                else:
                    await interaction.followup.send("あなたは既に結果を入力しています。", ephemeral=True)
                return
            self.player1_result = result
        else:
            if self.player2_result is not None:
                if not interaction.response.is_done():
                    await interaction.response.send_message("あなたは既に結果を入力しています。", ephemeral=True)
                else:
                    await interaction.followup.send("あなたは既に結果を入力しています。", ephemeral=True)
                return
            self.player2_result = result

        # 結果を受け付けたことをユーザーに伝える
        if not interaction.response.is_done():
            await interaction.response.send_message(f"{interaction.user.display_name} が {result} 勝を選択しました。")
        else:
            await interaction.followup.send(f"{interaction.user.display_name} が {result} 勝を選択しました。")


        # 両方の結果が揃ったか確認し、結果を処理
        if self.player1_result is not None and self.player2_result is not None:
            # 両者の結果が入力されたらタイマーをキャンセル
            self.cancel_timeout()
            await self.check_results()
        else:
            # 一方の結果のみが入力された場合、タイマーを開始
            if self.timeout_task is None:
                self.timeout_task = asyncio.create_task(self.timeout_wait())

    async def timeout_wait(self):
        guild = self.thread.guild  # スレッドからGuildを取得
        try:
            await asyncio.sleep(3 * 60 * 60)  # 3時間
            if self.player1_result is None and self.player2_result is not None:
                # プレイヤー1が未入力、プレイヤー2の勝利
                self.player1_result = 0
                self.player2_result = 2
                await self.thread.send(f"<@{self.player1_id}> が勝利数を報告しなかったため、<@{self.player2_id}> の勝利となります。")

                # プレイヤー1から「試合中」ロールを削除
                player1_member = guild.get_member(self.player1_id)
                if player1_member:
                    await remove_role(player1_member, "試合中")

                await self.check_results()

            elif self.player2_result is None and self.player1_result is not None:
                # プレイヤー2が未入力、プレイヤー1の勝利
                self.player1_result = 2
                self.player2_result = 0
                await self.thread.send(f"<@{self.player2_id}> が勝利数を報告しなかったため、<@{self.player1_id}> の勝利となります。")

                # プレイヤー2から「試合中」ロールを削除
                player2_member = guild.get_member(self.player2_id)
                if player2_member:
                    await remove_role(player2_member, "試合中")

                await self.check_results()

        except asyncio.CancelledError:
            # タスクがキャンセルされた場合は何もしない
            player1_member = guild.get_member(self.player1_id)
            player2_member = guild.get_member(self.player2_id)
            logging.info(f"{self.player1_id}({player1_member.display_name})と{self.player2_id}({player2_member.display_name})のタスクがキャンセルされました。")
            pass

    def cancel_timeout(self):
        """タイマータスクをキャンセルする"""
        if self.timeout_task is not None:
            self.timeout_task.cancel()
            self.timeout_task = None

    async def check_results(self):
        if self.results_locked:
            return

        try:
            if (self.player1_result + self.player2_result) in [2, 3] and self.player1_result != self.player2_result:
                # レーティングを更新し、変動前後の値と変動量を取得
                try:
                    user1_rating_before, user1_rating_after, user1_rating_change, user2_rating_before, user2_rating_after, user2_rating_change = self.update_ratings(
                        self.player1_id, self.player2_id, self.player1_result, self.player2_result
                    )
                except Exception as e:
                    logging.error(f"Error in update_ratings: {e}")
                    await self.thread.send("レーティングの更新中にエラーが発生しました。管理者にお問い合わせください。")
                    return

                self.cancel_timeout()

                user1 = session.query(User).filter_by(discord_id=str(self.player1_id)).first()
                if user1 is None:
                    logging.error(f"User with ID {self.player1_id} not found in the database.")
                    await self.thread.send(f"ユーザーID {self.player1_id} の情報が見つかりませんでした。")
                    return

                user2 = session.query(User).filter_by(discord_id=str(self.player2_id)).first()
                if user2 is None:
                    logging.error(f"User with ID {self.player2_id} not found in the database.")
                    await self.thread.send(f"ユーザーID {self.player2_id} の情報が見つかりませんでした。")
                    return

                user1_a, user1_b = self.matching_classes.get(self.player1_id, (None, None))
                user2_a, user2_b = self.matching_classes.get(self.player2_id, (None, None))

                update_history(
                    user1_id=user1.id,
                    user2_id=user2.id,
                    season_name=current_season_name,
                    user1_class_a=user1_a,
                    user1_class_b=user1_b,
                    user2_class_a=user2_a,
                    user2_class_b=user2_b,
                    user1_rating_change=user1_rating_change,
                    user2_rating_change=user2_rating_change
                )

                # 最新シーズンでマッチングしたフラグをオンにする
                user1.latest_season_matched = True
                user2.latest_season_matched = True
                session.commit()  # 変更をデータベースに保存
                self.results_locked = True

                # スレッドにレーティング変動メッセージを表示
                user1_change_sign = "+" if user1_rating_change > 0 else ""
                user2_change_sign = "+" if user2_rating_change > 0 else ""

                message = (
                    f"{user1.user_name}さんのレーティングが更新されました。\n"
                    f"変動前: {user1_rating_before:.0f} -> 変動後: {user1_rating_after:.0f} ({user1_change_sign}{user1_rating_change:.0f})\n"
                    f"{user2.user_name}さんのレーティングが更新されました。\n"
                    f"変動前: {user2_rating_before:.0f} -> 変動後: {user2_rating_after:.0f} ({user2_change_sign}{user2_rating_change:.0f})"
                )


                # メッセージを送信する前にログを追加
                logging.info(f"{message}")
                # スレッドに結果を表示
                await self.thread.send(message)

                await asyncio.sleep(5)

                # スレッドを削除
                retries = 5
                for attempt in range(retries):
                    try:
                        await self.thread.delete()
                        break
                    except discord.errors.HTTPException as e:
                        logging.error(f"Attempt {attempt + 1} to delete thread failed: {e}")
                        if attempt < retries - 1:
                            await asyncio.sleep(2 ** attempt)  # 指数バックオフ
                        else:
                            logging.error(f"Failed to delete thread {self.thread} after {retries} attempts.")
            else:
                # 結果が一致しない場合の処理
                self.player1_result = None
                self.player2_result = None
                await self.thread.send(
                    f"<@{self.player1_id}>と<@{self.player2_id}>、結果が一致しません。再度入力してください。",
                    view=self
                )
        except Exception as e:
            logging.error(f"Error in check_results: {e}")
            await self.thread.send("エラーが発生しました。管理者にお問い合わせください。")
            # スレッド削除の試行
            try:
                await self.thread.delete()
            except Exception as delete_exception:
                logging.error(f"Failed to delete thread after error: {delete_exception}")
        # 試合終了後にactive_result_viewsから削除
        if self.thread.id in active_result_views:
            del active_result_views[self.thread.id]

    def update_ratings(self, player1_id, player2_id, player1_wins, player2_wins):
        """レーティングを更新し、データベースに保存します。"""
        
        # プレイヤー1とプレイヤー2をデータベースから取得 (discord_id を使用)
        player1 = session.query(User).filter_by(discord_id=player1_id).first()
        player2 = session.query(User).filter_by(discord_id=player2_id).first()

        if not player1 or not player2:
            raise ValueError("One or both players not found in the database.")

        # レーティングを取得
        user1_rating_data = player1.rating
        user2_rating_data = player2.rating

        # 変動前のレーティングを記録
        user1_rating_before = user1_rating_data
        user2_rating_before = user2_rating_data

        # レーティング変動を計算
        user1_rating_change = calculate_rating_change(user1_rating_data, user2_rating_data, player1_wins, player2_wins)
        user2_rating_change = calculate_rating_change(user2_rating_data, user1_rating_data, player2_wins, player1_wins)

        # レーティングを更新
        player1.rating += user1_rating_change
        player2.rating += user2_rating_change

        # データベースに変更を保存
        session.commit()

        # 変動前の数値と変動量を返す
        return user1_rating_before, player1.rating, user1_rating_change, user2_rating_before, player2.rating, user2_rating_change

def update_history(user1_id, user2_id, season_name, user1_class_a, user1_class_b, user2_class_a, user2_class_b, user1_rating_change, user2_rating_change):
    """対戦履歴を更新する関数"""
    match_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if user1_rating_change > user2_rating_change:
        winner_user_id = user1_id
        loser_user_id = user2_id
    else:
        winner_user_id = user2_id
        loser_user_id = user1_id

    # match_history テーブルにデータを挿入
    new_match = MatchHistory(
        user1_id=user1_id,
        user2_id=user2_id,
        match_date=match_date,
        season_name=season_name,
        user1_class_a=user1_class_a,
        user1_class_b=user1_class_b,
        user2_class_a=user2_class_a,
        user2_class_b=user2_class_b,
        user1_rating_change=user1_rating_change,
        user2_rating_change=user2_rating_change,
        winner_user_id=winner_user_id,
        loser_user_id=loser_user_id
    )
    session.add(new_match)

    # user1 と user2 の情報を取得
    user1 = session.query(User).filter_by(id=user1_id).first()
    user2 = session.query(User).filter_by(id=user2_id).first()

    # total_matches を +1 する
    if user1:
        user1.total_matches += 1
    if user2:
        user2.total_matches += 1

    # win_count と loss_count を更新
    if user1.id == winner_user_id:
        user1.win_count += 1
        user2.loss_count += 1
    else:
        user2.win_count += 1
        user1.loss_count += 1

    # win_streak の更新と max_win_streak の更新
    if user1_rating_change > 0 and user2_rating_change < 0:
        # user1 が勝った場合
        if user1:
            user1.win_streak += 1
            if user1.win_streak > user1.max_win_streak:
                user1.max_win_streak = user1.win_streak  # max_win_streakを更新
        if user2:
            user2.win_streak = 0
    elif user2_rating_change > 0 and user1_rating_change < 0:
        # user2 が勝った場合
        if user2:
            user2.win_streak += 1
            if user2.win_streak > user2.max_win_streak:
                user2.max_win_streak = user2.win_streak  # max_win_streakを更新
        if user1:
            user1.win_streak = 0

    session.commit()

@bot.slash_command(name="manual_result", description="二人のユーザーの間で勝者を手動で決定します。", default_permission=False)
@commands.has_permissions(administrator=True)
async def manual_result(ctx: discord.ApplicationContext, player1: discord.Member, player1_wins: int, player2: discord.Member, player2_wins: int):
    # データベースからユーザー情報を取得
    user1 = session.query(User).filter_by(discord_id=player1.id).first()
    user2 = session.query(User).filter_by(discord_id=player2.id).first()
    matching_classes = {
        player1.id: (user1.class1, user1.class2),
        player2.id: (user2.class1, user2.class2)
    }
    if not user1 or not user2:
        await ctx.respond("指定されたユーザーがデータベースに見つかりませんでした。ユーザー登録を行ってください。", ephemeral=True)
        return

    # 勝利数を比較して結果を設定
    if player1_wins == 2 and player2_wins in [0, 1]:
        result_view = ResultView(player1.id, player2.id, matching_classes, None, None)
        result_view.player1_result = 2  # player1が勝者として2勝とする
        result_view.player2_result = player2_wins  # player2の勝利数を設定
    elif player2_wins == 2 and player1_wins in [0, 1]:
        result_view = ResultView(player1.id, player2.id, matching_classes, None, None)
        result_view.player2_result = 2  # player2が勝者として2勝とする
        result_view.player1_result = player1_wins  # player1の勝利数を設定
    else:
        await ctx.respond("勝利数の入力が正しくありません。2勝した方が勝者となり、もう一方は0勝または1勝でなければなりません。", ephemeral=True)
        return

    # スレッドがないため、ユーザーのレート更新部分を修正
    if result_view.results_locked:
        return

    # レーティングを更新し、変動前後の値と変動量を取得
    user1_rating_before, user1_rating_after, user1_rating_change, user2_rating_before, user2_rating_after, user2_rating_change = result_view.update_ratings(
        result_view.player1_id, result_view.player2_id, result_view.player1_result, result_view.player2_result
    )

    # 最新シーズンでマッチングしたフラグをオンにする
    user1.latest_season_matched = True
    user2.latest_season_matched = True
    session.commit()  # 変更をデータベースに保存
    result_view.results_locked = True

    # 試合履歴を更新
    update_current_season_name()
    season_name = current_season_name  # 必要に応じてシーズン名を設定
    user1_class_a, user1_class_b = matching_classes[player1.id]
    user2_class_a, user2_class_b = matching_classes[player2.id]
    update_history(user1.id, user2.id, season_name, user1_class_a, user1_class_b, user2_class_a, user2_class_b, user1_rating_change, user2_rating_change)

    # レート変動を確認できるようにする
    user1_change_sign = "+" if user1_rating_change > 0 else ""
    user2_change_sign = "+" if user2_rating_change > 0 else ""
    #ロールの削除
    await remove_role(player1, "試合中")
    await remove_role(player2, "試合中")
    # ユーザーにメンションを付けてレーティングの結果を表示
    await ctx.respond(
        f"{player1.mention} vs {player2.mention} の試合結果:\n"
        f"{player1.display_name}のレート: {user1_rating_before:.0f} -> {user1_rating_after:.0f} ({user1_change_sign}{user1_rating_change:.0f})\n"
        f"{player2.display_name}のレート: {user2_rating_before:.0f} -> {user2_rating_after:.0f} ({user2_change_sign}{user2_rating_change:.0f})",
    )

@bot.slash_command(name="remove_role", description="「試合中」ロールを外します。")
async def remove_matching_role(ctx: discord.ApplicationContext):
    await remove_role(ctx.user, "試合中")
    await ctx.respond("試合中ロールを外しました。", ephemeral=True)

@bot.slash_command(name="adjust_win_loss", description="指定したユーザーの勝敗数を調整します。", hidden=True)
@commands.has_permissions(administrator=True)
async def adjust_win_loss(ctx: discord.ApplicationContext, user1: discord.Member, user2: discord.Member):
    # データベースからユーザー情報を取得
    user1_data = session.query(User).filter_by(discord_id=user1.id).first()
    user2_data = session.query(User).filter_by(discord_id=user2.id).first()

    if user1_data and user2_data:
        # 勝敗数の変更が可能か確認
        if user1_data.loss_count > 0 and user2_data.win_count > 0:
            # 第一引数のユーザーのwin_countを+1, loss_countを-1
            user1_data.win_count += 1
            user1_data.loss_count -= 1

            # 第二引数のユーザーのwin_countを-1, loss_countを+1
            user2_data.win_count -= 1
            user2_data.loss_count += 1

            # 変更をデータベースにコミット
            session.commit()

            await ctx.respond(f"{user1.display_name} の勝利数: {user1_data.win_count}, 敗北数: {user1_data.loss_count}\n"
                              f"{user2.display_name} の勝利数: {user2_data.win_count}, 敗北数: {user2_data.loss_count}", ephemeral=True)
        else:
            # 勝敗数が0以下にならないため変更しない
            await ctx.respond("勝敗数が0以下になるため、変更できませんでした。", ephemeral=True)
    else:
        await ctx.respond("指定されたユーザーがデータベースで見つかりませんでした。", ephemeral=True)

@bot.slash_command(name="input_result", description="試合結果を手動で入力します。")
async def input_result(ctx: discord.ApplicationContext, result: int):
    if isinstance(ctx.channel, discord.Thread) and ctx.channel.parent_id == BATTLE_CHANNEL_ID:
        thread_id = ctx.channel.id
        await remove_role(ctx.user, "試合中")
        await ctx.respond(f"{ctx.user}がコマンドを使用しました。")
        # active_result_viewsからResultViewを取得
        result_view = active_result_views.get(thread_id)

        if result_view:
            # インタラクションをそのままhandle_resultに渡す
            await result_view.handle_result(ctx.interaction, result)
        else:
            await ctx.respond("このスレッドで進行中の試合は見つかりませんでした。", ephemeral=True)
    else:
        await ctx.respond("このコマンドは対戦スレッド内でのみ使用できます。", ephemeral=True)




# StayButtonView と StayButton の追加
class StayButtonView(discord.ui.View):
    def __init__(self, user_instance):
        super().__init__()
        self.user_instance = user_instance
        self.add_item(StayButton(user_instance))

class StayButton(discord.ui.Button):
    def __init__(self, user_instance):
        super().__init__(label="Stay機能を使用する", style=discord.ButtonStyle.primary)
        self.user_instance = user_instance

    async def callback(self, interaction: discord.Interaction):
        session = SessionLocal()
        user_id = str(interaction.user.id)
        user_instance = session.query(User).filter_by(discord_id=user_id).first()
        current_season = session.query(Season).order_by(Season.id.desc()).first()

        if user_instance:
            if user_instance.stay_flag == 0:
                # 確認メッセージを送信
                confirm_view = StayConfirmView(user_instance, current_season)
                await interaction.response.send_message(
                    "Stay 機能を使用すると、現在のレートと統計データが保存され、レートが 1500 にリセットされます。\n本当に実行しますか？",
                    view=confirm_view,
                    ephemeral=True
                )
            else:
                await interaction.response.send_message("あなたは既に今シーズンで Stay 機能を使用しました。", ephemeral=True)
        else:
            await interaction.response.send_message("ユーザー情報が見つかりません。ユーザー登録を行ってください。", ephemeral=True)
        session.close()

class StayConfirmView(discord.ui.View):
    def __init__(self, user_instance, current_season, timeout=60):
        super().__init__(timeout=timeout)
        self.user_instance = user_instance
        self.current_season = current_season

    @discord.ui.button(label="はい", style=discord.ButtonStyle.success)
    async def confirm(self, button: discord.ui.Button, interaction: discord.Interaction):
        session = SessionLocal()
        # ボタンを押したユーザーがコマンド実行者か確認
        if interaction.user.id != int(self.user_instance.discord_id):
            await interaction.response.send_message("このボタンはあなたのためのものではありません。", ephemeral=True)
            session.close()
            return

        # self.user_instance を現在のセッションにマージ
        user_instance = session.merge(self.user_instance)
        current_season = session.merge(self.current_season)

        # 現在のデータを user_season_record に保存
        existing_record = session.query(UserSeasonRecord).filter_by(user_id=user_instance.id, season_id=current_season.id).first()
        if existing_record:
            # 既にレコードが存在する場合は上書きしない
            pass
        else:
            new_record = UserSeasonRecord(
                user_id=user_instance.id,
                season_id=current_season.id,
                rating=user_instance.rating,
                rank=None,  # 順位はシーズン終了時に計算
                win_count=user_instance.win_count,
                loss_count=user_instance.loss_count,
                total_matches=user_instance.total_matches,
                win_streak=user_instance.win_streak,
                max_win_streak=user_instance.max_win_streak,
            )
            session.add(new_record)
            session.commit()

        # ユーザーの統計データをリセット
        user_instance.stayed_rating = user_instance.rating
        user_instance.rating = 1500
        user_instance.win_count = 0
        user_instance.loss_count = 0
        user_instance.total_matches = 0
        user_instance.win_streak = 0
        user_instance.max_win_streak = 0

        # stay_flag を 1 に設定
        user_instance.stay_flag = 1

        session.commit()
        session.close()

        await interaction.response.edit_message(content="Stay 機能を使用しました。あなたのレートと統計データはリセットされました。", view=None)
        
    @discord.ui.button(label="いいえ", style=discord.ButtonStyle.danger)
    async def cancel(self, button: discord.ui.Button, interaction: discord.Interaction):
        # ボタンを押したユーザーがコマンド実行者か確認
        if interaction.user.id != int(self.user_instance.discord_id):
            await interaction.response.send_message("このボタンはあなたのためのものではありません。", ephemeral=True)
            return

        await interaction.response.edit_message(content="Stay 操作はキャンセルされました。", view=None)

# ProfileView と ProfileButton の修正
class ProfileView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ProfileButton())

class ProfileButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="プロフィール表示", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction):
        session = SessionLocal()
        user_id = str(interaction.user.id)
        user_instance = session.query(User).filter_by(discord_id=user_id).first()
        try:
            if user_instance:
                # ユーザー情報の取得
                user_name = user_instance.user_name
                shadowverse_id = user_instance.shadowverse_id
                effective_rating = max(user_instance.rating, user_instance.stayed_rating or 0)
                rating = round(user_instance.rating, 3)
                trust_points = user_instance.trust_points
                win_count = user_instance.win_count
                loss_count = user_instance.loss_count

                from sqlalchemy import case, desc

                effective_rating_case = case(
                    (User.stayed_rating > User.rating, User.stayed_rating),
                    else_=User.rating
                ).label('effective_rating')

                # ユーザーの順位を計算（latest_season_matched が 1 のユーザーのみ）
                ranking = session.query(User.id, effective_rating_case).filter(User.latest_season_matched == 1).order_by(desc('effective_rating')).all()

                rank = None
                if user_instance.latest_season_matched == 1:
                    for idx, (uid, user_effective_rating) in enumerate(ranking, start=1):
                        if uid == user_instance.id:
                            rank = idx
                            break
                else:
                    rank = "未参加です"

                # プロフィールメッセージの作成
                profile_message = (
                    f"**ユーザープロフィール**\n"
                    f"ユーザー名 : {user_name}\n"
                    f"Shadowverse ID : {shadowverse_id}\n"
                    f"レーティング : {rating}\n"
                )

                # stayed_rating が存在する場合、その値を表示
                if user_instance.stayed_rating:
                    stayed_rating_rounded = round(user_instance.stayed_rating, 2)
                    profile_message += f"（Stay時のレート : {stayed_rating_rounded}）\n"

                profile_message += (
                    f"信用ポイント : {trust_points}\n"
                    f"勝敗 : {win_count}勝 {loss_count}敗\n"
                    f"順位 : {rank}\n"
                )

                # ビューを作成
                view = None

                # stay_flag が 0 の場合、StayButton を追加
                if user_instance.stay_flag == 0:
                    view = StayButtonView(user_instance)
                    profile_message += "\nあなたは stay 機能を使用できます。"

                await interaction.response.send_message(profile_message, ephemeral=True, view=view)
            else:
                await interaction.response.send_message("ユーザー情報が見つかりません。ユーザー登録を行ってください。", ephemeral=True)
        finally:
            # セッションの閉鎖
            session.close()

def count_characters(s):
    """全角も半角も1文字としてカウント"""
    count = 0
    for char in s:
        if unicodedata.east_asian_width(char) in ('F', 'W', 'A'):  # 全角・半角を区別せずにカウント
            count += 1
        else:
            count += 1
    return count

async def register_user(interaction, thread):
    """ユーザ情報を登録するメソッド"""
    username = str(interaction.user.display_name)
    user_id = interaction.user.id  # DiscordユーザーのIDを取得
    print(username)

    # 既存ユーザーをチェック
    try:
        existing_user = session.query(User).filter_by(discord_id=user_id).first()
        if existing_user and existing_user.rating and existing_user.trust_points:
            await thread.send("あなたはすでに登録されています。")
            await asyncio.sleep(8)
            await thread.delete()
            return

        # ユーザーにゲーム内の名前の入力を求める
        while True:
            await thread.send("ゲーム内で使用している名前を入力してください。名前は変更できないので注意してください。")

            def check(m):
                return m.author == interaction.user and m.channel == thread

            try:
                msg = await bot.wait_for('message', check=check, timeout=180.0)
                username = msg.content
                
                # ニックネームの長さを確認（12文字以内）
                if count_characters(username) > 12:
                    await thread.send("ニックネームは12文字以内にしてください（全角・半角問わず）。")
                    continue

                if not username:
                    await thread.send("無効な入力です。再度ゲーム内の名前を入力してください。")
                    continue

                await interaction.user.edit(nick=username)
                break
            except asyncio.TimeoutError:
                await thread.send("タイムアウトしました。もう一度お試しください。")
                await thread.delete()
                return

        # 新規ユーザーを作成
        user = User(
            user_name=username,
            discord_id=str(interaction.user.id),
            rating=1500,
            trust_points=100,
            stay_flag=False,
            win_streak=0,
            total_matches=0,
            max_win_streak=0,
            win_count=0,
            loss_count=0
        )

        # SHADOWVERSE_IDの入力を求める
        while not user.shadowverse_id:
            await thread.send("SHADOWVERSE_ID（9桁の数字）を入力してください：")

            try:
                msg = await bot.wait_for('message', check=check, timeout=180.0)
                user_id = msg.content
                if not user_id.isdigit() or len(user_id) != 9:
                    await thread.send("入力に不備があります。9桁の数字であることを確認し、やり直してください。")
                    continue
                user.shadowverse_id = user_id
            except asyncio.TimeoutError:
                await thread.send("登録がタイムアウトしました。もう一度お試しください。")
                await thread.delete()
                return

        session.add(user)

            # データベースにユーザー情報を保存
        session.commit()
        await thread.send(f"**ユーザー {username} の登録が完了しました。**")
    except Exception as e:
        session.rollback()
        print(f"エラーが発生しました: {e}")
    finally:
        await asyncio.sleep(6)
        await thread.delete()
        session.close()

@tasks.loop(hours=1)
async def update_rate_ranking():
    channel = bot.get_channel(RANKING_CHANNEL_ID)  # レーティングランキングを表示するチャンネルID
    if channel:
        await channel.purge()
        view = RankingView(session)
        await channel.send("ランキングを閲覧するにはボタンを押してください。レーティングランキングは1時間ごとに更新されます。", view=view)
        ranking_view = RankingView(session)
        await ranking_view.show_rate_ranking(channel)


async def update_stats_periodically():
    record_channel = bot.get_channel(RECORD_CHANNEL_ID)
    past_record_channel = bot.get_channel(PAST_RECORD_CHANNEL_ID)
    last50_record_channel = bot.get_channel(LAST_50_MATCHES_RECORD_CHANNEL_ID)
    while True:
        await record_channel.purge()
        await past_record_channel.purge()
        await last50_record_channel.purge()
        view1 = CurrentSeasonRecordView(session)
        view2 = PastSeasonRecordView(session)
        view3 = Last50RecordView(WinRecord(session))
        await record_channel.send(view=view1)
        await past_record_channel.send(view=view2)
        await last50_record_channel.send(view=view3)
        await asyncio.sleep(3600)


#起動処理
@bot.event
async def on_ready():
    print(f'We have logged in as {bot.user}')
    update_current_season_name()
    await bot.sync_commands()
    ranking_channel = bot.get_channel(RANKING_CHANNEL_ID)  # RANKING_CHANNEL_IDを指定
    await ranking_channel.purge()
    view = RankingView(session)
    await ranking_channel.send("ランキングを閲覧するにはボタンを押してください。レーティングランキングは1時間ごとに更新されます。", view=view)
    past_ranking_channel = bot.get_channel(PAST_RANING_CHANNEL_ID)  # RANKING_CHANNEL_IDを指定
    await past_ranking_channel.purge()
    view = RankingButtonView(session)
    await past_ranking_channel.send(view=view)
    welcome_channel = bot.get_channel(WELCOME_CHANNEL_ID)
    if welcome_channel:
        await welcome_channel.purge()
        view = RegisterView()
        await welcome_channel.send("**SV Ratingsへようこそ！**\n以下のボタンを押してユーザー登録を行ってください。詳しくは☑┊quick-startを参照してください。", view=view)
    else:
        print("WELCOME category not found.")
    # 1時間ごとにランキングを更新
    update_rate_ranking.start()
    # マッチングチャンネルにボタンを送信
    # MatchmakingView のインスタンスを作成してグローバル変数に保持
    # 部分一致でチャンネルを検索
    matching_channel = bot.get_channel(MATCHING_CHANNEL_ID)
    if matching_channel:
        await matching_channel.purge()
        await matching_channel.send("使用するクラスを選択してください。", view=MyView())
        await update_matchmaking_button(matching_channel)
    else:
        print("マッチングチャンネルが見つかりませんでした。")
    profile_channel = bot.get_channel(PROFILE_CHANNEL_ID)  # PROFILE_CHANNEL_IDを実際のチャンネルIDに置き換えてください
    if profile_channel:
        await profile_channel.purge()
        view = ProfileView()
        await profile_channel.send("プロフィールを表示するには以下のボタンを押してください。", view=view)
    else:
        print("プロフィールチャンネルが見つかりませんでした。")
    bot.loop.create_task(update_stats_periodically())

async def update_matchmaking_button(channel):
    """マッチングチャンネルでボタンを更新する関数"""
    # 以前のメッセージを削除
    view = MatchmakingView()  # 既存インスタンスがあればそれを使用
    await channel.send("マッチングを開始するにはボタンをクリックしてください\nマッチングが成功したらbattleチャンネルにスレッドが作成されます そちらで対戦を行ってください", view=view)

@bot.slash_command(name="trust_report", description="ユーザーの信用ポイントを減点します", default_permission=False)
@commands.has_permissions(administrator=True)
async def trust_report(interaction: discord.Interaction, user: discord.Member, points: int):
    # ユーザーをデータベースから取得
    user_instance = session.query(User).filter_by(discord_id=user.id).first()

    if not user_instance:
        await interaction.response.send_message(f"ユーザー {user.display_name} がデータベースに見つかりませんでした。", ephemeral=True)
        return

    # 信用ポイントを減点
    user_instance.trust_points = user_instance.trust_points - points
    credit = user_instance.trust_points

    # 変更をデータベースに保存
    session.commit()

    await interaction.response.send_message(f"{user.display_name} さんに {points} ポイントの減点が適用されました。現在の信用ポイント: {credit}")

    # 信用ポイントが60未満の場合の処理
    if credit < 60:
        await interaction.followup.send(f"{user.display_name} さんの信用ポイントが60未満です。必要な対応を行ってください。")
    session.close()  # セッションを閉じる


active_users = {}



@bot.command()
@commands.has_permissions(administrator=True)
async def start_season(ctx, season_name: str):
    """新しいシーズンを開始するコマンド"""
    # 最新のシーズンを取得
    last_season = session.query(Season).order_by(desc(Season.id)).first()

    if last_season and last_season.end_date is None:
        await ctx.send("前のシーズンが終了していないため、新しいシーズンを開始できません。")
        return

    # 新しいシーズンを作成
    new_season = Season(season_name=season_name, start_date=datetime.now(), created_at=datetime.now())
    session.add(new_season)
    session.commit()
    update_current_season_name()
    await ctx.send(f"'{season_name}' が開始されました！")
    #マッチングボタンの表示
    matching_channel = bot.get_channel(MATCHING_CHANNEL_ID)
    if matching_channel:
        await matching_channel.purge()
        await matching_channel.send("使用するクラスを選択してください。", view=MyView())
        await update_matchmaking_button(matching_channel)

@bot.command()
@commands.has_permissions(administrator=True)
async def end_season(ctx):
    """現在のシーズンを終了するコマンド"""
    # 最新のシーズンを取得
    last_season = session.query(Season).order_by(desc(Season.id)).first()

    if not last_season or last_season.end_date is not None:
        await ctx.send("終了するシーズンが見つかりません。")
        return

    # シーズンを終了
    last_season.end_date = datetime.now()
    session.commit()

    # シーズン統計を集計
    win_record = WinRecord(session)
    win_record.totalize_season(last_season.id)

    # 全ユーザーのレートやポイントをリセット
    users = session.query(User).all()
    for user in users:
        # ユーザーのレーティングを1500にリセット
        user.rating = 1500
        user.latest_season_matched = 0
        # trust_pointsを1増やす（最大100）
        if user.trust_points < 100:
            user.trust_points += 1
        
        user.stayed_rating = None
        user.total_matches = 0
        user.win_streak = 0
        user.max_win_streak = 0  # 最大連勝数をリセット
        user.win_count = 0  # 勝利数をリセット
        user.loss_count = 0  # 敗北数をリセット
        # stay_flagを0にリセット
        user.stay_flag = 0

    session.commit()
    # 現在のシーズン名を更新
    update_current_season_name()
    await ctx.send(f"シーズン '{last_season.season_name}' が終了しました。")
    #マッチングボタンの削除
    matching_channel = bot.get_channel(MATCHING_CHANNEL_ID)
    if matching_channel:
        await matching_channel.purge()
        await matching_channel.send("シーズン開始前のため対戦できません")

@bot.event
async def on_error(event_method, *args, **kwargs):
    with open('errorlog.txt', 'a', encoding='utf-8') as f:
        current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        f.write(f'[{current_time}] イベント "{event_method}" でエラーが発生しました。\n')
        traceback.print_exc(file=f)
        f.write('\n')

bot.run(bot_token)