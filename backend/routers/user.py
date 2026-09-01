"""
routers/user.py
Authenticated user-side APIs for assignments and interviews.
"""

import base64
import os
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse

import database as db
from auth_service import require_role
from schemas import (
    AssignmentSummaryResponse,
    EvaluationResponse,
    FlagRequest,
    SnapshotRequest,
    StartAssignmentResponse,
    SubmitAnswerRequest,
)
from services.ai_service import (
    build_interview_plan_with_usage,
    generate_initial_question_with_usage,
    generate_next_question_with_usage,
    evaluate_answer_with_usage,
    generate_final_report_with_usage,
)
from services.stt_service import transcribe_audio
from services.tts_service import generate_speech

router = APIRouter(prefix="/user", tags=["user"])


def _rm(*paths):
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def _get_user_assignment(assignment_id: str, user_id: str) -> dict:
    assignment = db.get_assignment_for_user(assignment_id, user_id)
    if not assignment:
        raise HTTPException(404, detail="Assignment not found")
    return assignment


def _get_owned_session(session_id: str, user_id: str) -> dict:
    session = db.get_session(session_id)
    if not session or session.get("user_id") != user_id:
        raise HTTPException(404, detail="Session not found")
    return session


@router.get("/assignments", response_model=list[AssignmentSummaryResponse])
def list_assignments(user=Depends(require_role("user"))):
    assignments = db.list_assignments_for_user(user["id"])
    result = []
    for item in assignments:
        plan = item.get("plan") or {}
        result.append(AssignmentSummaryResponse(
            id=item["id"],
            interview_title=plan.get("interview_title") or "Interview Assignment",
            target_question_count=plan.get("target_question_count"),
            recommended_interview_time_minutes=plan.get("recommended_interview_time_minutes"),
            status=item["status"],
            assigned_at=item["assigned_at"],
            started_at=item["started_at"],
            completed_at=item["completed_at"],
            session_id=item["session_id"],
        ))
    return result


@router.post("/assignments/{assignment_id}/start", response_model=StartAssignmentResponse)
def start_assignment(assignment_id: str, user=Depends(require_role("user"))):
    assignment = _get_user_assignment(assignment_id, user["id"])

    if assignment["status"] in {"completed", "expired"}:
        raise HTTPException(400, detail="This interview is already closed")

    if assignment["session_id"]:
        session = _get_owned_session(assignment["session_id"], user["id"])
        plan = session.get("plan") or {}
        questions = session.get("questions") or []
        history = session.get("history") or []
        cur_idx = len(history)
        first_question = questions[cur_idx] if cur_idx < len(questions) else questions[-1]
        return StartAssignmentResponse(
            assignment_id=assignment["id"],
            session_id=session["id"],
            interview_title=plan.get("interview_title", "Interview"),
            candidate_seniority=plan.get("candidate_seniority", ""),
            target_question_count=plan.get("target_question_count", len(questions)),
            question_count_reasoning=plan.get("question_count_reasoning", ""),
            recommended_interview_time_minutes=session["interview_time_limit_minutes"],
            phase_budget=plan.get("phase_budget", {}),
            first_question=first_question,
            status=assignment["status"],
            started_at=session.get("created_at"),
        )

    plan = assignment.get("plan") or {}
    if not plan:
        plan, _ = build_interview_plan_with_usage(
            job_description=assignment["job_description"],
            resume_text=assignment["resume_text"],
            years_of_experience=assignment["years_of_experience"],
        )
    first_question, _ = generate_initial_question_with_usage(
        interview_plan=plan,
        years_of_experience=assignment["years_of_experience"],
    )
    session_id = str(uuid.uuid4())
    created = db.create_session(
        session_id=session_id,
        plan=plan,
        questions=[first_question],
        history=[],
        interview_time_limit_minutes=plan["recommended_interview_time_minutes"],
        user_id=user["id"],
        assignment_id=assignment["id"],
        status="in_progress",
    )
    db.update_assignment_session(assignment["id"], session_id, status="in_progress")

    return StartAssignmentResponse(
        assignment_id=assignment["id"],
        session_id=session_id,
        interview_title=plan["interview_title"],
        candidate_seniority=plan["candidate_seniority"],
        target_question_count=plan["target_question_count"],
        question_count_reasoning=plan["question_count_reasoning"],
        recommended_interview_time_minutes=plan["recommended_interview_time_minutes"],
        phase_budget=plan["phase_budget"],
        first_question=first_question,
        status="in_progress",
        started_at=created.get("created_at") if isinstance(created, dict) else None,
    )


