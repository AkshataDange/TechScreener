"""
database.py
PostgreSQL persistence layer.
"""

import json
import re
import uuid
from datetime import datetime, timezone, timedelta

import psycopg2
from config import settings

DB_URL = settings.database_url


def get_conn():
    match = re.match(r"postgresql://(.*?):(.*?)@([^:]+):(\d+)/(.*)", DB_URL)
    if match:
        user, password, host, port, dbname = match.groups()
        return psycopg2.connect(host=host, port=port, user=user, password=password, dbname=dbname)
    return psycopg2.connect(DB_URL)


def init_db():
    """Create all tables and apply migrations. Called once on app startup."""
    conn = get_conn()
    cur = conn.cursor()

    # ── Legacy column rename (kept from original) ─────────────────────────────
    cur.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'sessions'
                  AND column_name = 'question_time_limit_minutes'
            ) AND NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'sessions'
                  AND column_name = 'interview_time_limit_minutes'
            ) THEN
                ALTER TABLE sessions
                RENAME COLUMN question_time_limit_minutes TO interview_time_limit_minutes;
            END IF;
        END $$;
    """)

    # ── Core tables ───────────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id                           TEXT PRIMARY KEY,
            topic                        TEXT,
            num_questions                INTEGER,
            interview_time_limit_minutes INTEGER NOT NULL DEFAULT 10,
            questions                    JSONB NOT NULL DEFAULT '[]',
            created_at                   TIMESTAMPTZ NOT NULL,
            user_id                      TEXT,
            assignment_id                TEXT,
            status                       TEXT NOT NULL DEFAULT 'in_progress',
            completed_at                 TIMESTAMPTZ,
            -- Adaptive interview additions
            plan                         JSONB,
            history                      JSONB NOT NULL DEFAULT '[]',
            report                       JSONB
        );

        CREATE TABLE IF NOT EXISTS users (
            id            TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            email         TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL,
            is_active     BOOLEAN NOT NULL DEFAULT TRUE,
            created_at    TIMESTAMPTZ NOT NULL
        );

        CREATE TABLE IF NOT EXISTS auth_tokens (
            token       TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at  TIMESTAMPTZ NOT NULL,
            expires_at  TIMESTAMPTZ NOT NULL
        );

        CREATE TABLE IF NOT EXISTS interview_assignments (
            id                             TEXT PRIMARY KEY,
            user_id                        TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            session_id                     TEXT UNIQUE,
            -- Legacy fields (nullable for old rows)
            topic                          TEXT,
            num_questions                  INTEGER,
            interview_time_limit_minutes   INTEGER,
            -- Adaptive interview inputs (new)
            job_description                TEXT,
            resume_text                    TEXT,
            years_of_experience            NUMERIC(4,1),
            -- Status
            status                         TEXT NOT NULL DEFAULT 'assigned',
            assigned_by                    TEXT,
            assigned_at                    TIMESTAMPTZ NOT NULL,
            started_at                     TIMESTAMPTZ,
            completed_at                   TIMESTAMPTZ
        );

        CREATE TABLE IF NOT EXISTS answers (
            id            SERIAL PRIMARY KEY,
            session_id    TEXT    NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            question_idx  INTEGER NOT NULL,
            question_text TEXT    NOT NULL,
            transcript    TEXT,
            evaluation    JSONB,
            answered_at   TIMESTAMPTZ NOT NULL,
            UNIQUE (session_id, question_idx)
        );

        CREATE TABLE IF NOT EXISTS flags (
            id          SERIAL PRIMARY KEY,
            session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            event_type  TEXT NOT NULL,
            detail      TEXT,
            flagged_at  TIMESTAMPTZ NOT NULL
        );

        CREATE TABLE IF NOT EXISTS interview_videos (
            id           SERIAL PRIMARY KEY,
            session_id   TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            video_path   TEXT NOT NULL,
            question_idx INTEGER NOT NULL DEFAULT 0,
            created_at   TIMESTAMPTZ NOT NULL
        );

        CREATE TABLE IF NOT EXISTS approved_candidates (
            id              SERIAL PRIMARY KEY,
            candidate_id    TEXT UNIQUE NOT NULL,
            candidate_name  TEXT,
            email           TEXT,
            score           NUMERIC(6,2),
            jd_id           TEXT,
            jd_title        TEXT,
            resume_filename TEXT,
            approved_by     TEXT,
            is_ready        BOOLEAN DEFAULT FALSE,
            created_at      TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE INDEX IF NOT EXISTS idx_answers_session_id     ON answers(session_id);
        CREATE INDEX IF NOT EXISTS idx_flags_session_id       ON flags(session_id);
        CREATE INDEX IF NOT EXISTS idx_auth_tokens_user_id    ON auth_tokens(user_id);
        CREATE INDEX IF NOT EXISTS idx_assignments_user_id    ON interview_assignments(user_id);
    """)

    # ── Migrations: add new columns to existing tables safely ─────────────────

    _migrations = [
        # sessions
        "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS user_id TEXT",
        "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS assignment_id TEXT",
        "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'in_progress'",
        "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ",
        "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS interview_time_limit_minutes INTEGER NOT NULL DEFAULT 10",
        # Adaptive additions to sessions
        "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS plan JSONB",
        "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS history JSONB NOT NULL DEFAULT '[]'",
        "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS report JSONB",
        # Adaptive additions to interview_assignments
        "ALTER TABLE interview_assignments ADD COLUMN IF NOT EXISTS job_description TEXT",
        "ALTER TABLE interview_assignments ADD COLUMN IF NOT EXISTS resume_text TEXT",
        "ALTER TABLE interview_assignments ADD COLUMN IF NOT EXISTS years_of_experience NUMERIC(4,1)",
        # Make legacy assignment columns nullable (they are no longer required)
        "ALTER TABLE interview_assignments ADD COLUMN IF NOT EXISTS plan JSONB",
        "ALTER TABLE interview_assignments ALTER COLUMN topic DROP NOT NULL",
        "ALTER TABLE interview_assignments ALTER COLUMN num_questions DROP NOT NULL",
        "ALTER TABLE interview_assignments ALTER COLUMN interview_time_limit_minutes DROP NOT NULL",
    ]
    for stmt in _migrations:
        try:
            cur.execute(stmt)
        except Exception:
            conn.rollback()
            # Re-acquire cursor after rollback
            cur = conn.cursor()

    # ── Foreign key constraints ────────────────────────────────────────────────
    cur.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.table_constraints
                WHERE constraint_name = 'sessions_user_id_fkey'
                  AND table_name = 'sessions'
            ) THEN
                ALTER TABLE sessions
                ADD CONSTRAINT sessions_user_id_fkey
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL;
            END IF;
        END $$;
    """)
    cur.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.table_constraints
                WHERE constraint_name = 'sessions_assignment_id_fkey'
                  AND table_name = 'sessions'
            ) THEN
                ALTER TABLE sessions
                ADD CONSTRAINT sessions_assignment_id_fkey
                FOREIGN KEY (assignment_id) REFERENCES interview_assignments(id) ON DELETE SET NULL;
            END IF;
        END $$;
    """)

    conn.commit()
    cur.close()
    conn.close()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Users & auth ──────────────────────────────────────────────────────────────

