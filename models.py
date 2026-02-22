"""
ドメインモデル & SQLAlchemy モデル
"""
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


# ---------------------------------------------------------------------------
# SQLAlchemy ORM モデル
# ---------------------------------------------------------------------------

class Word(db.Model):
    """words テーブル"""
    __tablename__ = "words"

    id             = db.Column(db.Integer, primary_key=True, autoincrement=True)
    word           = db.Column(db.String(100), nullable=False)
    meaning        = db.Column(db.Text, nullable=False)
    part_of_speech = db.Column(db.String(50), nullable=False)
    example        = db.Column(db.Text, nullable=False)
    example_ja     = db.Column(db.Text, nullable=False)
    transitivity   = db.Column(db.String(50), nullable=True)
    countability   = db.Column(db.String(50), nullable=True)
    book           = db.Column(db.String(20), nullable=False, default="reading")
    created_at     = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    study_record = db.relationship(
        "StudyRecord",
        back_populates="word",
        uselist=False,
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Word id={self.id} word={self.word!r}>"


class StudyRecord(db.Model):
    """study_records テーブル"""
    __tablename__ = "study_records"

    id              = db.Column(db.Integer, primary_key=True, autoincrement=True)
    word_id         = db.Column(db.Integer, db.ForeignKey("words.id"), unique=True, nullable=False)
    ease_factor     = db.Column(db.Float, nullable=False, default=2.5)
    interval_days   = db.Column(db.Integer, nullable=False, default=0)
    repetitions     = db.Column(db.Integer, nullable=False, default=0)
    due_date        = db.Column(db.Date, nullable=False, default=date.today)
    last_reviewed_at = db.Column(db.DateTime, nullable=True)

    word = db.relationship("Word", back_populates="study_record")

    def __repr__(self) -> str:
        return f"<StudyRecord word_id={self.word_id} due={self.due_date}>"


# ---------------------------------------------------------------------------
# ドメイン転送オブジェクト（Gemini APIレスポンスから生成）
# ---------------------------------------------------------------------------

@dataclass
class WordInfo:
    """Gemini API から取得した単語情報を受け渡すための値オブジェクト"""
    word:           str
    meaning:        str
    part_of_speech: str
    example:        str
    example_ja:     str
    transitivity:   Optional[str] = None
    countability:   Optional[str] = None
    book:           str = "reading"
