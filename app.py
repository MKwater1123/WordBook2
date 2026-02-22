"""
Flask アプリ エントリポイント
"""
import os
import sys
from datetime import date

import io
from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
    make_response,
    send_file,
)
from flask_migrate import Migrate
from dotenv import load_dotenv

load_dotenv()

from gtts import gTTS
from models import db, WordInfo
from infrastructure import GeminiGateway, DatabaseStorage, CsvExporter, ApiException, StorageException
from services import WordService, StudyService

# ---------------------------------------------------------------------------
# Flask アプリとDB初期化
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", "sqlite:///vocabulary.db"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)
migrate = Migrate(app, db)

# ---------------------------------------------------------------------------
# サービス・ゲートウェイの初期化
# ---------------------------------------------------------------------------
try:
    gateway = GeminiGateway()
except ValueError as e:
    print(f"[ERROR] {e}", file=sys.stderr)
    sys.exit(1)

storage  = DatabaseStorage()
exporter = CsvExporter()
word_service  = WordService(gateway, exporter, storage)
study_service = StudyService(storage)


# ---------------------------------------------------------------------------
# Context Processor: 全テンプレートに辞書件数を注入
# ---------------------------------------------------------------------------
@app.context_processor
def inject_dictionary_count():
    try:
        count = word_service.count_dictionary()
        reading_count = word_service.count_dictionary(book="reading")
        listening_count = word_service.count_dictionary(book="listening")
    except Exception:
        count = reading_count = listening_count = 0
    return {
        "dictionary_count": count,
        "reading_count": reading_count,
        "listening_count": listening_count,
    }


# ===========================================================================
# 音声読み上げ (TTS)
# ===========================================================================

@app.route("/tts")
def tts():
    """指定テキストを gTTS で MP3 に変換して返す"""
    text = request.args.get("text", "").strip()
    lang = request.args.get("lang", "en")
    if not text:
        return "No text provided", 400
    buf = io.BytesIO()
    gTTS(text=text, lang=lang, slow=False).write_to_fp(buf)
    buf.seek(0)
    return send_file(buf, mimetype="audio/mpeg")


# ===========================================================================
# 検索機能 (F-001, F-002, F-003)
# ===========================================================================

@app.route("/")
def index():
    search_word   = session.get("search_word", "")
    search_results = session.get("search_results", [])
    pending_add   = session.get("pending_add")
    return render_template(
        "index.html",
        search_word=search_word,
        search_results=search_results,
        pending_add=pending_add,
    )


@app.route("/search", methods=["POST"])
def search():
    word = request.form.get("word", "").strip()
    if not word:
        flash("検索する単語を入力してください。", "error")
        return redirect(url_for("index"))

    try:
        results = word_service.search_word(word)
        # WordInfo を dict に変換してセッションに保存
        session["search_word"] = word
        session["search_results"] = [
            {
                "word":           r.word,
                "meaning":        r.meaning,
                "part_of_speech": r.part_of_speech,
                "example":        r.example,
                "example_ja":     r.example_ja,
                "transitivity":   r.transitivity,
                "countability":   r.countability,
            }
            for r in results
        ]
        session.pop("pending_add", None)
        if not results:
            flash(f"「{word}」の検索結果が見つかりませんでした。", "error")
    except ApiException as e:
        flash(f"API エラー: {e}", "error")
        session.pop("search_results", None)

    return redirect(url_for("index"))


@app.route("/add", methods=["POST"])
def add():
    try:
        book = request.form.get("book", "reading")
        if book not in ("reading", "listening"):
            book = "reading"
        word_info = WordInfo(
            word=request.form["word"],
            meaning=request.form["meaning"],
            part_of_speech=request.form["part_of_speech"],
            example=request.form["example"],
            example_ja=request.form["example_ja"],
            transitivity=request.form.get("transitivity") or None,
            countability=request.form.get("countability") or None,
            book=book,
        )
    except KeyError as e:
        flash(f"フォームデータが不足しています: {e}", "error")
        return redirect(url_for("index"))

    force_add = request.form.get("force_add") == "1"
    result = word_service.add_to_dictionary(word_info, force_add=force_add)

    book_label = "Reading" if word_info.book == "reading" else "Listening"
    if result == "added":
        session.pop("pending_add", None)
        flash(f"「{word_info.word}」を {book_label} 単語帳に追加しました。", "success")
    elif result == "duplicate":
        # 重複確認のためフォームデータをセッションに保存
        session["pending_add"] = {
            "word":           word_info.word,
            "meaning":        word_info.meaning,
            "part_of_speech": word_info.part_of_speech,
            "example":        word_info.example,
            "example_ja":     word_info.example_ja,
            "transitivity":   word_info.transitivity,
            "countability":   word_info.countability,
            "book":           word_info.book,
        }
    else:
        flash("単語の追加に失敗しました。", "error")

    return redirect(url_for("index"))