def create_user(name: str, email: str, password_hash: str, role: str = "user") -> dict:
    conn = get_conn()
    cur = conn.cursor()
    now = _now()
    user_id = str(uuid.uuid4())
    try:
        cur.execute(
            """
            INSERT INTO users (id, name, email, password_hash, role, is_active, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            """,
            (user_id, name, email.lower(), password_hash, role, True, now),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()
    return {"id": user_id, "name": name, "email": email.lower(), "role": role,
            "is_active": True, "created_at": now.isoformat()}


def get_user(user_id: str) -> dict | None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, name, email, password_hash, role, is_active, created_at FROM users WHERE id = %s",
        (user_id,),
    )
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        return None
    return {"id": row[0], "name": row[1], "email": row[2], "password_hash": row[3],
            "role": row[4], "is_active": row[5],
            "created_at": row[6].isoformat() if row[6] else None}


def get_user_by_email(email: str) -> dict | None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, name, email, password_hash, role, is_active, created_at FROM users WHERE email = %s",
        (email.lower(),),
    )
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        return None
    return {"id": row[0], "name": row[1], "email": row[2], "password_hash": row[3],
            "role": row[4], "is_active": row[5],
            "created_at": row[6].isoformat() if row[6] else None}


def list_users(role: str | None = None) -> list:
    conn = get_conn()
    cur = conn.cursor()
    if role:
        cur.execute(
            "SELECT id, name, email, password_hash, role, is_active, created_at FROM users WHERE role = %s ORDER BY created_at DESC",
            (role,),
        )
    else:
        cur.execute(
            "SELECT id, name, email, password_hash, role, is_active, created_at FROM users ORDER BY created_at DESC"
        )
    rows = cur.fetchall()
    cur.close(); conn.close()
    return [{"id": r[0], "name": r[1], "email": r[2], "password_hash": r[3],
             "role": r[4], "is_active": r[5],
             "created_at": r[6].isoformat() if r[6] else None} for r in rows]


