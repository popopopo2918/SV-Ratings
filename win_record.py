import discord
from discord.ui import Button, View, Select
from sqlalchemy import create_engine, Column, Integer, String, desc
from sqlalchemy.ext.automap import automap_base
from sqlalchemy.orm import Session
import matplotlib.pyplot as plt
import io
import asyncio

# データベースの設定
db_path = 'db/shadowverse_bridge.db'
engine = create_engine(f'sqlite:///{db_path}', echo=True)
# 自動マッピング用のベースクラスの準備
Base = automap_base()

# データベース内のテーブルをすべて反映
Base.prepare(engine, reflect=True)

# マッピングされたクラスの取得
User = Base.classes.user  # テーブル名が 'user' だと仮定
Class = Base.classes.deck_class
MatchHistory = Base.classes.match_history  # テーブル名が 'match_history' だと仮定
Season = Base.classes.season  # テーブル名が 'season' だと仮定
UserSeasonRecord = Base.classes.user_season_record
session = Session(engine)

class CurrentSeasonRecord:
    def __init__(self, session):
        self.session = session

    def get_current_season(self):
        season = self.session.query(Season).order_by(Season.id.desc()).first()
        return season

    async def show_class_select(self, interaction):
        user = self.session.query(User).filter_by(discord_id=str(interaction.user.id)).first()
        # latest_season_matched が False なら "未参加です" と返して終了
        if user and not user.latest_season_matched:
            await interaction.response.send_message("未参加です", ephemeral=True)
            return
        season = self.get_current_season()
        if season:
            await interaction.response.send_message(
                content="クラスを選択してください:", 
                view=ClassSelectView(season_id=season.id), 
                ephemeral=True
            )
        else:
            await interaction.response.send_message("シーズンが見つかりません。", ephemeral=True)

class CurrentSeasonRecordView(discord.ui.View):
    def __init__(self, session):
        super().__init__(timeout=None)
        self.session = session
        button = discord.ui.Button(label="現在のシーズン", style=discord.ButtonStyle.primary)

        async def button_callback(interaction):
            record = CurrentSeasonRecord(self.session)
            await record.show_class_select(interaction)

        button.callback = button_callback
        self.add_item(button)

class PastSeasonRecordView(discord.ui.View):
    def __init__(self, session):
        super().__init__(timeout=None)
        self.session = session
        button = discord.ui.Button(label="過去のシーズン", style=discord.ButtonStyle.secondary)

        async def button_callback(interaction):
            record = PastSeasonRecord(self.session)
            await record.show_season_select(interaction)

        button.callback = button_callback
        self.add_item(button)

class PastSeasonRecord:
    def __init__(self, session):
        self.session = session

    def get_past_seasons(self):
        # 最新シーズンを取得
        latest_season = self.session.query(Season).order_by(Season.id.desc()).first()
        # 最新シーズンを除いたシーズン一覧を取得
        seasons = self.session.query(Season).filter(Season.id != latest_season.id).order_by(Season.id.desc()).all()
        return seasons

    async def show_season_select(self, interaction):
        seasons = self.get_past_seasons()
        options = [
            discord.SelectOption(label="全シーズン", value="all")
        ]
        used_values = set()
        for season in seasons:
            value = str(season.id)
            if value in used_values:
                # 重複を避けるためにユニークな値を生成
                value = f"{season.id}_{season.season_name}"
            options.append(discord.SelectOption(label=season.season_name, value=value))
            used_values.add(value)
        
        select = discord.ui.Select(placeholder="シーズンを選択してください...", options=options)

        async def select_callback(select_interaction):
            if not select_interaction.response.is_done():
                await select_interaction.response.defer(ephemeral=True)
            selected_season_id = select_interaction.data['values'][0]
            if selected_season_id == "all":
                # 全シーズンを選択した場合、season_id を None にして ClassSelectView を呼び出す
                await select_interaction.followup.send(
                    content="クラスを選択してください:", 
                    view=ClassSelectView(season_id=None),
                    ephemeral=True
                )
            else: 
                selected_season_id = int(selected_season_id)
                user = self.session.query(User).filter_by(discord_id=str(select_interaction.user.id)).first()
                if not user:
                    await select_interaction.followup.send("ユーザーが見つかりません。", ephemeral=True)
                    return

            # ユーザーが選択したシーズンに参加しているか確認
                user_record = self.session.query(UserSeasonRecord).filter_by(user_id=user.id, season_id=selected_season_id).first()
                if not user_record:
                    # 参加していなかった場合 "未参加です。" と返す
                    message = await select_interaction.followup.send("未参加です。", ephemeral=True)
                    await asyncio.sleep(10)
                    await message.delete()
                    return
            # ユーザーがシーズンに参加している場合、クラスを選択させる
                await select_interaction.followup.send(
                    content="クラスを選択してください:", 
                    view=ClassSelectView(season_id=selected_season_id),
                    ephemeral=True
                )

        select.callback = select_callback
        view = discord.ui.View()
        view.add_item(select)
        await interaction.response.send_message("シーズンを選択してください:", view=view, ephemeral=True)
        await asyncio.sleep(15)
        # インタラクションメッセージを削除する
        try:
            await interaction.delete_original_response()
        except discord.errors.NotFound:
            # メッセージが見つからなかった場合はエラーを無視
            pass

