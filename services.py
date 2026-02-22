"""
サービス層
  - WordService  : 単語検索・辞書管理のユースケース
  - StudyService : 学習セッション管理・SRS スケジューリング
"""
from datetime import date, datetime, timedelta
from typing import Literal

from models import Word, WordInfo
from infrastructure import GeminiGateway, DatabaseStorage, CsvExporter


# ---------------------------------------------------------------------------
# WordService
# ---------------------------------------------------------------------------

class WordService:
    """単語検索・辞書管理のユースケースを実装するサービス"""

    def __init__(
        self,
        gateway: GeminiGateway,
        exporter: CsvExporter,
        storage: DatabaseStorage,
    ) -> None:
        self._gateway = gateway
        self._exporter = exporter
        self._storage = storage

    def search_word(self, word: str) -> list[WordInfo]:
        """Gemini API で単語を検索し、WordInfo リストに変換して返す"""
        raw_list = self._gateway.get_word_info_json(word)
        results: list[WordInfo] = []
        for item in raw_list:
            try:
                results.append(
                    WordInfo(
                        word=item.get("word", word),
                        meaning=item.get("meaning", ""),
                        part_of_speech=item.get("part_of_speech", ""),
                        example=item.get("example", ""),
                        example_ja=item.get("example_ja", ""),
                        transitivity=item.get("transitivity"),
                        countability=item.get("countability"),
                    )
                )
            except (KeyError, TypeError):
                continue
        return results

    def get_dictionary(self, sort: str = "word", order: str = "asc", book: str = None, pos: str = None) -> list[Word]:
        """DB から単語帳を取得し、ソートして返す"""
        words = self._storage.get_all_words(book=book, pos=pos)
        reverse = order == "desc"
        key_func = {
            "word": lambda w: w.word.lower(),
        }.get(sort, lambda w: w.word.lower())
        return sorted(words, key=key_func, reverse=reverse)

    def get_parts_of_speech(self, book: str = None) -> list[str]:
        """単語帳に存在する品詞一覧を返す"""
        return self._storage.get_parts_of_speech(book=book)

    AddResult = Literal["added", "duplicate", "write_failed"]

    def add_to_dictionary(self, word_info: WordInfo, force_add: bool = False) -> "WordService.AddResult":
        """重複チェック後に DB へ追加する。StudyRecord も同時生成される。"""
        if not force_add:
            existing = self._storage.find_word(word_info.word, book=word_info.book)
            if existing:
                return "duplicate"
        try:
            self._storage.add_word(word_info)
            return "added"
        except Exception:
            return "write_failed"

    def clear_dictionary(self, book: str = None) -> None:
        """words および study_records を全件削除または指定帳のみ削除する"""
        self._storage.delete_all_words(book=book)

    def export_dictionary(self, book: str = None) -> str:
        """辞書データを CSV 文字列に変換して返す"""
        words = self._storage.get_all_words(book=book)
        return self._exporter.export(words)

    def count_dictionary(self, book: str = None) -> int:
        return self._storage.count_words(book=book)

    def export_selected(self, ids: list[int]) -> str:
        """指定IDの単語を CSV 文字列に変換して返す"""
        words = self._storage.get_words_by_ids(ids)
        return self._exporter.export(words)

    def delete_selected(self, ids: list[int]) -> None:
        """指定IDの単語を削除する"""
        self._storage.delete_words_by_ids(ids)

    def update_word(self, word_id: int, word_info: WordInfo) -> None:
        """既存単語を更新する"""
        self._storage.update_word(word_id, word_info)

    def get_word_by_id(self, word_id: int) -> "Word | None":
        """IDで単語を取得する"""
        return self._storage.get_word_by_id(word_id)


# ---------------------------------------------------------------------------
# StudyService
# ---------------------------------------------------------------------------

class StudyService:
    """学習セッション管理と SM-2 ベースの SRS スケジューリング"""

    def __init__(self, storage: DatabaseStorage) -> None:
        self._storage = storage

    def get_due_cards(self, today: date, book: str = None) -> list[Word]:
        return self._storage.get_due_words(today, book=book)

    def get_due_count(self, today: date, book: str = None) -> int:
        return len(self._storage.get_due_words(today, book=book))

    def build_session_queue(self, cards: list[Word]) -> list[int]:
        """単語 ID のキューを返す"""
        return [card.id for card in cards]

    def evaluate(self, word_id: int, rating: int) -> None:
        """
        SM-2 アルゴリズムで StudyRecord を更新する。
        rating: 0=もう一度, 1=難しい, 2=正解, 3=簡単
        """
        record = self._storage.get_study_record(word_id)
        if record is None:
            return

        ef   = record.ease_factor
        intv = record.interval_days
        reps = record.repetitions
        today = date.today()

        if rating == 0:            # もう一度
            reps = 0
            intv = 0

        elif rating == 1:          # 難しい
            reps = max(0, reps - 1)
            intv = max(1, round(intv * 0.8))
            ef   = max(1.3, ef - 0.15)

        elif rating == 2:          # 正解
            if reps == 0:
                intv = 1
            elif reps == 1:
                intv = 6
            else:
                intv = round(intv * ef)
            reps += 1

        elif rating == 3:          # 簡単
            if reps == 0:
                intv = 4
            elif reps == 1:
                intv = 10
            else:
                intv = round(intv * ef * 1.3)
            reps += 1
            ef = min(4.0, ef + 0.1)

        record.ease_factor     = ef
        record.interval_days   = intv
        record.repetitions     = reps
        record.due_date        = today + timedelta(days=intv)
        record.last_reviewed_at = datetime.utcnow()

        self._storage.update_study_record(record)

    def get_next_due_date(self, today: date):
        return self._storage.get_next_due_date(today)