@router.post("/assignments/{assignment_id}/complete")
def complete_assignment(
    assignment_id: str,
    status: str = "completed",
    user=Depends(require_role("user")),
):
    assignment = _get_user_assignment(assignment_id, user["id"])
    if status not in {"completed", "expired"}:
        raise HTTPException(400, detail="status must be completed or expired")
    if assignment["session_id"]:
        _get_owned_session(assignment["session_id"], user["id"])
        db.mark_session_completed(assignment["session_id"], status=status)
    db.mark_assignment_completed(assignment["id"], status=status)
    return {"ok": True, "status": status}


@router.get("/interview/session/{session_id}")
def get_user_session(session_id: str, user=Depends(require_role("user"))):
    return _get_owned_session(session_id, user["id"])


@router.get("/interview/speak-question")
def speak_question(
    session_id: str,
    question_idx: int,
    background_tasks: BackgroundTasks,
    user=Depends(require_role("user")),
):
    session = _get_owned_session(session_id, user["id"])
    questions = session["questions"]
    if not 0 <= question_idx < len(questions):
        raise HTTPException(400, detail=f"question_idx must be 0-{len(questions) - 1}")

    out_path = f"/tmp/ts_q_{uuid.uuid4()}.wav"
    q = questions[question_idx]
    question_text = q["question"] if isinstance(q, dict) else q
    try:
        generate_speech(question_text, out_path)
    except Exception as exc:
        raise HTTPException(500, detail=str(exc))

    background_tasks.add_task(_rm, out_path)
    return FileResponse(out_path, media_type="audio/wav")


@router.post("/interview/answer", response_model=EvaluationResponse)
def submit_answer(body: SubmitAnswerRequest, user=Depends(require_role("user"))):
    session = _get_owned_session(body.session_id, user["id"])
    current_question = session["questions"][body.question_idx]
    result, _token_usage = evaluate_answer_with_usage(
        phase=current_question["phase"],
        topic=session["topic"],
        question=body.question,
        answer=body.answer,
    )
    db.save_answer(
        session_id=body.session_id,
        question_idx=body.question_idx,
        question_text=body.question,
        transcript=body.answer,
        evaluation=result,
    )
    return EvaluationResponse(**result)


@router.post("/interview/voice-answer")
async def voice_answer(
    session_id: str,
    question_idx: int,
    question: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    user=Depends(require_role("user")),
):
    session = _get_owned_session(session_id, user["id"])
    in_path = f"/tmp/ts_in_{uuid.uuid4()}.wav"

    try:
        with open(in_path, "wb") as handle:
            handle.write(await file.read())

        transcript = transcribe_audio(in_path)
        current_question = session["questions"][question_idx]
        result, _token_usage = evaluate_answer_with_usage(
            phase=current_question["phase"],
            topic=session["topic"],
            question=question,
            answer=transcript,
        )
        db.save_answer(
            session_id=session_id,
            question_idx=question_idx,
            question_text=question,
            transcript=transcript,
            evaluation=result,
        )

        background_tasks.add_task(_rm, in_path)
        return {"transcript": transcript, "evaluation": result}
    except Exception as exc:
        _rm(in_path)
        raise HTTPException(500, detail=str(exc))


@router.post("/interview/flag")
def add_flag(body: FlagRequest, user=Depends(require_role("user"))):
    _get_owned_session(body.session_id, user["id"])
    db.add_flag(body.session_id, body.event_type, body.detail)
    return {"ok": True}


@router.post("/interview/snapshot")
def add_snapshot(body: SnapshotRequest, user=Depends(require_role("user"))):
    _get_owned_session(body.session_id, user["id"])
    db.add_snapshot(body.session_id, body.image_data)
    return {"ok": True}


@router.get("/interview/snapshot/{snapshot_id}")
def get_snapshot(snapshot_id: int, user=Depends(require_role("user"))):
    snapshot = db.get_snapshot(snapshot_id)
    if not snapshot:
        raise HTTPException(404, detail="Snapshot not found")
    _get_owned_session(snapshot["session_id"], user["id"])
    return Response(content=base64.b64decode(snapshot["image_data"]), media_type="image/jpeg")