@app.route("/add/cancel", methods=["POST"])
def add_cancel():
    session.pop("pending_add", None)
    return redirect(url_for("index"))


# ===========================================================================
# 単語帳機能 (F-004, F-005, F-006)
# ===========================================================================

@app.route("/dictionary")
def dictionary():
    sort  = request.args.get("sort", "word")
    order = request.args.get("order", "asc")
    book  = request.args.get("book", "reading")
    pos   = request.args.get("pos", "")
    if book not in ("reading", "listening"):
        book = "reading"
    try:
        pos_list = word_service.get_parts_of_speech(book=book)
        words = word_service.get_dictionary(sort=sort, order=order, book=book, pos=pos or None)
    except StorageException as e:
        flash(str(e), "error")
        words = []
        pos_list = []
    return render_template("dictionary.html", words=words, sort=sort, order=order, book=book, pos=pos, pos_list=pos_list)


@app.route("/export")
def export():
    book = request.args.get("book")
    try:
        csv_str = word_service.export_dictionary(book=book)
    except StorageException as e:
        flash(str(e), "error")
        return redirect(url_for("dictionary"))

    filename = f"my_dictionary_{book}.csv" if book else "my_dictionary.csv"
    response = make_response(csv_str.encode("utf-8"))
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return response


@app.route("/clear", methods=["POST"])
def clear():
    book = request.form.get("book")
    if book not in ("reading", "listening"):
        book = None
    try:
        word_service.clear_dictionary(book=book)
        label = "Reading" if book == "reading" else "Listening" if book == "listening" else ""
        flash(f"{label}単語帳をクリアしました。", "success")
    except StorageException as e:
        flash(str(e), "error")
    dest_book = book if book else "reading"
    return redirect(url_for("dictionary", book=dest_book))


@app.route("/export/selected", methods=["POST"])
def export_selected():
    ids_raw = request.form.getlist("ids[]")
    try:
        ids = [int(i) for i in ids_raw if i.strip().isdigit()]
    except ValueError:
        flash("無効なIDが含まれています。", "error")
        return redirect(url_for("dictionary"))
    book = request.form.get("book", "reading")
    if not ids:
        flash("エクスポートする単語を選択してください。", "error")
        return redirect(url_for("dictionary", book=book))
    try:
        csv_str = word_service.export_selected(ids)
    except StorageException as e:
        flash(str(e), "error")
        return redirect(url_for("dictionary", book=book))
    filename = f"selected_{book}.csv"
    response = make_response(csv_str.encode("utf-8"))
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return response


@app.route("/delete/selected", methods=["POST"])
def delete_selected():
    ids_raw = request.form.getlist("ids[]")
    try:
        ids = [int(i) for i in ids_raw if i.strip().isdigit()]
    except ValueError:
        flash("無効なIDが含まれています。", "error")
        return redirect(url_for("dictionary"))
    book = request.form.get("book", "reading")
    if not ids:
        flash("削除する単語を選択してください。", "error")
        return redirect(url_for("dictionary", book=book))
    try:
        word_service.delete_selected(ids)
        flash(f"{len(ids)} 件の単語を削除しました。", "success")
    except StorageException as e:
        flash(str(e), "error")
    return redirect(url_for("dictionary", book=book))


@app.route("/dictionary/word/add", methods=["POST"])
def dictionary_add():
    """単語帳に手動で新しい単語を追加する"""
    book = request.form.get("book", "reading")
    if book not in ("reading", "listening"):
        book = "reading"
    word_str = request.form.get("word", "").strip()
    if not word_str:
        flash("単語を入力してください。", "error")
        return redirect(url_for("dictionary", book=book))
    try:
        word_info = WordInfo(
            word=word_str,
            meaning=request.form.get("meaning", "").strip(),
            part_of_speech=request.form.get("part_of_speech", "").strip(),
            example=request.form.get("example", "").strip(),
            example_ja=request.form.get("example_ja", "").strip(),
            transitivity=request.form.get("transitivity", "").strip() or None,
            countability=request.form.get("countability", "").strip() or None,
            book=book,
        )
    except Exception as e:
        flash(f"入力内容が不正です: {e}", "error")
        return redirect(url_for("dictionary", book=book))

    result = word_service.add_to_dictionary(word_info, force_add=True)
    book_label = "Reading" if book == "reading" else "Listening"
    if result == "added":
        flash(f"「{word_info.word}」を {book_label} 単語帳に追加しました。", "success")
    else:
        flash("単語の追加に失敗しました。", "error")
    return redirect(url_for("dictionary", book=book))


