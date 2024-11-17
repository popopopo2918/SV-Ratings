import sqlite3
from datetime import datetime
import logging

# ログの設定
logging.basicConfig(
    filename='errorlog.txt',  # ログを保存するファイル名
    level=logging.ERROR,  # ログのレベルをエラーに設定
    format='%(asctime)s - %(levelname)s - %(message)s'  # ログのフォーマット
)

# データベースに接続
try:
    conn = sqlite3.connect('db/shadowverse_bridge.db')
    cursor = conn.cursor()

    classes = ['エルフ', 'ロイヤル', 'ウィッチ', 'ドラゴン', 'ネクロマンサー', 'ヴァンパイア', 'ビショップ', 'ネメシス']

    # ユーザーテーブルの作成または更新
    create_user_table = '''
    CREATE TABLE IF NOT EXISTS user (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        discord_id TEXT UNIQUE,
        user_name TEXT,
        shadowverse_id TEXT UNIQUE,
        rating INTEGER DEFAULT 1500,
        stayed_rating INTEGER,
        trust_points INTEGER DEFAULT 100,
        stay_flag BOOLEAN DEFAULT 0,
        total_matches INTEGER DEFAULT 0,
        win_streak INTEGER DEFAULT 0,
        max_win_streak INTEGER DEFAULT 0,
        win_count INTEGER DEFAULT 0,
        loss_count INTEGER DEFAULT 0,
        latest_season_matched BOOLEAN DEFAULT 0,
        cancelled_matched_count INTEGER DEFAULT 0,
        class1 TEXT,
        class2 TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (class1) REFERENCES deck_class(class_name),
        FOREIGN KEY (class2) REFERENCES deck_class(class_name)
    )
    '''

    # マッチ履歴テーブルの作成
    create_match_history_table = '''
    CREATE TABLE IF NOT EXISTS match_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user1_id INTEGER,
        user2_id INTEGER,
        match_date TEXT,
        season_name TEXT,
        user1_class_a TEXT,
        user1_class_b TEXT,
        user2_class_a TEXT,
        user2_class_b TEXT,
        user1_rating_change INTEGER, 
        user2_rating_change INTEGER,  
        winner_user_id INTEGER,       
        loser_user_id INTEGER,
        FOREIGN KEY (user1_id) REFERENCES user(id),
        FOREIGN KEY (user2_id) REFERENCES user(id),
        FOREIGN KEY (winner_user_id) REFERENCES user(id), 
        FOREIGN KEY (loser_user_id) REFERENCES user(id),  
        FOREIGN KEY (season_name) REFERENCES season(season_name),
        FOREIGN KEY (user1_class_a) REFERENCES deck_class(class_name),
        FOREIGN KEY (user1_class_b) REFERENCES deck_class(class_name),
        FOREIGN KEY (user2_class_a) REFERENCES deck_class(class_name),
        FOREIGN KEY (user2_class_b) REFERENCES deck_class(class_name)
    )
    '''

    # ゲーム履歴テーブルの作成
    create_game_history_table = '''
    CREATE TABLE IF NOT EXISTS game_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        match_history_id INTEGER,
        winner_user_id INTEGER,
        user1_class TEXT,
        user2_class TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (match_history_id) REFERENCES match_history(id),
        FOREIGN KEY (winner_user_id) REFERENCES user(id),
        FOREIGN KEY (user1_class) REFERENCES deck_class(class_name),
        FOREIGN KEY (user2_class) REFERENCES deck_class(class_name)
    )
    '''

    # シーズンテーブルの作成
    create_season_table = '''
    CREATE TABLE IF NOT EXISTS season (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        season_name TEXT UNIQUE,
        start_date TEXT,
        end_date TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    '''

    # ユーザーシーズン記録テーブルの作成
    create_user_season_record = '''
    CREATE TABLE IF NOT EXISTS user_season_record (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        season_id INTEGER,
        rating INTEGER,
        rank INTEGER,
        win_count INTEGER DEFAULT 0,
        loss_count INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES user(id),
        FOREIGN KEY (season_id) REFERENCES season(id)
    )
    '''

    # デッキクラステーブルの作成
    create_deck_class_table = '''
    CREATE TABLE IF NOT EXISTS deck_class (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        class_name TEXT UNIQUE,
        delete_flag BOOLEAN DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    '''

    # テーブルの作成
    cursor.execute(create_user_table)
    cursor.execute(create_match_history_table)
    cursor.execute(create_game_history_table)
    cursor.execute(create_season_table)
    cursor.execute(create_user_season_record)
    cursor.execute(create_deck_class_table)

    # デッキクラスのテーブルにデータを挿入
    insert_deck_class = '''
    INSERT OR IGNORE INTO deck_class (class_name, delete_flag, created_at)
    VALUES (?, ?, ?)
    '''

    # 各クラスをデータベースに挿入
    #for class_name in classes:
    #    created_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    #    cursor.execute(insert_deck_class, (class_name, False, created_at))

    # 既存のテーブルにカラムを追加（必要な場合）
    # ユーザーテーブルに `cancelled_matches_count` カラムがない場合は追加する
    alter_user_table_add_cancelled_matches_count = '''
    ALTER TABLE user ADD COLUMN cancelled_matches_count INTEGER DEFAULT 0
    '''
    # user_season_record テーブルに新しいカラムを追加
    alter_user_season_record_add_total_matches = '''
    ALTER TABLE user_season_record ADD COLUMN total_matches INTEGER DEFAULT 0
    '''

    alter_user_season_record_add_win_streak = '''
    ALTER TABLE user_season_record ADD COLUMN win_streak INTEGER DEFAULT 0
    '''

    alter_user_season_record_add_max_win_streak = '''
    ALTER TABLE user_season_record ADD COLUMN max_win_streak INTEGER DEFAULT 0
    '''

    # 変更を保存して接続を閉じる
    conn.commit()

except Exception as e:
    logging.error(f"データベースの処理中にエラーが発生しました: {e}")

finally:
    conn.close()
