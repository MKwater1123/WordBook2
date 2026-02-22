"""
インフラ層
  - GeminiGateway   : Google Gemini API との通信
  - DatabaseStorage : SQLAlchemy 経由の DB 操作
  - CsvExporter     : CSV 文字列生成
"""
import csv
import io
import json
import os
from datetime import date
from typing import Optional

from google import genai
from google.genai import types as genai_types

from models import db, Word, StudyRecord, WordInfo


# ---------------------------------------------------------------------------
# カスタム例外
# ---------------------------------------------------------------------------

class ApiException(Exception):
    """Gemini API への通信エラー"""


class StorageException(Exception):
    """DB 操作エラー"""


# ---------------------------------------------------------------------------
# GeminiGateway
# ---------------------------------------------------------------------------

_PROMPT_TEMPLATE = """
You are a professional English dictionary assistant.
Search the following English word and return detailed information for each part of speech as a JSON array.

word: "{word}"

Return ONLY a valid JSON array (no markdown, no explanation).
Each element must have these keys:
  - "word"           : string  (the English word)
  - "meaning"        : string  (Japanese meaning)
  - "part_of_speech" : string  (e.g. "名詞", "動詞", "形容詞", "副詞", "前置詞", "接続詞")
  - "example"        : string  (English example sentence)
  - "example_ja"     : string  (Japanese translation of the example sentence)
  - "transitivity"   : string or null  (only for verbs: "他動詞", "自動詞", "他動詞・自動詞"; null otherwise)
  - "countability"   : string or null  (only for nouns: "可算", "不可算", "可算・不可算"; null otherwise)

Example output (do NOT copy this):
[
  {{
    "word": "run",
    "meaning": "走る",
    "part_of_speech": "動詞",
    "example": "She runs every morning.",
    "example_ja": "彼女は毎朝走る。",
    "transitivity": "自動詞",
    "countability": null
  }}
]
"""


class GeminiGateway:
    """Google Gemini API ゲートウェイ"""

    def __init__(self) -> None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise ValueError(
                "環境変数 GEMINI_API_KEY が設定されていません。"
                ".env ファイルに GEMINI_API_KEY=your_key を追加してください。"
            )
        self._client = genai.Client(api_key=api_key)
        self._model_name = "gemini-2.5-flash"

    def get_word_info_json(self, word: str) -> list[dict]:
        """
        Gemini API に単語情報を問い合わせ、辞書のリストを返す。
        失敗時は ApiException を送出する。
        """
        try:
            prompt = _PROMPT_TEMPLATE.format(word=word)
            response = self._client.models.generate_content(
                model=self._model_name,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    response_mime_type="application/json",
                ),
            )
            raw = response.text.strip()
            # JSON 配列として解析
            data = json.loads(raw)
            if not isinstance(data, list):
                raise ApiException("Gemini API のレスポンスが配列ではありません。")
            return data
        except json.JSONDecodeError as e:
            raise ApiException(f"Gemini API のレスポンスを JSON として解析できませんでした: {e}") from e
        except Exception as e:
            if isinstance(e, ApiException):
                raise
            raise ApiException(f"Gemini API との通信中にエラーが発生しました: {e}") from e


# ---------------------------------------------------------------------------
# DatabaseStorage
# ---------------------------------------------------------------------------

class DatabaseStorage:
    """SQLAlchemy を使った DB 操作クラス"""

    # ---- 単語 ----

    def get_all_words(self) -> list[Word]:
        try:
            return Word.query.all()
        except Exception as e:
            raise StorageException(f"全単語の取得に失敗しました: {e}") from e

    def get_word_by_id(self, word_id: int) -> Optional[Word]:
        try:
            return Word.query.get(word_id)
        except Exception as e:
            raise StorageException(f"単語(id={word_id})の取得に失敗しました: {e}") from e

    def find_word(self, word_str: str) -> list[Word]:
        """大文字小文字を無視して単語を検索する"""
        try:
            return Word.query.filter(
                db.func.lower(Word.word) == word_str.lower()
            ).all()
        except Exception as e:
            raise StorageException(f"単語検索に失敗しました: {e}") from e

    def add_word(self, word_info: WordInfo) -> Word:
        """単語を DB に追加し、StudyRecord の初期レコードも同時生成する"""
        try:
            word = Word(
                word=word_info.word,
                meaning=word_info.meaning,
                part_of_speech=word_info.part_of_speech,
                example=word_info.example,
                example_ja=word_info.example_ja,
                transitivity=word_info.transitivity,
                countability=word_info.countability,
            )
            db.session.add(word)
            db.session.flush()  # word.id を確定させる

            record = StudyRecord(
                word_id=word.id,
                ease_factor=2.5,
                interval_days=0,
                repetitions=0,
                due_date=date.today(),
            )
            db.session.add(record)
            db.session.commit()
            return word
        except Exception as e:
            db.session.rollback()
            raise StorageException(f"単語の追加に失敗しました: {e}") from e

    def delete_all_words(self) -> None:
        """全単語・学習レコードを削除する"""
        try:
            StudyRecord.query.delete()
            Word.query.delete()
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise StorageException(f"単語帳のクリアに失敗しました: {e}") from e

    def count_words(self) -> int:
        try:
            return Word.query.count()
        except Exception as e:
            raise StorageException(f"件数取得に失敗しました: {e}") from e

    # ---- 学習レコード ----

    def get_study_record(self, word_id: int) -> Optional[StudyRecord]:
        try:
            return StudyRecord.query.filter_by(word_id=word_id).first()
        except Exception as e:
            raise StorageException(f"学習レコードの取得に失敗しました: {e}") from e

    def update_study_record(self, record: StudyRecord) -> None:
        try:
            db.session.add(record)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise StorageException(f"学習レコードの更新に失敗しました: {e}") from e

    def get_due_words(self, today: date) -> list[Word]:
        """due_date が today 以前の単語を返す"""
        try:
            records = (
                StudyRecord.query
                .filter(StudyRecord.due_date <= today)
                .all()
            )
            return [r.word for r in records if r.word is not None]
        except Exception as e:
            raise StorageException(f"学習対象単語の取得に失敗しました: {e}") from e

    def get_next_due_date(self, today: date) -> Optional[date]:
        """today より後で最も早い due_date を返す"""
        try:
            record = (
                StudyRecord.query
                .filter(StudyRecord.due_date > today)
                .order_by(StudyRecord.due_date.asc())
                .first()
            )
            return record.due_date if record else None
        except Exception as e:
            raise StorageException(f"次回学習日の取得に失敗しました: {e}") from e


# ---------------------------------------------------------------------------
# CsvExporter
# ---------------------------------------------------------------------------

class CsvExporter:
    """単語帳を CSV 文字列に変換するクラス"""

    COLUMNS = [
        ("Word",            "word"),
        ("Meaning",         "meaning"),
        ("Part of Speech",  "part_of_speech"),
        ("Example (EN)",    "example"),
        ("Example (JA)",    "example_ja"),
        ("Transitivity",    "transitivity"),
        ("Countability",    "countability"),
    ]

    def export(self, words: list[Word]) -> str:
        """UTF-8 BOM 付きの CSV 文字列を返す"""
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([col for col, _ in self.COLUMNS])
        for word in words:
            writer.writerow([
                getattr(word, attr) or ""
                for _, attr in self.COLUMNS
            ])
        return "\ufeff" + output.getvalue()  # BOM 付き