def upsert_user_by_email(name: str, email: str, password_hash: str, role: str = "user") -> dict:
    conn = get_conn()
    cur = conn.cursor()
    now = _now()
    user_id = str(uuid.uuid4())
    cur.execute(
        """
        INSERT INTO users (id, name, email, password_hash, role, is_active, created_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (email) DO UPDATE SET
            name = EXCLUDED.name,
            password_hash = EXCLUDED.password_hash,
            role = EXCLUDED.role,
            is_active = TRUE
        RETURNING id, name, email, password_hash, role, is_active, created_at
        """,
        (user_id, name, email.lower(), password_hash, role, True, now),
    )
    row = cur.fetchone()
    conn.commit(); cur.close(); conn.close()
    return {"id": row[0], "name": row[1], "email": row[2], "password_hash": row[3],
            "role": row[4], "is_active": row[5],
            "created_at": row[6].isoformat() if row[6] else None}


def create_auth_token(token: str, user_id: str, expires_in: timedelta):
    conn = get_conn()
    cur = conn.cursor()
    now = _now()
    cur.execute(
        "INSERT INTO auth_tokens (token, user_id, created_at, expires_at) VALUES (%s,%s,%s,%s)",
        (token, user_id, now, now + expires_in),
    )
    conn.commit(); cur.close(); conn.close()


def get_user_by_token(token: str) -> dict | None:
    conn = get_conn()
    cur = conn.cursor()
    now = _now()
    cur.execute(
        """
        SELECT u.id, u.name, u.email, u.password_hash, u.role, u.is_active, u.created_at
        FROM auth_tokens t
        JOIN users u ON u.id = t.user_id
        WHERE t.token = %s AND t.expires_at > %s
        """,
        (token, now),
    )
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        return None
    return {"id": row[0], "name": row[1], "email": row[2], "password_hash": row[3],
            "role": row[4], "is_active": row[5],
            "created_at": row[6].isoformat() if row[6] else None}


# ── Assignments ───────────────────────────────────────────────────────────────

def create_assignment(
    user_id: str,
    job_description: str,
    resume_text: str,
    years_of_experience: float,
    plan: dict | None = None,
    assigned_by: str | None = None,
    status: str = "assigned",
) -> dict:
    conn = get_conn()
    cur = conn.cursor()
    now = _now()
    assignment_id = str(uuid.uuid4())
    cur.execute(
        """
        INSERT INTO interview_assignments (
            id, user_id, job_description, resume_text, years_of_experience,
            plan, status, assigned_by, assigned_at
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (assignment_id, user_id, job_description, resume_text,
         years_of_experience, json.dumps(plan) if plan else None,
         status, assigned_by, now),
    )
    conn.commit(); cur.close(); conn.close()
    return {
        "id": assignment_id,
        "user_id": user_id,
        "session_id": None,
        "job_description": job_description,
        "resume_text": resume_text,
        "years_of_experience": years_of_experience,
        "plan": plan,
        "status": status,
        "assigned_by": assigned_by,
        "assigned_at": now.isoformat(),
        "started_at": None,
        "completed_at": None,
    }


def _assignment_from_row(row) -> dict:
    return {
        "id": row[0],
        "user_id": row[1],
        "session_id": row[2],
        "job_description": row[3],
        "resume_text": row[4],
        "years_of_experience": float(row[5]) if row[5] is not None else None,
        "status": row[6],
        "assigned_by": row[7],
        "assigned_at": row[8].isoformat() if row[8] else None,
        "started_at": row[9].isoformat() if row[9] else None,
        "completed_at": row[10].isoformat() if row[10] else None,
        "plan": row[11] or {},
    }


def get_assignment(assignment_id: str) -> dict | None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, user_id, session_id, job_description, resume_text, years_of_experience,
               status, assigned_by, assigned_at, started_at, completed_at, plan
        FROM interview_assignments
        WHERE id = %s
        """,
        (assignment_id,),
    )
    row = cur.fetchone()
    cur.close(); conn.close()
    return _assignment_from_row(row) if row else None