class Last50Record:
    def __init__(self, win_record):
        self.win_record = win_record

    async def show_button(self, interaction):
        button = discord.ui.Button(label="直近50戦", style=discord.ButtonStyle.primary)

        async def button_callback(button_interaction):
            await self.win_record.show_recent50_stats(button_interaction, interaction.user.id)

        button.callback = button_callback
        view = discord.ui.View()
        view.add_item(button)
        await interaction.response.send_message("直近50戦の統計を見るにはボタンを押してください:", view=view, ephemeral=True)

class Last50RecordView(discord.ui.View):
    def __init__(self, win_record):
        super().__init__(timeout=None)
        self.win_record = win_record

        button = discord.ui.Button(label="直近50戦", style=discord.ButtonStyle.primary)

        async def button_callback(button_interaction):
            await self.win_record.show_recent50_stats(button_interaction, button_interaction.user.id)

        button.callback = button_callback
        self.add_item(button)

class ClassSelect(discord.ui.Select):
    """クラス選択を行う処理。"""
    def __init__(self, season_id=None):
        self.session = session
        # データベースからクラス名を取得
        class_names = self.session.query(Class.class_name).all()
        valid_classes = [name[0] for name in class_names]  # クラス名をリストに変換

        options = [
            discord.SelectOption(label="全クラス", value="all_classes")
        ] + [discord.SelectOption(label=cls) for cls in valid_classes]

        super().__init__(placeholder="クラスを選択してください...", min_values=1, max_values=2, options=options)
        self.season_id = season_id

    async def callback(self, interaction: discord.Interaction):
        selected_classes = self.values
        user_id = interaction.user.id  # 操作したユーザーのDiscord IDを取得
        win_record = WinRecord(session)

        # 全クラスと他のクラスが選ばれている場合のチェック
        if "all_classes" in selected_classes and len(selected_classes) > 1:
            await interaction.response.send_message("全クラスと他のクラスを同時に選択することはできません。", ephemeral=True)
            return

        # インタラクションのレスポンスを一度行う
        await interaction.response.defer(ephemeral=True)

        if "all_classes" in selected_classes:
            if self.season_id:
                await win_record.show_season_stats(interaction, user_id, self.season_id)
            else:
                await win_record.show_all_time_stats(interaction, user_id)
        else:
            if len(selected_classes) == 2:
                # 2つのクラスを選択した場合、クラスの組み合わせに完全一致する試合を取得
                await win_record.show_class_stats(interaction, user_id, selected_classes, self.season_id)
            else:
                # 1つのクラスのみ選択した場合、そのクラスに関連する試合を取得
                await win_record.show_class_stats(interaction, user_id, selected_classes[0], self.season_id)

        # インタラクションメッセージを削除する
        try:
            await interaction.delete_original_response()
        except discord.errors.NotFound:
            # メッセージが見つからなかった場合はエラーを無視
            pass
        

class ClassSelectView(discord.ui.View):
    def __init__(self, season_id=None):
        super().__init__(timeout=None)
        self.add_item(ClassSelect(season_id))

