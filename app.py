import os
import io
import time
import json
import hmac
import hashlib
import secrets
import sqlite3
import string
from datetime import datetime, timezone

import pandas as pd
import qrcode
import streamlit as st
from streamlit_autorefresh import st_autorefresh

APP_TITLE = "LAKS"
DB_PATH = os.getenv("QUIZ_DB_PATH", "quiz_app.db")
ADMIN_USER = os.getenv("QUIZ_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("QUIZ_ADMIN_PASSWORD", "admin@123")
APP_BASE_URL = os.getenv("QUIZ_BASE_URL", "http://192.168.0.2:8501").rstrip("/")

st.set_page_config(page_title=APP_TITLE, page_icon="🎯", layout="wide", initial_sidebar_state="collapsed")

def db():
    conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS questions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL DEFAULT 'General',
            question TEXT NOT NULL,
            options_json TEXT NOT NULL,
            correct_index INTEGER NOT NULL,
            explanation TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS rooms(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'lobby',
            question_ids_json TEXT NOT NULL,
            question_index INTEGER NOT NULL DEFAULT -1,
            question_started REAL,
            question_seconds INTEGER NOT NULL DEFAULT 20,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS players(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_code TEXT NOT NULL,
            nickname TEXT NOT NULL,
            token TEXT NOT NULL,
            score INTEGER NOT NULL DEFAULT 0,
            joined_at TEXT NOT NULL,
            UNIQUE(room_code, nickname),
            UNIQUE(token),
            FOREIGN KEY(room_code) REFERENCES rooms(code) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS answers(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_code TEXT NOT NULL,
            player_id INTEGER NOT NULL,
            question_id INTEGER NOT NULL,
            selected_index INTEGER NOT NULL,
            is_correct INTEGER NOT NULL,
            response_time REAL NOT NULL,
            answered_at TEXT NOT NULL,
            UNIQUE(room_code, player_id, question_id),
            FOREIGN KEY(player_id) REFERENCES players(id) ON DELETE CASCADE,
            FOREIGN KEY(question_id) REFERENCES questions(id)
        );
        """)
        count = c.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        if count == 0:
            samples = [
                ]
            c.executemany("INSERT INTO questions(category,question,options_json,correct_index,explanation,created_at) VALUES(?,?,?,?,?,?)",
                          [(cat,q,json.dumps(opts),ans,exp,utcnow()) for cat,q,opts,ans,exp in samples])

def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def admin_ok():
    return bool(st.session_state.get("admin_authenticated"))

def login_form():
    st.subheader("Admin login")
    with st.form("admin_login"):
        username = st.text_input("Username", value="")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", type="primary", use_container_width=True)
    if submitted:
        ok = hmac.compare_digest(username, ADMIN_USER) and hmac.compare_digest(password, ADMIN_PASSWORD)
        if ok:
            st.session_state.admin_authenticated = True
            st.rerun()
        else:
            st.error("Invalid username or password.")

def get_questions():
    with db() as c:
        return c.execute("SELECT * FROM questions ORDER BY id DESC").fetchall()

def get_room(code):
    with db() as c:
        return c.execute("SELECT * FROM rooms WHERE code=?", (code.upper().strip(),)).fetchone()

def make_code():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    for _ in range(30):
        code = "".join(secrets.choice(alphabet) for _ in range(6))
        if not get_room(code):
            return code
    raise RuntimeError("Could not generate a unique room code.")

def create_room(title, question_ids, seconds):
    code = make_code()
    with db() as c:
        c.execute("INSERT INTO rooms(code,title,status,question_ids_json,question_index,question_seconds,created_at) VALUES(?,?, 'lobby', ?, -1, ?, ?)",
                  (code, title.strip() or "Live Quiz", json.dumps(question_ids), int(seconds), utcnow()))
    return code

def get_room_questions(room):
    ids = json.loads(room["question_ids_json"])
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    with db() as c:
        rows = c.execute(f"SELECT * FROM questions WHERE id IN ({marks})", ids).fetchall()
    lookup = {r["id"]: r for r in rows}
    return [lookup[i] for i in ids if i in lookup]

def get_current_question(room):
    qs = get_room_questions(room)
    idx = room["question_index"]
    return qs[idx] if 0 <= idx < len(qs) else None

def leaderboard(code):
    with db() as c:
        return pd.read_sql_query("""
          SELECT p.nickname AS Player, p.score AS Score,
                 COUNT(a.id) AS Answered,
                 SUM(CASE WHEN a.is_correct=1 THEN 1 ELSE 0 END) AS Correct
          FROM players p LEFT JOIN answers a ON a.player_id=p.id AND a.room_code=p.room_code
          WHERE p.room_code=?
          GROUP BY p.id ORDER BY p.score DESC, Correct DESC, p.joined_at ASC
        """, c, params=(code,)).fillna(0)

def room_players(code):
    with db() as c:
        return c.execute("SELECT COUNT(*) FROM players WHERE room_code=?", (code,)).fetchone()[0]

def player_by_token(token):
    with db() as c:
        return c.execute("SELECT * FROM players WHERE token=?", (token,)).fetchone()

def join_room(code, nickname):
    code = code.upper().strip()
    room = get_room(code)
    if not room:
        return None, "Room code not found."
    if room["status"] in ("finished", "closed"):
        return None, "This quiz is no longer accepting players."
    nickname = " ".join(nickname.strip().split())[:24]
    if not nickname:
        return None, "Enter a display name."
    token = secrets.token_urlsafe(32)
    try:
        with db() as c:
            c.execute("INSERT INTO players(room_code,nickname,token,joined_at) VALUES(?,?,?,?)",
                      (code, nickname, token, utcnow()))
        return token, None
    except sqlite3.IntegrityError:
        return None, "That name is already used in this room. Choose another."

def start_question(code, index):
    room = get_room(code)
    if not room:
        return
    qs = get_room_questions(room)
    if index >= len(qs):
        with db() as c:
            c.execute("UPDATE rooms SET status='finished', question_started=NULL WHERE code=?", (code,))
        return
    with db() as c:
        c.execute("UPDATE rooms SET status='question', question_index=?, question_started=? WHERE code=?",
                  (index, time.time(), code))

def close_question(code):
    with db() as c:
        c.execute("UPDATE rooms SET status='reveal' WHERE code=?", (code,))

def set_room_status(code, status):
    with db() as c:
        c.execute("UPDATE rooms SET status=? WHERE code=?", (status, code))

def answer_question(code, token, qid, selected, started):
    """Validate and save one answer atomically; return a clear status message."""
    player = player_by_token(token)
    if not player or player["room_code"] != code:
        return False, "Player session not found. Please leave and rejoin the quiz."

    with db() as c:
        # Read the live room and question inside the same database transaction.
        room = c.execute("SELECT * FROM rooms WHERE code=?", (code,)).fetchone()
        if not room or room["status"] != "question":
            return False, "The host is not currently accepting answers."
        qids = get_room_questions(dict(room))
        index = int(room["question_index"] or 0)
        if index >= len(qids) or int(qids[index]["id"]) != int(qid):
            return False, "This question has changed. Your answer was not saved."

        elapsed = max(0.0, time.time() - float(room["question_started"] or started or time.time()))
        duration = max(1, int(room["question_seconds"]))
        if elapsed > duration + 2:
            return False, "Time is up. Wait for the next question."

        selected = int(selected)
        options = json.loads(qids[index]["options_json"])
        if selected < 0 or selected >= len(options):
            return False, "Please select one of the available options."

        already = c.execute(
            "SELECT 1 FROM answers WHERE room_code=? AND player_id=? AND question_id=?",
            (code, player["id"], int(qid))
        ).fetchone()
        if already:
            return False, "Your answer has already been recorded."

        correct = selected == int(qids[index]["correct_index"])
        points = max(100, int(1000 * (1 - elapsed / duration * 0.5))) if correct else 0
        try:
            c.execute(
                "INSERT INTO answers(room_code,player_id,question_id,selected_index,is_correct,response_time,answered_at) VALUES(?,?,?,?,?,?,?)",
                (code, player["id"], int(qid), selected, int(correct), elapsed, utcnow())
            )
            if correct:
                c.execute("UPDATE players SET score=score+? WHERE id=?", (points, player["id"]))
        except sqlite3.IntegrityError:
            return False, "Your answer has already been recorded."
    return True, ("Correct! +" + str(points) + " points") if correct else "Answer submitted successfully."

def question_answered(code, token, qid):
    player = player_by_token(token)
    if not player:
        return False
    with db() as c:
        return c.execute("SELECT 1 FROM answers WHERE room_code=? AND player_id=? AND question_id=?",
                         (code, player["id"], qid)).fetchone() is not None

def qr_png(url):
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

def public_url(code):
    base = APP_BASE_URL
    if not base:
        try:
            base = st.context.url.split("?")[0].rstrip("/")
        except Exception:
            base = ""
    return f"{base}/?room={code}" if base else f"?room={code}"

def question_bank():
    st.header("📚 Question bank")
    st.caption("Add questions individually or import many questions from an Excel workbook.")

    # Download a correctly formatted Excel template for bulk question import.
    template_columns = ["category", "question", "option_a", "option_b", "option_c", "option_d", "correct_answer", "explanation"]
    template_df = pd.DataFrame([
        {"category": "Cyber Security", "question": "What should you do with a suspicious link?",
         "option_a": "Click it", "option_b": "Forward it", "option_c": "Verify through an official channel", "option_d": "Share your PIN",
         "correct_answer": "C", "explanation": "Never click suspicious links; verify using a known official channel."}
    ], columns=template_columns)
    template_buffer = io.BytesIO()
    with pd.ExcelWriter(template_buffer, engine="openpyxl") as writer:
        template_df.to_excel(writer, index=False, sheet_name="Questions")
    st.download_button(
        "⬇️ Download Excel template", data=template_buffer.getvalue(),
        file_name="quiz_questions_template.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

    with st.expander("📥 Import questions from Excel", expanded=True):
        st.write("Upload an `.xlsx` or `.xls` file using the template. Correct answer must be A, B, C, or D (or the full matching option text).")
        uploaded_excel = st.file_uploader("Choose Excel file", type=["xlsx", "xls"], key="question_excel_upload")
        if uploaded_excel is not None:
            try:
                import_df = pd.read_excel(uploaded_excel, dtype=str).fillna("")
                import_df.columns = [str(col).strip().lower() for col in import_df.columns]
                required = ["question", "option_a", "option_b", "option_c", "option_d", "correct_answer"]
                missing = [col for col in required if col not in import_df.columns]
                if missing:
                    st.error("Missing required columns: " + ", ".join(missing) + ". Download and use the template above.")
                elif import_df.empty:
                    st.warning("The Excel sheet has no question rows.")
                else:
                    valid_rows, errors = [], []
                    for row_number, (_, row) in enumerate(import_df.iterrows(), start=2):
                        q = str(row.get("question", "")).strip()
                        options = [str(row.get(f"option_{letter}", "")).strip() for letter in "abcd"]
                        answer_raw = str(row.get("correct_answer", "")).strip()
                        answer_upper = answer_raw.upper()
                        if not q:
                            errors.append(f"Row {row_number}: question is blank.")
                            continue
                        if any(not option for option in options):
                            errors.append(f"Row {row_number}: all four options are required.")
                            continue
                        if answer_upper in ("A", "B", "C", "D"):
                            correct_index = "ABCD".index(answer_upper)
                        else:
                            matches = [i for i, option in enumerate(options) if option.casefold() == answer_raw.casefold()]
                            if len(matches) != 1:
                                errors.append(f"Row {row_number}: correct_answer must be A/B/C/D or exactly match one option.")
                                continue
                            correct_index = matches[0]
                        category = str(row.get("category", "General")).strip() or "General"
                        explanation = str(row.get("explanation", "")).strip()
                        valid_rows.append((category, q, json.dumps(options), correct_index, explanation, utcnow()))

                    if errors:
                        st.error("Please fix these Excel rows before importing:")
                        for error in errors[:20]:
                            st.write(f"- {error}")
                        if len(errors) > 20:
                            st.caption(f"And {len(errors) - 20} more validation errors.")
                    elif st.button(f"Import {len(valid_rows)} question(s)", type="primary", key="import_excel_questions"):
                        with db() as c:
                            c.executemany(
                                "INSERT INTO questions(category,question,options_json,correct_index,explanation,created_at) VALUES(?,?,?,?,?,?)",
                                valid_rows
                            )
                        st.success(f"Successfully imported {len(valid_rows)} question(s).")
                        st.rerun()
            except Exception as exc:
                st.error(f"Could not read this Excel file: {exc}")

    st.divider()
    st.subheader("Add one question manually")
    with st.form("add_question", clear_on_submit=True):
        category = st.text_input("Category", "General")
        question = st.text_area("Question")
        cols = st.columns(2)
        opts = [cols[0].text_input("Option A"), cols[1].text_input("Option B"),
                cols[0].text_input("Option C"), cols[1].text_input("Option D")]
        correct = st.selectbox("Correct answer", ["A", "B", "C", "D"])
        explanation = st.text_area("Explanation (optional)")
        add = st.form_submit_button("Add question", type="primary")
    if add:
        if not question.strip() or any(not x.strip() for x in opts):
            st.error("Enter a question and all four options.")
        else:
            with db() as c:
                c.execute("INSERT INTO questions(category,question,options_json,correct_index,explanation,created_at) VALUES(?,?,?,?,?,?)",
                          (category.strip() or "General", question.strip(), json.dumps(opts), "ABCD".index(correct), explanation.strip(), utcnow()))
            st.success("Question added.")
            st.rerun()
    rows = get_questions()
    if rows:
        st.subheader(f"Saved questions ({len(rows)})")
        df = pd.DataFrame([{"ID": r["id"], "Category": r["category"], "Question": r["question"],
                            "Correct option": "ABCD"[r["correct_index"]]} for r in rows])
        st.dataframe(df, use_container_width=True, hide_index=True)
        delete_id = st.number_input("Question ID to delete", min_value=0, step=1, value=0)
        if st.button("Delete selected question", disabled=delete_id == 0):
            with db() as c:
                used = c.execute("SELECT COUNT(*) FROM answers WHERE question_id=?", (int(delete_id),)).fetchone()[0]
                if used:
                    st.error("This question is used in a quiz history and cannot be deleted.")
                else:
                    c.execute("DELETE FROM questions WHERE id=?", (int(delete_id),))
                    st.success("Question deleted.")
                    st.rerun()

def host_page():
    st.header("🎤 HOST QUIZ")
    tab_create, tab_manage, tab_bank = st.tabs(["Create quiz", "Manage live quiz", "Question bank"])
    with tab_create:
        questions = get_questions()
        if not questions:
            st.warning("Add questions in the Question bank first.")
        else:
            with st.form("create_room"):
                title = st.text_input("Quiz title", "Cyber Security Awareness Quiz")
                categories = sorted({r["category"] for r in questions})
                selected_categories = st.multiselect("Filter categories (empty = all)", categories)
                available = [r for r in questions if not selected_categories or r["category"] in selected_categories]
                choices = {f'[{r["id"]}] {r["question"][:100]}': r["id"] for r in available}
                selected_labels = st.multiselect("Choose questions in quiz order", list(choices.keys()), default=list(choices.keys())[:min(5,len(choices))])
                seconds = st.slider("Seconds per question", 5, 120, 20, 5)
                submit = st.form_submit_button("Create room", type="primary")
            if submit:
                ids = [choices[x] for x in selected_labels]
                if not ids:
                    st.error("Select at least one question.")
                else:
                    code = create_room(title, ids, seconds)
                    st.session_state.host_room = code
                    st.success(f"Room created: {code}")
                    st.rerun()
    with tab_manage:
        code = st.text_input("Room code to manage", value=st.session_state.get("host_room", "")).upper().strip()
        room = get_room(code) if code else None
        if room:
            st.session_state.host_room = code
            st.subheader(f"{room['title']} · Room {code}")
            join_url = public_url(code)
            left, right = st.columns([1,2])
            with left:
                st.metric("Players", room_players(code))
                st.code(code, language=None)
                st.caption("Join URL")
                st.write(join_url)
                st.image(qr_png(join_url), width=190, caption="Scan to join")
            with right:
                st.write(f"Status: **{room['status'].title()}**")
                qs = get_room_questions(room)
                st.write(f"Question {max(0, room['question_index']+1)} of {len(qs)}")
                c1, c2, c3 = st.columns(3)
                if room["status"] in ("lobby", "reveal"):
                    next_index = room["question_index"] + 1
                    if c1.button("▶ Start / Next question", type="primary", disabled=next_index >= len(qs)):
                        start_question(code, next_index)
                        st.rerun()
                if room["status"] == "question":
                    if c2.button("⏹ Close answers"):
                        close_question(code)
                        st.rerun()
                if room["status"] == "finished":
                    st.success("Quiz finished.")
                if c3.button("🏁 End quiz"):
                    set_room_status(code, "finished")
                    st.rerun()
                if room["status"] == "question":
                    live_q = get_current_question(room)
                    if live_q:
                        st.divider()
                        st.subheader(f"Live question {room['question_index'] + 1}: {live_q['question']}")
                        live_options = json.loads(live_q["options_json"])
                        st.markdown("**Options shown to players:**")
                        for option_index, option_text in enumerate(live_options):
                            st.write(f"{chr(65 + option_index)}. {option_text}")
                    elapsed = max(0, time.time() - (room["question_started"] or time.time()))
                    remaining = max(0, int(room["question_seconds"] - elapsed))
                    st.progress(min(1.0, elapsed / max(1, room["question_seconds"])))
                    st.metric("Time remaining", f"{remaining}s")
                    if remaining <= 0:
                        close_question(code)
                        st.rerun()
            st.subheader("Live leaderboard")
            st.dataframe(leaderboard(code), use_container_width=True, hide_index=True)
            st.caption("The host page refreshes automatically while a question is live.")
        else:
            st.info("Create a room or enter an existing room code above.")
    with tab_bank:
        question_bank()

def join_page(prefill=""):
    st.header("👤 JOIN QUIZ")
    if "player_token" not in st.session_state:
        st.session_state.player_token = ""
    token = st.session_state.player_token
    player = player_by_token(token) if token else None
    if not player:
        with st.form("join_form"):
            code = st.text_input("Room code", value=prefill, max_chars=6).upper().strip()
            nickname = st.text_input("Your display name", max_chars=24)
            go = st.form_submit_button("Join quiz", type="primary", use_container_width=True)
        if go:
            new_token, error = join_room(code, nickname)
            if error:
                st.error(error)
            else:
                st.session_state.player_token = new_token
                st.session_state.player_room = code
                st.rerun()
        return

    code = player["room_code"]
    # Poll on every player-page render. Use the player's token in the key so the
    # polling component is tied to this browser's player session, not just the room.
    st_autorefresh(interval=1000, key=f"player_poll_{token}")
    room = get_room(code)  # Always read the latest room state from SQLite.
    if not room:
        st.error("Room not found. Please join again.")
        st.session_state.player_token = ""
        return
    st.subheader(f"Welcome, {player['nickname']}!")
    st.caption(f"Room {code} · {room['title']}")
    st.caption(f"Quiz status: {room['status'].title()}")
    player_should_poll = False
    if room["status"] == "lobby":
        player_should_poll = True
        st.info("You are in the lobby. The host will start the quiz shortly.")
        st.metric("Players in room", room_players(code))
    elif room["status"] == "question":
        q = get_current_question(room)
        if q:
            already = question_answered(code, token, q["id"])
            # Once an answer is recorded, polling can resume so the reveal appears
            # automatically. While the player is choosing, do not rerun the page.
            player_should_poll = already
            elapsed = max(0, time.time() - (room["question_started"] or time.time()))
            remaining = max(0, int(room["question_seconds"] - elapsed))
            st.progress(min(1.0, elapsed / max(1, room["question_seconds"])))
            st.write(f"Question {room['question_index']+1}:")
            st.subheader(q["question"])
            if already:
                st.success("Answer submitted successfully. Your answer is recorded.")
                st.caption("Waiting for the host to reveal the result…")
            elif remaining <= 0:
                st.warning("Time is up. Wait for the next question.")
                player_should_poll = True
            else:
                options = json.loads(q["options_json"])
                with st.form(f"answer_{code}_{q['id']}", clear_on_submit=False):
                    selected = st.radio(
                        "Choose one answer",
                        options=list(range(len(options))),
                        format_func=lambda i: options[i],
                        key=f"answer_choice_{code}_{q['id']}",
                        index=None
                    )
                    submit = st.form_submit_button(
                        "Submit answer",
                        type="primary",
                        use_container_width=True,
                    )
                if submit:
                    ok, message = answer_question(code, token, q["id"], selected, room["question_started"])
                    if ok:
                        st.session_state[f"answer_status_{code}_{q['id']}"] = ("success", message)
                    else:
                        st.session_state[f"answer_status_{code}_{q['id']}"] = ("error", message)
                    st.rerun()
                status = st.session_state.get(f"answer_status_{code}_{q['id']}")
                if status:
                    if status[0] == "success":
                        st.success(status[1])
                    else:
                        st.error(status[1])
            st.caption("Answers are saved on the server when you press Submit answer.")
    elif room["status"] == "reveal":
        player_should_poll = True
        q = get_current_question(room)
        if q:
            st.info("Answer revealed")
            st.subheader(q["question"])
            options = json.loads(q["options_json"])
            correct_idx = int(q["correct_index"])
            for i, option in enumerate(options):
                st.write(("✅ " if i == correct_idx else "▫️ ") + option)
            if q["explanation"]:
                st.caption(q["explanation"])
        st.subheader("Leaderboard")
        st.dataframe(leaderboard(code), use_container_width=True, hide_index=True)
        st.info("Wait for the host to start the next question.")
    elif room["status"] == "finished":
        player_should_poll = True
        st.success("Quiz complete!")
        st.subheader("Final leaderboard")
        scores = leaderboard(code)
        st.dataframe(scores, use_container_width=True, hide_index=True)
        mine = scores[scores["Player"] == player["nickname"]]
        if not mine.empty:
            st.metric("Your final score", int(mine.iloc[0]["Score"]))
        csv = scores.to_csv(index=False).encode("utf-8")
        st.download_button("Download leaderboard CSV", csv, file_name=f"leaderboard_{code}.csv", mime="text/csv")
    else:
        player_should_poll = True
        st.info("Waiting for the host.")
    # The auto-refresh at the top of this function keeps the player screen synced
    # with every host state change (including question_index changes).
    st.divider()
    if st.button("Leave / switch room", use_container_width=True):
        st.session_state.player_token = ""
        st.session_state.player_room = ""
        st.rerun()

def main():
    init_db()
    st.markdown("""
    <style>
    .stApp {background: linear-gradient(135deg, rgba(20,35,75,.05), rgba(20,120,150,.04));}
    div[data-testid="stMetric"] {border:1px solid rgba(128,128,128,.25);padding:14px;border-radius:14px;}
    </style>
    """, unsafe_allow_html=True)
    st.title("🎯 Welcome to Learning & Knowledge Surfing (LAKS)")
    st.caption("Host a live quiz, invite players with a room code or QR code, and follow scores in real time.")
    # Auto-refresh only the host screen. Refreshing participant pages in the
    # background can interrupt answer selection/submission on some clients.
    if st.session_state.get("host_room") and not st.session_state.get("player_token"):
        st_autorefresh(interval=2000, key="host_live_quiz_poll")
    query_room = ""
    try:
        query_room = st.query_params.get("room", "")
    except Exception:
        pass
    page = st.radio("Choose a mode", ["🏠 Home", "🎤 HOST QUIZ", "👤 JOIN QUIZ", "🔐 Admin"], horizontal=True, label_visibility="collapsed")
    if page == "🏠 Home":
        a, b = st.columns(2)
        with a:
            st.markdown("### 🎤 HOST QUIZ")
            st.write("Create a room, select questions, set a timer and control the game.")
            if st.button("🎤 HOST QUIZ", type="primary", use_container_width=True):
                st.session_state.mode = "host"
                st.rerun()
        with b:
            st.markdown("### 👤 JOIN QUIZ")
            st.write("Enter a room code or scan the host's QR code.")
            if st.button("👤 JOIN QUIZ", use_container_width=True):
                st.session_state.mode = "join"
                st.rerun()
        st.info("Use the mode selector above to switch pages. Admin access manages the question bank.")
        if st.session_state.get("mode") == "host":
            if admin_ok():
                host_page()
            else:
                st.warning("Sign in as admin to host a quiz.")
                login_form()
        elif st.session_state.get("mode") == "join":
            join_page(query_room)
    elif page == "🎤 HOST QUIZ":
        if admin_ok():
            host_page()
        else:
            st.warning("Admin login is required to host a quiz.")
            login_form()
    elif page == "👤 JOIN QUIZ":
        join_page(query_room)
    else:
        if not admin_ok():
            login_form()
        else:
            st.success(f"Signed in as {ADMIN_USER}")
            if st.button("Sign out"):
                st.session_state.admin_authenticated = False
                st.rerun()
            st.write("Admin tools")
            question_bank()
            st.subheader("Recent rooms")
            with db() as c:
                rooms = c.execute("SELECT code,title,status,created_at FROM rooms ORDER BY id DESC LIMIT 50").fetchall()
            if rooms:
                st.dataframe(pd.DataFrame([dict(r) for r in rooms]), use_container_width=True, hide_index=True)

if __name__ == "__main__":
    main()