def get_assignment_for_user(assignment_id: str, user_id: str) -> dict | None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, user_id, session_id, job_description, resume_text, years_of_experience,
               status, assigned_by, assigned_at, started_at, completed_at, plan
        FROM interview_assignments
        WHERE id = %s AND user_id = %s
        """,
        (assignment_id, user_id),
    )
    row = cur.fetchone()
    cur.close(); conn.close()
    return _assignment_from_row(row) if row else None


def list_assignments_for_user(user_id: str) -> list:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, user_id, session_id, job_description, resume_text, years_of_experience,
               status, assigned_by, assigned_at, started_at, completed_at, plan
        FROM interview_assignments
        WHERE user_id = %s
        ORDER BY assigned_at DESC
        """,
        (user_id,),
    )
    rows = cur.fetchall()
    cur.close(); conn.close()
    return [_assignment_from_row(row) for row in rows]


def list_assignments_with_users() -> list:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            a.id, a.user_id, a.session_id,
            a.job_description, a.resume_text, a.years_of_experience,
            a.status, a.assigned_by, a.assigned_at, a.started_at, a.completed_at, a.plan,
            u.name, u.email
        FROM interview_assignments a
        JOIN users u ON u.id = a.user_id
        ORDER BY a.assigned_at DESC
        """
    )
    rows = cur.fetchall()
    cur.close(); conn.close()
    return [
        {**_assignment_from_row(row[:12]), "user_name": row[12], "user_email": row[13]}
        for row in rows
    ]


def has_pending_assignment(user_id: str) -> bool:
    """Check if user has any pending (assigned or in_progress) assignment."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM interview_assignments WHERE user_id = %s AND status IN ('assigned', 'in_progress')",
        (user_id,),
    )
    count = cur.fetchone()[0]
    cur.close(); conn.close()
    return count > 0


def update_assignment_session(assignment_id: str, session_id: str, status: str = "in_progress"):
    conn = get_conn()
    cur = conn.cursor()
    now = _now()
    cur.execute(
        """
        UPDATE interview_assignments
        SET session_id = %s, status = %s, started_at = COALESCE(started_at, %s)
        WHERE id = %s
        """,
        (session_id, status, now, assignment_id),
    )
    conn.commit(); cur.close(); conn.close()


def mark_assignment_completed(assignment_id: str, status: str = "completed"):
    conn = get_conn()
    cur = conn.cursor()
    now = _now()
    cur.execute(
        "UPDATE interview_assignments SET status = %s, completed_at = %s WHERE id = %s",
        (status, now, assignment_id),
    )
    conn.commit(); cur.close(); conn.close()


def delete_unstarted_assignments():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM interview_assignments WHERE session_id IS NULL")
    deleted = cur.rowcount
    conn.commit(); cur.close(); conn.close()
    return deleted