class WinRecord:
    def __init__(self, session: Session):
        self.session = session

    def totalize_season(self, season_id):
        """シーズン終了時に全ユーザーのシーズン統計を user_season_record に保存"""
        users = self.session.query(User).filter(User.latest_season_matched == True).all()
        season = self.session.query(Season).filter_by(id=season_id).first()
        if not season:
            raise ValueError(f"Season with ID {season_id} not found.")
        season_name = season.season_name

        # ユーザーごとの最終レートを計算し、順位付けのためにリストに格納
        user_final_ratings = []
        for user in users:
            # 効果的レートを計算
            final_rating = max(user.rating, user.stayed_rating or 0)
            user_final_ratings.append((user.id, final_rating))

        # 効果的レートでユーザーをソートして順位を計算
        user_final_ratings.sort(key=lambda x: x[1], reverse=True)

        user_rankings = {}
        current_rank = 1
        previous_rating = None
        for idx, (user_id, final_rating) in enumerate(user_final_ratings):
            if final_rating != previous_rating:
                current_rank = idx + 1
            user_rankings[user_id] = current_rank
            previous_rating = final_rating

        # 各ユーザーの統計情報を保存
        for user in users:
            # 効果的レートを取得
            final_rating = max(user.rating, user.stayed_rating or 0)

            # ユーザーの stay_flag と stayed_rating を確認
            existing_record = self.session.query(UserSeasonRecord).filter_by(
                user_id=user.id,
                season_id=season_id
            ).first()

            if user.stay_flag == 1:
                if user.rating > user.stayed_rating:
                    # rating の方が高い場合、既存のレコードを上書き
                    if existing_record:
                        existing_record.rating = user.rating
                        existing_record.win_count = user.win_count
                        existing_record.loss_count = user.loss_count
                        existing_record.total_matches = user.total_matches
                        existing_record.max_win_streak = user.max_win_streak
                        existing_record.rank = user_rankings[user.id]
                    else:
                        # レコードが存在しない場合、新規作成
                        new_record = UserSeasonRecord(
                            user_id=user.id,
                            season_id=season_id,
                            rating=user.rating,
                            rank=user_rankings[user.id],
                            win_count=user.win_count,
                            loss_count=user.loss_count,
                            total_matches=user.total_matches,
                            max_win_streak=user.max_win_streak,
                        )
                        self.session.add(new_record)
                else:
                    # rating <= stayed_rating の場合、既存のレコードの rank を更新
                    if existing_record:
                        existing_record.rank = user_rankings[user.id]
            else:
                # stay_flag が 0 のユーザー
                if existing_record:
                    # 既存のレコードがある場合、データを上書き
                    existing_record.rating = final_rating
                    existing_record.win_count = user.win_count
                    existing_record.loss_count = user.loss_count
                    existing_record.total_matches = user.total_matches
                    existing_record.max_win_streak = user.max_win_streak
                    existing_record.rank = user_rankings[user.id]
                else:
                    # レコードが存在しない場合、新規作成
                    new_record = UserSeasonRecord(
                        user_id=user.id,
                        season_id=season_id,
                        rating=final_rating,
                        rank=user_rankings[user.id],
                        win_count=user.win_count,
                        loss_count=user.loss_count,
                        total_matches=user.total_matches,
                        max_win_streak=user.max_win_streak,
                    )
                    self.session.add(new_record)

        self.session.commit()

    async def show_all_time_stats(self, interaction: discord.Interaction, user_id):
        """全シーズン累計の統計を表示"""
        user = self.session.query(User).filter_by(discord_id=str(user_id)).first()
        if user:
            # user_season_recordから全シーズンの勝敗数を集計
            records = self.session.query(UserSeasonRecord).filter_by(user_id=user.id).all()
            total_win_count = sum(record.win_count for record in records)
            total_loss_count = sum(record.loss_count for record in records)
            total_count = total_win_count + total_loss_count
            win_rate = (total_win_count / total_count) * 100 if total_count > 0 else 0

            message = await interaction.followup.send(
                f"{user.user_name} の全シーズン勝率: {win_rate:.2f}%\n{total_count}戦   {total_win_count}勝-{total_loss_count}敗",
                ephemeral=True
            )
        else:
            message = await interaction.followup.send("ユーザーが見つかりません。", ephemeral=True)
        await asyncio.sleep(10)
        try:
            await message.delete()
        except discord.errors.NotFound:
            pass

    async def show_season_stats(self, interaction: discord.Interaction, user_id, season_id):
        """指定されたシーズンの統計を表示"""
        user = self.session.query(User).filter_by(discord_id=str(user_id)).first()

        # `season_id` から `season_name` を取得
        season = self.session.query(Season).filter_by(id=season_id).first()
        if not season:
            await interaction.followup.send("指定されたシーズンが見つかりません。", ephemeral=True)
            return

        season_name = season.season_name
        # 最新シーズンかどうかを判定するために、seasonテーブルからidが一番大きく、end_dateがnullのものを取得する
        latest_season = self.session.query(Season).filter(Season.end_date == None).order_by(Season.id.desc()).first()

        # 最新シーズンかどうかの判定
        is_latest_season = (season_name == latest_season.season_name if latest_season else False)

        if user:
            if is_latest_season:
                # 最新シーズンの場合、userテーブルからデータを取得
                win_count = user.win_count
                loss_count = user.loss_count
                total_count = win_count + loss_count
                win_rate = (win_count / total_count) * 100 if total_count > 0 else 0

                # 最新シーズンのレートと順位を計算
                final_rating = user.rating
                rank = self.session.query(User).filter(User.rating > final_rating).count() + 1

            else:
                # 過去シーズンの場合、PastSeasonRecordからデータを取得
                past_record = self.session.query(UserSeasonRecord).filter_by(user_id=user.id, season_id=season_id).first()
                if not past_record:
                    await interaction.followup.send("過去シーズンのレコードが見つかりません。", ephemeral=True)
                    return

                final_rating = past_record.rating
                rank = past_record.rank
                win_count = past_record.win_count
                loss_count = past_record.loss_count
                total_count = win_count + loss_count
                win_rate = (win_count / total_count) * 100 if total_count > 0 else 0

            message = await interaction.followup.send(
                f"{user.user_name} のシーズン {season_name} 統計:\n"
                f"勝率: {win_rate:.2f}% ({total_count}戦 {win_count}勝-{loss_count}敗)\n"
                f"レート: {final_rating:.2f}\n"
                f"順位: {rank}位",
                ephemeral=True
            )
        else:
            message = await interaction.followup.send("ユーザーが見つかりません。", ephemeral=True)

        await asyncio.sleep(10)
        try:
            await message.delete()
        except discord.errors.NotFound:
            pass

    async def show_date_range_stats(self, interaction: discord.Interaction, user_id, start_date, end_date):
        """指定された日付範囲の統計を表示"""
        user = self.session.query(User).filter_by(discord_id=str(user_id)).first()
        if user:
            matches = self.session.query(MatchHistory).filter(
                ((MatchHistory.winner_user_id == user.id) | (MatchHistory.loser_user_id == user.id)) &
                (MatchHistory.match_date.between(start_date, end_date))
            ).all()
            win_count = sum(1 for match in matches if match.winner_user_id == user.id)
            total_count = len(matches)
            loss_count = total_count - win_count
            win_rate = (win_count / total_count) * 100 if total_count > 0 else 0
            await interaction.followup.send(
                f"{user.user_name} の {start_date} から {end_date} の間の勝率: {win_rate:.2f}%\n{total_count}戦   {win_count}勝-{loss_count}敗", 
                ephemeral=True
            )
        else:
            await interaction.followup.send("ユーザーが見つかりません。", ephemeral=True)

    async def show_vs_stats(self, interaction: discord.Interaction, user_id, opponent_id):
        """指定された相手との対戦履歴を表示"""
        user = self.session.query(User).filter_by(discord_id=str(user_id)).first()
        opponent = self.session.query(User).filter_by(discord_id=str(opponent_id)).first()
        if user and opponent:
            matches = self.session.query(MatchHistory).filter(
                ((MatchHistory.winner_user_id == user.id) & (MatchHistory.loser_user_id == opponent.id)) |
                ((MatchHistory.winner_user_id == opponent.id) & (MatchHistory.loser_user_id == user.id))
            ).all()
            win_count = sum(1 for match in matches if match.winner_user_id == user.id)
            total_count = len(matches)
            loss_count = total_count - win_count
            win_rate = (win_count / total_count) * 100 if total_count > 0 else 0
            message = await interaction.followup.send(
                f"{user.user_name} vs {opponent.user_name} 勝率:{win_rate:.2f}%\n{total_count}戦   {win_count}勝-{loss_count}敗", 
                ephemeral=True
            )
        else:
            message = await interaction.followup.send("ユーザーまたは対戦相手が見つかりません。", ephemeral=True)
        await asyncio.sleep(10)
        try:
            await message.delete()
        except discord.errors.NotFound:
            # メッセージが見つからなかった場合はエラーを無視
            pass
    async def show_recent50_stats(self, interaction: discord.Interaction, user_id):
        """最新のシーズンの直近50戦のレート推移のグラフと統計を表示"""
        # インタラクションユーザーのIDからuserテーブルのIDを取得
        user = self.session.query(User).filter_by(discord_id=str(user_id)).first()
        if not user:
            await interaction.response.send_message("ユーザーが見つかりません。", ephemeral=True)
            return

        # 最新のシーズンを取得
        latest_season = self.session.query(Season).order_by(Season.id.desc()).first()
        if not latest_season:
            await interaction.response.send_message("最新のシーズンが見つかりません。", ephemeral=True)
            return

        if user and not user.latest_season_matched:
            await interaction.response.send_message("未参加です", ephemeral=True)
            return

        # クラス名と略字の対応
        class_abbreviations = {
            "エルフ": "E",
            "ロイヤル": "R",
            "ヴァンパイア": "V",
            "ウィッチ": "W",
            "ネクロマンサー": "Nc",
            "ドラゴン": "D",
            "ビショップ": "B",
            "ネメシス": "Nm"
        }

        # 該当するmatch_historyを取得（最新シーズンのみ）
        matches_query = self.session.query(MatchHistory).filter(
            ((MatchHistory.user1_id == user.id) | (MatchHistory.user2_id == user.id)) &
            (MatchHistory.season_name == latest_season.season_name)
        ).order_by(MatchHistory.match_date.desc())

        total_matches = matches_query.count()

        # 初期レートと試合リストの設定
        if total_matches >= 50:
            # 最新50戦を取得
            latest_50_matches = matches_query.limit(50).all()
            # 試合を古い順に並べ替えてレート推移を計算
            matches_for_graph = list(reversed(latest_50_matches))  # グラフ用（古い順）
            matches_for_embed = latest_50_matches  # Embed用（新しい順）
            title_suffix = " (最新50戦)"
        else:
            # 総試合数が50未満の場合、初期レートを1500とする
            initial_rating = 1500
            # 試合を古い順に取得
            matches_for_graph = self.session.query(MatchHistory).filter(
                ((MatchHistory.user1_id == user.id) | (MatchHistory.user2_id == user.id)) &
                (MatchHistory.season_name == latest_season.season_name)
            ).order_by(MatchHistory.match_date.asc()).all()
            matches_for_embed = list(reversed(matches_for_graph))  # Embed用（新しい順）
            title_suffix = f" (最新{total_matches}戦)" if total_matches > 0 else ""

        # レート推移の初期化
        initial_rating = 1500 if total_matches < 50 else user.rating - sum(
            match.user1_rating_change if match.user1_id == user.id else match.user2_rating_change
            for match in matches_for_graph
        )
        ratings = [initial_rating]
        win_count = 0
        loss_count = 0
        class_stats = {}

        # 対戦履歴のリスト
        match_entries = []

        # グラフ用のデータ計算
        for match in matches_for_graph:
            # 勝敗の判定
            if match.winner_user_id == user.id:
                result = "WIN"
                win_count += 1
            else:
                result = "LOSE"
                loss_count += 1

            # レーティング変動の取得
            if match.user1_id == user.id:
                rating_change = match.user1_rating_change
            else:
                rating_change = match.user2_rating_change

            # レーティングの更新
            current_rating = ratings[-1] + rating_change
            ratings.append(current_rating)

            # クラスごとの勝敗を集計
            if match.user1_id == user.id:
                user_classes = (match.user1_class_a, match.user1_class_b)
            else:
                user_classes = (match.user2_class_a, match.user2_class_b)

            key = ','.join([class_abbreviations.get(c, c) for c in user_classes if c])
            if key not in class_stats:
                class_stats[key] = {'win': 0, 'loss': 0}
            if result == "WIN":
                class_stats[key]['win'] += 1
            else:
                class_stats[key]['loss'] += 1

        total_count = win_count + loss_count
        win_rate = (win_count / total_count) * 100 if total_count > 0 else 0

        # 順位を計算 (例: レートが高い順にランキング)
        rank = self.session.query(User).filter(User.rating > user.rating).count() + 1

        # Matplotlibのフォント設定（日本語対応）
        plt.rcParams['font.family'] = 'Yu Gothic'  # 適切な日本語フォントを指定してください

        # グラフを作成
        plt.figure(figsize=(10, 6))
        plt.plot(range(len(ratings)), ratings, marker='o')
        plt.title(f"レーティンググラフ{title_suffix}")
        plt.xlabel("試合数")
        plt.ylabel("レーティング")

        # グラフの上下の幅を設定
        min_rating = min(ratings)
        max_rating = max(ratings)
        y_min = (min_rating // 100) * 100 - 50
        y_max = ((max_rating + 99) // 100) * 100 + 50
        plt.ylim(y_min, y_max)

        # グリッドを追加
        plt.grid(True)

        buf = io.BytesIO()
        plt.savefig(buf, format='png')
        buf.seek(0)
        plt.close()

        # クラスごとの勝率情報を生成
        if class_stats:
            class_stats_message = "\n".join([
                f"{cls}　{stats['win']}勝-{stats['loss']}敗　{(stats['win'] / (stats['win'] + stats['loss']) * 100):.2f}%"
                for cls, stats in class_stats.items()
            ])
        else:
            class_stats_message = "クラスごとの戦績はありません。"

        # 統計のメッセージを作成
        stats_message = (
            f"最新シーズンの試合履歴{title_suffix}\n"
            f"{total_count}戦　{win_count}勝-{loss_count}敗　勝率: {win_rate:.2f}%\n\n"
            f"現在のレーティング: {ratings[-1]:.2f}\n\n"
            f"現在の順位: {rank}位\n\n"
            f"{class_stats_message}"
        )

        # 対戦履歴をページングして表示
        match_entries = []

        for idx, match in enumerate(matches_for_embed, start=1):
            # 勝敗の判定
            if match.winner_user_id == user.id:
                result = "**```WIN```**"
                # フィールド名のスペース調整
                spacing = "　"  # 全角スペース
            else:
                result = "**```LOSE```**"
                spacing = "  "  # 半角スペース2つ

            # レーティング変動の取得
            if match.user1_id == user.id:
                opponent_id = match.user2_id
                rating_change = match.user1_rating_change
                user_classes = (match.user1_class_a, match.user1_class_b)
                opponent_classes = (match.user2_class_a, match.user2_class_b)
            else:
                opponent_id = match.user1_id
                rating_change = match.user2_rating_change
                user_classes = (match.user2_class_a, match.user2_class_b)
                opponent_classes = (match.user1_class_a, match.user1_class_b)

            # 対戦相手の名前を取得
            opponent = self.session.query(User).filter_by(id=opponent_id).first()
            opponent_name = opponent.user_name if opponent else "Unknown"

            # クラス名を略字に変換
            user_class_abbr = ','.join([class_abbreviations.get(c, c) for c in user_classes if c])
            opponent_class_abbr = ','.join([class_abbreviations.get(c, c) for c in opponent_classes if c])

            # 対戦履歴のエントリを作成
            field_name = f"{result}{spacing}{opponent_name}"
            field_value = f"{user_class_abbr} vs {opponent_class_abbr} {rating_change:+.2f}"
            match_entries.append((field_name, field_value))

        # Embedの作成
        pages = [match_entries[i:i+10] for i in range(0, len(match_entries), 10)]
        embeds = []

        for page_num, page_entries in enumerate(pages, start=1):
            embed = discord.Embed(
                title=f"{user.user_name} の直近の対戦履歴 - ページ {page_num}/{len(pages)}",
                color=discord.Color.blue()
            )
            for field_name, field_value in page_entries:
                embed.add_field(name=field_name, value=field_value, inline=False)
            embeds.append(embed)

        # ページング用のViewを作成
        view = self.MatchHistoryPaginator(embeds)

        # 統計メッセージを送信
        await interaction.response.send_message(stats_message, ephemeral=True)

        # グラフ画像を送信
        graph_message = await interaction.followup.send(file=discord.File(buf, 'recent50.png'), ephemeral=True)

        # 最初のページを送信
        history_message = await interaction.followup.send(embed=embeds[0], view=view, ephemeral=True)

        # 10分後（600秒）にメッセージを削除
        await asyncio.sleep(600)
        try:
            await history_message.delete()
            await graph_message.delete()
            await interaction.delete_original_response()
        except discord.errors.NotFound:
            pass

            # クラスごとの勝敗を集計
            if match.user1_id == user.id:
                user_classes = (match.user1_class_a, match.user1_class_b)
            else:
                user_classes = (match.user2_class_a, match.user2_class_b)

            key = ','.join([class_abbreviations.get(c, c) for c in user_classes if c])
            if key not in class_stats:
                class_stats[key] = {'win': 0, 'loss': 0}
            if result == "WIN":
                class_stats[key]['win'] += 1
            else:
                class_stats[key]['loss'] += 1

    class MatchHistoryPaginator(discord.ui.View):
        def __init__(self, embeds):
            super().__init__(timeout=600)  # タイムアウトを設定
            self.embeds = embeds
            self.current = 0

        @discord.ui.button(label="前へ", style=discord.ButtonStyle.primary)
        async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
            if self.current > 0:
                self.current -= 1
                await interaction.response.edit_message(embed=self.embeds[self.current], view=self)
            else:
                await interaction.response.defer()

        @discord.ui.button(label="次へ", style=discord.ButtonStyle.primary)
        async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
            if self.current < len(self.embeds) - 1:
                self.current += 1
                await interaction.response.edit_message(embed=self.embeds[self.current], view=self)
            else:
                await interaction.response.defer()

    async def show_class_stats(self, interaction: discord.Interaction, user_id, selected_classes, season_id=None):
        """指定されたクラスでの戦績を表示"""
        user = self.session.query(User).filter_by(discord_id=str(user_id)).first()
        if not user:
            message = await interaction.followup.send("ユーザーが見つかりません。", ephemeral=True)
            await asyncio.sleep(300)
            try:
                await message.delete()
            except discord.errors.NotFound:
                # メッセージが見つからなかった場合はエラーを無視
                pass
            return

        # クエリを作成
        if isinstance(selected_classes, list) and len(selected_classes) == 2:
            class1, class2 = selected_classes
            query = self.session.query(MatchHistory).filter(
                ((MatchHistory.user1_id == user.id) & 
                (((MatchHistory.user1_class_a == class1) & (MatchHistory.user1_class_b == class2)) |
                ((MatchHistory.user1_class_a == class2) & (MatchHistory.user1_class_b == class1)))) |
                ((MatchHistory.user2_id == user.id) & 
                (((MatchHistory.user2_class_a == class1) & (MatchHistory.user2_class_b == class2)) |
                ((MatchHistory.user2_class_a == class2) & (MatchHistory.user2_class_b == class1))))
            )
            selected_class_str = f"{class1} と {class2}"
        else:
            selected_class = selected_classes[0] if isinstance(selected_classes, list) else selected_classes
            query = self.session.query(MatchHistory).filter(
                ((MatchHistory.user1_id == user.id) & 
                ((MatchHistory.user1_class_a == selected_class) | (MatchHistory.user1_class_b == selected_class))) |
                ((MatchHistory.user2_id == user.id) & 
                ((MatchHistory.user2_class_a == selected_class) | (MatchHistory.user2_class_b == selected_class)))
            )
            selected_class_str = selected_class

        if season_id is not None:
            season_name = self.session.query(Season).filter_by(id=season_id).first().season_name
            query = query.filter(MatchHistory.season_name == season_name)

        matches = query.all()
        win_count = sum(1 for match in matches if match.winner_user_id == user.id)
        total_count = len(matches)
        loss_count = total_count - win_count
        win_rate = (win_count / total_count) * 100 if total_count > 0 else 0

        # メッセージを送信し、30秒後に削除
        message = await interaction.followup.send(
            f"{user.user_name} の {selected_class_str} クラスでの戦績:\n"
            f"勝率: {win_rate:.2f}%\n"
            f"{total_count}戦   {win_count}勝-{loss_count}敗", 
            ephemeral=True
        )
        await asyncio.sleep(300)
        try:
            await message.delete()
        except discord.errors.NotFound:
            # メッセージが見つからなかった場合はエラーを無視
            pass


