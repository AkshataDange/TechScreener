"""
routers/review.py
Reviewer-facing endpoints: list sessions, full session review, PDF export.
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

import database as db

router = APIRouter(prefix="/review", tags=["review"])


@router.delete("/{session_id}")
def delete_session(session_id: str):
    """Delete a session and all its associated answers, flags, and snapshots."""
    if not db.get_session(session_id):
        raise HTTPException(404, detail=f"Session {session_id} not found")
    db.delete_session(session_id)
    return {"ok": True}


@router.get("/sessions")
def list_sessions():
    """Returns all sessions with summary counts (answers, flags)."""
    return db.get_all_sessions()


@router.get("/{session_id}")
def get_review(session_id: str):
    """
    Full review payload for a completed session:
      - session metadata (topic, questions, created_at)
      - per-question answers with transcript + evaluation JSON
      - proctoring flag events
      - snapshot metadata (use GET /interview/snapshot/{id} to fetch images)
      - summary (avg score, flag count, etc.)
    """
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(404, detail=f"Session {session_id} not found")

    answers   = db.get_answers(session_id)
    flags     = db.get_flags(session_id)
    snapshots = db.get_snapshots(session_id)

    scored = [
        a for a in answers
        if a["evaluation"] and isinstance(a["evaluation"].get("score"), (int, float))
    ]
    avg_score = (
        round((sum(a["evaluation"]["score"] for a in scored) / len(scored)) * 2, 1)
        if scored else 0.0
    )

    return {
        "session": session,
        "answers": answers,
        "flags":   flags,
        "snapshots": snapshots,
        "summary": {
            "avg_score":          avg_score,
            "questions_answered": len(scored),
            "total_questions":    len(session["questions"]),
            "flag_count":         len(flags),
            "snapshot_count":     len(snapshots),
        },
    }