def delete_all_assignments_for_user(user_id: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM interview_assignments WHERE user_id = %s", (user_id,))
    deleted = cur.rowcount
    conn.commit(); cur.close(); conn.close()
    return deleted


def delete_assignment(assignment_id: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM interview_assignments WHERE id = %s", (assignment_id,))
    conn.commit(); cur.close(); conn.close()


def delete_all_assignments():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM interview_assignments")
    deleted = cur.rowcount
    conn.commit(); cur.close(); conn.close()
    return deleted


def recover_orphaned_assignments() -> int:
    """Recreate assignment rows for sessions that lost their assignment link."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO interview_assignments (
            id, user_id, session_id,
            job_description, resume_text, years_of_experience,
            status, assigned_by, assigned_at, started_at, completed_at
        )
        SELECT
            COALESCE(NULLIF(s.assignment_id, ''), 'recovered-' || s.id),
            s.user_id,
            s.id,
            NULL, NULL, NULL,
            CASE WHEN s.status IN ('completed', 'expired') THEN s.status ELSE 'in_progress' END,
            NULL,
            s.created_at, s.created_at, s.completed_at
        FROM sessions s
        LEFT JOIN interview_assignments ia ON ia.session_id = s.id
        WHERE s.user_id IS NOT NULL AND ia.id IS NULL
        """
    )
    recovered = cur.rowcount
    conn.commit(); cur.close(); conn.close()
    return recovered


# ── Sessions ──────────────────────────────────────────────────────────────────

def create_session(
    session_id: str,
    plan: dict,
    questions: list,
    history: list,
    interview_time_limit_minutes: int,
    user_id: str | None = None,
    assignment_id: str | None = None,
    status: str = "in_progress",
) -> dict:
    """
    Create a new adaptive interview session.

    - plan     : full LLM-generated interview plan (interview_title, phase_budget, etc.)
    - questions: starts as [first_question]; grows via append_question() after each answer
    - history  : starts empty []; grows via append_history() after each evaluation
    """
    conn = get_conn()
    cur = conn.cursor()
    now = _now()

    # Derive legacy fields from plan for backward compat with existing queries
    topic         = plan.get("interview_title", "Adaptive Interview")
    num_questions = plan.get("target_question_count", len(questions))

    cur.execute(
        """
        INSERT INTO sessions (
            id, topic, num_questions, interview_time_limit_minutes,
            questions, plan, history,
            user_id, assignment_id, status, created_at
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            session_id,
            topic,
            num_questions,
            interview_time_limit_minutes,
            json.dumps(questions),
            json.dumps(plan),
            json.dumps(history),
            user_id,
            assignment_id,
            status,
            now,
        ),
    )
    conn.commit(); cur.close(); conn.close()
    return {
        "id": session_id,
        "plan": plan,
        "questions": questions,
        "history": history,
        "interview_time_limit_minutes": interview_time_limit_minutes,
        "user_id": user_id,
        "assignment_id": assignment_id,
        "status": status,
        "created_at": now.isoformat(),
    }


def get_session(session_id: str) -> dict | None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            id, topic, num_questions, interview_time_limit_minutes,
            questions, created_at, user_id, assignment_id, status, completed_at,
            plan, history, report
        FROM sessions
        WHERE id = %s
        """,
        (session_id,),
    )
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        return None
    return {
        "id":                           row[0],
        "topic":                        row[1],
        "num_questions":                row[2],
        "interview_time_limit_minutes": row[3],
        "questions":                    row[4] or [],
        "created_at":                   row[5].isoformat() if row[5] else None,
        "user_id":                      row[6],
        "assignment_id":                row[7],
        "status":                       row[8],
        "completed_at":                 row[9].isoformat() if row[9] else None,
        "plan":                         row[10] or {},
        "history":                      row[11] or [],
        "report":                       row[12],
    }


def append_question(session_id: str, question: dict) -> None:
    """
    Append a newly generated question to session["questions"].
    Called by the router after each answer is evaluated (adaptive next question).
    Uses a Postgres JSONB append to avoid a read-modify-write race.
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE sessions
        SET questions = questions || %s::jsonb
        WHERE id = %s
        """,
        (json.dumps([question]), session_id),
    )
    conn.commit(); cur.close(); conn.close()


def append_history(session_id: str, entry: dict) -> None:
    """
    Append a history entry (question + answer + evaluation) to session["history"].
    Called by the router immediately after evaluate_answer_with_usage().
    history length == number of answered questions == current question index.
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE sessions
        SET history = history || %s::jsonb
        WHERE id = %s
        """,
        (json.dumps([entry]), session_id),
    )
    conn.commit(); cur.close(); conn.close()