@app.route("/dictionary/word/<int:word_id>/edit", methods=["POST"])
def dictionary_edit(word_id: int):
    """単語帳の既存エントリを編集する"""
    book = request.form.get("book", "reading")
    if book not in ("reading", "listening"):
        book = "reading"
    word_str = request.form.get("word", "").strip()
    if not word_str:
        flash("単語を入力してください。", "error")
        return redirect(url_for("dictionary", book=book))
    try:
        word_info = WordInfo(
            word=word_str,
            meaning=request.form.get("meaning", "").strip(),
            part_of_speech=request.form.get("part_of_speech", "").strip(),
            example=request.form.get("example", "").strip(),
            example_ja=request.form.get("example_ja", "").strip(),
            transitivity=request.form.get("transitivity", "").strip() or None,
            countability=request.form.get("countability", "").strip() or None,
            book=book,
        )
    except Exception as e:
        flash(f"入力内容が不正です: {e}", "error")
        return redirect(url_for("dictionary", book=book))
    try:
        word_service.update_word(word_id, word_info)
        flash(f"「{word_info.word}」を更新しました。", "success")
    except StorageException as e:
        flash(str(e), "error")
    return redirect(url_for("dictionary", book=book))


# ===========================================================================
# 学習機能 (F-011, F-012, F-013)
# ===========================================================================

@app.route("/study")
def study_top():
    today = date.today()
    reading_due   = study_service.get_due_count(today, book="reading")
    listening_due = study_service.get_due_count(today, book="listening")
    reading_total   = word_service.count_dictionary(book="reading")
    listening_total = word_service.count_dictionary(book="listening")
    return render_template(
        "study_top.html",
        reading_due=reading_due,
        listening_due=listening_due,
        reading_total=reading_total,
        listening_total=listening_total,
        today=today,
    )


@app.route("/study/start", methods=["POST"])
def study_start():
    today = date.today()
    book  = request.form.get("book", "reading")
    if book not in ("reading", "listening"):
        book = "reading"
    cards = study_service.get_due_cards(today, book=book)
    if not cards:
        flash("本日学習すべき単語はありません。", "info")
        return redirect(url_for("study_top"))

    queue = study_service.build_session_queue(cards)
    session["study_queue"]   = queue
    session["study_done"]    = []
    session["study_ratings"] = {}
    session["study_total"]   = len(queue)
    session["study_book"]    = book
    return redirect(url_for("study_card"))


@app.route("/study/card")
def study_card():
    queue = session.get("study_queue", [])
    if not queue:
        return redirect(url_for("study_result"))

    word_id = queue[0]
    word    = storage.get_word_by_id(word_id)
    if word is None:
        # 削除済みの単語はスキップ
        session["study_queue"] = queue[1:]
        return redirect(url_for("study_card"))

    done_count  = len(session.get("study_done", []))
    total_count = session.get("study_total", len(queue))
    current_num = done_count + 1

    return render_template(
        "study_session.html",
        word=word,
        step=1,
        current_num=current_num,
        total_count=total_count,
    )


@app.route("/study/answer", methods=["POST"])
def study_answer():
    queue = session.get("study_queue", [])
    if not queue:
        return redirect(url_for("study_result"))

    word_id = queue[0]
    word    = storage.get_word_by_id(word_id)

    done_count  = len(session.get("study_done", []))
    total_count = session.get("study_total", len(queue))
    current_num = done_count + 1

    return render_template(
        "study_session.html",
        word=word,
        step=2,
        current_num=current_num,
        total_count=total_count,
    )


@app.route("/study/evaluate", methods=["POST"])
def study_evaluate():
    try:
        word_id = int(request.form["word_id"])
        rating  = int(request.form["rating"])
    except (KeyError, ValueError):
        return "Bad Request", 400

    if rating not in (0, 1, 2, 3):
        return "Bad Request: rating must be 0-3", 400

    queue   = session.get("study_queue", [])
    done    = session.get("study_done", [])
    ratings = session.get("study_ratings", {})

    # キューから先頭を除去
    if queue and queue[0] == word_id:
        queue = queue[1:]

    if rating == 0:
        # もう一度: キュー末尾に再追加
        queue.append(word_id)
    else:
        # 完了扱い（done に追加）
        study_service.evaluate(word_id, rating)
        done.append(word_id)
        ratings[str(word_id)] = rating

    session["study_queue"]   = queue
    session["study_done"]    = done
    session["study_ratings"] = ratings

    if not queue:
        return redirect(url_for("study_result"))
    return redirect(url_for("study_card"))


@app.route("/study/result")
def study_result():
    today   = date.today()
    done    = session.get("study_done", [])
    ratings = session.get("study_ratings", {})
    total   = session.get("study_total", 0)

    # 評価別カウント
    counts = {0: 0, 1: 0, 2: 0, 3: 0}
    for v in ratings.values():
        counts[v] = counts.get(v, 0) + 1

    next_due = study_service.get_next_due_date(today)

    # セッションクリア
    session.pop("study_queue", None)
    session.pop("study_done", None)
    session.pop("study_ratings", None)
    session.pop("study_total", None)

    return render_template(
        "study_result.html",
        studied_count=len(done),
        total_count=total,
        counts=counts,
        next_due=next_due,
    )


# ---------------------------------------------------------------------------
# 起動
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)