def save_report(session_id: str, report: dict) -> None:
    """Persist the LLM-generated final report for a completed session."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE sessions SET report = %s WHERE id = %s",
        (json.dumps(report), session_id),
    )
    conn.commit(); cur.close(); conn.close()


def get_report(session_id: str) -> dict | None:
    """Retrieve the stored final report, or None if not yet generated."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT report FROM sessions WHERE id = %s", (session_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    return row[0] if row else None


def get_all_sessions() -> list:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT
            s.id, s.topic, s.num_questions, s.interview_time_limit_minutes, s.created_at,
            COUNT(DISTINCT a.id) AS answer_count,
            COUNT(DISTINCT f.id) AS flag_count
        FROM sessions s
        LEFT JOIN answers a ON s.id = a.session_id
        LEFT JOIN flags   f ON s.id = f.session_id
        GROUP BY s.id
        ORDER BY s.created_at DESC
    """)
    rows = cur.fetchall()
    cur.close(); conn.close()
    return [
        {"id": r[0], "topic": r[1], "num_questions": r[2],
         "interview_time_limit_minutes": r[3],
         "created_at": r[4].isoformat() if r[4] else None,
         "answer_count": r[5], "flag_count": r[6]}
        for r in rows
    ]


def list_completed_reports() -> list:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            ia.id AS assignment_id,
            s.id  AS session_id,
            u.id  AS user_id,
            u.name        AS user_name,
            u.email       AS user_email,
            s.topic,
            ia.status,
            s.created_at,
            ia.completed_at,
            s.num_questions,
            COUNT(DISTINCT ans.id)  AS answer_count,
            COUNT(DISTINCT f.id)    AS flag_count,
            -- overall_score from stored report if available, else compute from answers
            COALESCE(
                (s.report ->> 'overall_score')::numeric,
                AVG((ans.evaluation ->> 'score')::numeric) FILTER (WHERE ans.evaluation ? 'score')
            ) AS overall_score,
            s.report ->> 'overall_verdict' AS overall_verdict
        FROM interview_assignments ia
        JOIN users    u  ON u.id  = ia.user_id
        JOIN sessions s  ON s.id  = ia.session_id
        LEFT JOIN answers ans ON ans.session_id = s.id
        LEFT JOIN flags   f   ON f.session_id   = s.id
        WHERE ia.status IN ('completed', 'expired')
        GROUP BY ia.id, s.id, u.id
        ORDER BY COALESCE(ia.completed_at, s.created_at) DESC
        """
    )
    rows = cur.fetchall()
    cur.close(); conn.close()
    return [
        {
            "assignment_id":    row[0],
            "session_id":       row[1],
            "user_id":          row[2],
            "user_name":        row[3],
            "user_email":       row[4],
            "topic":            row[5],
            "status":           row[6],
            "created_at":       row[7].isoformat() if row[7] else None,
            "completed_at":     row[8].isoformat() if row[8] else None,
            "total_questions":  row[9],
            "questions_answered": row[10],
            "flag_count":       row[11],
            "overall_score":    round(float(row[12]), 1) if row[12] is not None else 0.0,
            "overall_verdict":  row[13],
        }
        for row in rows
    ]


def mark_session_completed(session_id: str, status: str = "completed"):
    conn = get_conn()
    cur = conn.cursor()
    now = _now()
    cur.execute(
        "UPDATE sessions SET status = %s, completed_at = %s WHERE id = %s",
        (status, now, session_id),
    )
    conn.commit(); cur.close(); conn.close()


# ── Answers ───────────────────────────────────────────────────────────────────

def save_answer(session_id: str, question_idx: int, question_text: str,
                transcript: str, evaluation: dict):
    conn = get_conn()
    cur = conn.cursor()
    now = _now()
    cur.execute(
        """
        INSERT INTO answers
            (session_id, question_idx, question_text, transcript, evaluation, answered_at)
        VALUES (%s,%s,%s,%s,%s,%s)
        ON CONFLICT (session_id, question_idx) DO UPDATE SET
            transcript  = EXCLUDED.transcript,
            evaluation  = EXCLUDED.evaluation,
            answered_at = EXCLUDED.answered_at
        """,
        (session_id, question_idx, question_text, transcript, json.dumps(evaluation), now),
    )
    conn.commit(); cur.close(); conn.close()


def get_answers(session_id: str) -> list:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT question_idx, question_text, transcript, evaluation, answered_at
        FROM answers
        WHERE session_id = %s
        ORDER BY question_idx
        """,
        (session_id,),
    )
    rows = cur.fetchall()
    cur.close(); conn.close()
    return [
        {"question_idx": r[0], "question_text": r[1], "transcript": r[2],
         "evaluation": r[3], "answered_at": r[4].isoformat() if r[4] else None}
        for r in rows
    ]


# ── Flags ─────────────────────────────────────────────────────────────────────

def add_flag(session_id: str, event_type: str, detail: str | None = None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO flags (session_id, event_type, detail, flagged_at) VALUES (%s,%s,%s,%s)",
        (session_id, event_type, detail, _now()),
    )
    conn.commit(); cur.close(); conn.close()


def get_flags(session_id: str) -> list:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, session_id, event_type, detail, flagged_at FROM flags WHERE session_id = %s ORDER BY flagged_at",
        (session_id,),
    )
    rows = cur.fetchall()
    cur.close(); conn.close()
    return [{"id": r[0], "session_id": r[1], "event_type": r[2],
             "detail": r[3], "flagged_at": r[4].isoformat() if r[4] else None}
            for r in rows]



# ── Video ─────────────────────────────────────────────────────────────────────

def add_video(session_id: str, video_path: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO interview_videos (session_id, video_path, created_at) VALUES (%s,%s,%s)",
        (session_id, video_path, _now()),
    )
    conn.commit(); cur.close(); conn.close()


def get_video(session_id: str) -> dict | None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, session_id, video_path, created_at FROM interview_videos WHERE session_id = %s",
        (session_id,),
    )
    rows = cur.fetchall()
    cur.close(); conn.close()
    if not rows:
        return None
    return {"id": rows[0][0], "session_id": rows[0][1], "video_path": rows[0][2],
            "created_at": rows[0][3].isoformat() if rows[0][3] else None}


# ── Delete ────────────────────────────────────────────────────────────────────

def delete_session(session_id: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM sessions WHERE id = %s", (session_id,))
    conn.commit(); cur.close(); conn.close()


# ── Approved candidates (Resume Parser integration) ──────────────────────────

def get_approved_candidates() -> list:
    """Fetch all approved candidates for admin dashboard."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, candidate_id, candidate_name, email, score,
               jd_id, jd_title, resume_filename, approved_by, is_ready, created_at
        FROM approved_candidates
        ORDER BY created_at DESC
        """
    )
    rows = cur.fetchall()
    cols = [desc[0] for desc in cur.description]
    cur.close(); conn.close()
    result = []
    for row in rows:
        item = dict(zip(cols, row))
        if item.get("score") is not None:
            item["score"] = float(item["score"])
        if item.get("created_at") is not None:
            item["created_at"] = item["created_at"].isoformat()
        result.append(item)
    return result


def get_approved_candidate(candidate_id: str) -> dict | None:
    """Fetch a single approved candidate by candidate_id."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, candidate_id, candidate_name, email, score,
               jd_id, jd_title, resume_filename, approved_by, is_ready, created_at
        FROM approved_candidates
        WHERE candidate_id = %s
        """,
        (candidate_id,),
    )
    row = cur.fetchone()
    cols = [desc[0] for desc in cur.description]
    cur.close(); conn.close()
    if not row:
        return None
    item = dict(zip(cols, row))
    if item.get("score") is not None:
        item["score"] = float(item["score"])
    if item.get("created_at") is not None:
        item["created_at"] = item["created_at"].isoformat()
    return item


def update_candidate_email(candidate_id: str, email: str) -> bool:
    """L1 admin fills in missing email → mark candidate as ready."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE approved_candidates
        SET email = %s, is_ready = TRUE
        WHERE candidate_id = %s
        """,
        (email.lower(), candidate_id),
    )
    updated = cur.rowcount
    conn.commit(); cur.close(); conn.close()
    return updated > 0