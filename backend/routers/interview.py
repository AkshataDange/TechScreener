"""
routers/interview.py

Adaptive L1 interview flow:
  POST /session        →  build plan + generate first question (LLM decides count & time)
  GET  /next-question  →  fetch the current unanswered question for the frontend
  GET  /speak-question →  Piper TTS → WAV → browser plays it
  POST /answer         →  text answer → evaluate → generate next question (adaptive)
  POST /voice-answer   →  audio → Whisper STT → evaluate → generate next question (adaptive)
  GET  /report/{id}    →  final report (generated after last answer is submitted)
"""

import os
import uuid
import base64
import logging

from fastapi import APIRouter, HTTPException, UploadFile, File, BackgroundTasks, WebSocket
from fastapi.responses import FileResponse, Response

import database as db
from schemas import (
    CreateSessionRequest, CreateSessionResponse,
    SubmitAnswerRequest, EvaluationResponse, AnswerPipelineResponse,
    FlagRequest, SnapshotRequest,
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
from services.streaming_stt_service import handle_streaming_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/interview", tags=["interview"])

# Running token totals per session (in-memory, for logging only)
_SESSION_TOKEN_TOTALS: dict[str, dict[str, int]] = {}
_SESSION_FINAL_LOGGED: set[str] = set()


# ── Session ────────────────────────────────────────────────────────────────────

@router.post("/session", response_model=CreateSessionResponse)
def create_session(body: CreateSessionRequest):
    """
    Starts an adaptive L1 interview.

    The LLM reads the JD + resume and decides:
      - How many questions to ask (8–16)
      - How long the interview should take (30–60 min)
      - Which topics to cover and in what order

    Returns the interview plan metadata and the very first question.
    Admin no longer supplies num_questions or interview_time_limit_minutes.
    """
    # ── 1. Build interview plan (LLM decides count + duration) ────────────────
    plan, plan_usage = build_interview_plan_with_usage(
        job_description=body.job_description,
        resume_text=body.resume_text,
        years_of_experience=body.years_of_experience,
    )
    _record_token_usage("startup", "build_interview_plan", plan_usage)

    # ── 2. Generate the opening question (always Phase 1: Communication) ───────
    first_question, q_usage = generate_initial_question_with_usage(
        interview_plan=plan,
        years_of_experience=body.years_of_experience,
    )
    _record_token_usage("startup", "generate_initial_question", q_usage)

    # ── 3. Persist session ─────────────────────────────────────────────────────
    session_id = str(uuid.uuid4())

    # questions  : grows by one after every evaluated answer
    # history    : parallel list — mirrors questions[], populated after evaluation
    # plan       : full interview plan from the LLM
    db.create_session(
        session_id=session_id,
        plan=plan,
        questions=[first_question],          # starts with one question
        history=[],                          # empty until first answer is submitted
        interview_time_limit_minutes=plan["recommended_interview_time_minutes"],
    )

    return CreateSessionResponse(
        session_id=session_id,
        interview_title=plan["interview_title"],
        candidate_seniority=plan["candidate_seniority"],
        target_question_count=plan["target_question_count"],
        question_count_reasoning=plan.get("question_count_reasoning", ""),
        recommended_interview_time_minutes=plan["recommended_interview_time_minutes"],
        phase_budget=plan["phase_budget"],
        first_question=first_question,
    )


@router.get("/session/{session_id}")
def get_session(session_id: str):
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(404, detail="Session not found")
    return session


@router.get("/next-question/{session_id}")
def get_next_question(session_id: str):
    """
    Returns the current unanswered question for the frontend.
    question_idx = len(history) because a question is only 'answered'
    once its evaluation is appended to history.
    """
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(404, detail="Session not found")

    history = session.get("history", [])
    questions = session.get("questions", [])
    current_idx = len(history)           # next unanswered question

    if current_idx >= len(questions):
        # All generated questions have been answered — interview complete
        raise HTTPException(409, detail="Interview complete. No more questions.")

    return {
        "question_idx": current_idx,
        "question": questions[current_idx],
        "total_questions": session["plan"]["target_question_count"],
        "questions_answered": current_idx,
    }


# ── Speak a question (Piper TTS → WAV) ────────────────────────────────────────

@router.get("/speak-question")
def speak_question(
    session_id: str,
    question_idx: int,
    background_tasks: BackgroundTasks,
):
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(404, detail="Session not found")

    questions = session.get("questions", [])
    if not 0 <= question_idx < len(questions):
        raise HTTPException(400, detail=f"question_idx {question_idx} not yet generated")

    out_path = f"/tmp/ts_q_{uuid.uuid4()}.wav"
    try:
        q = questions[question_idx]
        question_text = q["question"] if isinstance(q, dict) else q
        generate_speech(question_text, out_path)
    except Exception as e:
        raise HTTPException(500, detail=str(e))

    background_tasks.add_task(_rm, out_path)
    return FileResponse(out_path, media_type="audio/wav")


# ── Shared answer processing ───────────────────────────────────────────────────

def _process_answer(
    session_id: str,
    question_idx: int,
    answer_text: str,
) -> dict:
    """
    Core adaptive pipeline shared by /answer (text) and /voice-answer (audio).

    Steps:
      1. Validate session and question index
      2. Evaluate the answer with the phase-appropriate rubric
      3. Append to history
      4. If interview is complete → generate final report
      5. If more questions remain → generate the next question adaptively
         and append it to session["questions"]
      6. Return evaluation + status (next_question | complete)
    """
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(404, detail="Session not found")

    questions = session.get("questions", [])
    history   = session.get("history", [])
    plan      = session.get("plan", {})

    # Guard: question_idx must be the current unanswered question
    expected_idx = len(history)
    if question_idx != expected_idx:
        raise HTTPException(
            400,
            detail=f"Out-of-order submission: expected question_idx={expected_idx}, got {question_idx}.",
        )
    if question_idx >= len(questions):
        raise HTTPException(400, detail="question_idx out of range for generated questions.")

    current_question = questions[question_idx]

    # ── 1. Evaluate ────────────────────────────────────────────────────────────
    evaluation, eval_usage = evaluate_answer_with_usage(
        phase=current_question["phase"],           # always from stored question object
        topic=current_question.get("skill_tag") or plan.get("interview_title", "General"),
        question=current_question["question"],
        answer=answer_text,
    )
    _record_token_usage(session_id, "evaluate_answer", eval_usage)

    # ── 2. Build history entry and persist ─────────────────────────────────────
    history_entry = {
        "question_idx": question_idx,
        "phase":        current_question["phase"],
        "question":     current_question["question"],
        "skill_tag":    current_question.get("skill_tag"),
        "source_focus": current_question.get("source_focus"),
        "difficulty":   current_question.get("difficulty"),
        "answer":       answer_text,
        "evaluation":   evaluation,
    }
    history.append(history_entry)

    db.save_answer(
        session_id=session_id,
        question_idx=question_idx,
        question_text=current_question["question"],
        transcript=answer_text,
        evaluation=evaluation,
    )
    db.append_history(session_id, history_entry)   # keeps session["history"] in sync

    total_questions = plan.get("target_question_count", len(questions))
    interview_complete = len(history) >= total_questions

    # ── 3a. Interview complete → generate final report ─────────────────────────
    if interview_complete:
        report, report_usage = generate_final_report_with_usage(plan, history)
        _record_token_usage(session_id, "generate_final_report", report_usage)
        db.save_report(session_id, report)
        _record_token_usage(session_id, "session_total", {}, final=True)

        return {
            "status": "complete",
            "evaluation": evaluation,
            "report": report,
            "next_question": None,
            "next_question_idx": None,
        }

    # ── 3b. More questions remain → generate next question adaptively ──────────
    next_question, next_q_usage = generate_next_question_with_usage(
        interview_plan=plan,
        years_of_experience=plan.get("years_of_experience", 0),
        num_questions=total_questions,
        question_idx=question_idx,
        history=history,
        latest_evaluation=evaluation,
    )
    _record_token_usage(session_id, "generate_next_question", next_q_usage)

    questions.append(next_question)
    db.append_question(session_id, next_question)   # persists the new question

    return {
        "status": "next",
        "evaluation": evaluation,
        "report": None,
        "next_question": next_question,
        "next_question_idx": question_idx + 1,
    }


# ── Text answer (conceptual / code questions) ──────────────────────────────────

@router.post("/answer", response_model=AnswerPipelineResponse)
def submit_answer(body: SubmitAnswerRequest):
    return _process_answer(
        session_id=body.session_id,
        question_idx=body.question_idx,
        answer_text=body.answer,
    )


# ── Voice answer: Whisper STT → evaluate → next question ──────────────────────

@router.post("/voice-answer")
async def voice_answer(
    session_id: str,
    question_idx: int,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
):
    in_path = f"/tmp/ts_in_{uuid.uuid4()}.wav"
    try:
        content = await file.read()
        with open(in_path, "wb") as f:
            f.write(content)

        transcript = transcribe_audio(in_path)

        result = _process_answer(
            session_id=session_id,
            question_idx=question_idx,
            answer_text=transcript,
        )

        background_tasks.add_task(_rm, in_path)
        return {"transcript": transcript, **result}

    except HTTPException:
        _rm(in_path)
        raise
    except Exception as e:
        _rm(in_path)
        raise HTTPException(500, detail=str(e))


# ── Final report ───────────────────────────────────────────────────────────────

@router.get("/report/{session_id}")
def get_report(session_id: str):
    """
    Returns the final report. Generated automatically after the last answer
    is submitted via /answer or /voice-answer — this endpoint just retrieves it.
    """
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(404, detail="Session not found")
    report = db.get_report(session_id)
    if not report:
        raise HTTPException(404, detail="Report not yet generated. Submit all answers first.")
    return report


# ── Proctoring ─────────────────────────────────────────────────────────────────

@router.post("/flag")
def add_flag(body: FlagRequest):
    if not db.get_session(body.session_id):
        raise HTTPException(404, detail="Session not found")
    db.add_flag(body.session_id, body.event_type, body.detail)
    return {"ok": True}


@router.post("/snapshot")
def add_snapshot(body: SnapshotRequest):
    if not db.get_session(body.session_id):
        raise HTTPException(404, detail="Session not found")
    db.add_snapshot(body.session_id, body.image_data)
    return {"ok": True}


@router.get("/snapshot/{snapshot_id}")
def get_snapshot(snapshot_id: int):
    data = db.get_snapshot_data(snapshot_id)
    if not data:
        raise HTTPException(404)
    return Response(content=base64.b64decode(data), media_type="image/jpeg")


# ── Video recording ────────────────────────────────────────────────────────────

VIDEO_UPLOAD_DIR = "/home/neosoft/Documents/AiInterview/techscreen/videos"
os.makedirs(VIDEO_UPLOAD_DIR, exist_ok=True)


@router.post("/video")
async def upload_video(
    session_id: str,
    chunk_index: int,
    is_final: bool = False,
    video: UploadFile = File(...),
):
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")

    video_filename = f"{session_id}.webm"
    video_path = os.path.join(VIDEO_UPLOAD_DIR, video_filename)
    content = await video.read()

    mode = "wb" if chunk_index == 0 else "ab"
    with open(video_path, mode) as f:
        f.write(content)

    db.add_video(session_id, video_filename)
    return {"ok": True, "chunk_index": chunk_index, "path": video_path}


@router.get("/video/{session_id}")
def get_video(session_id: str):
    video_filename = f"{session_id}.webm"
    video_path = os.path.join(VIDEO_UPLOAD_DIR, video_filename)
    if not os.path.exists(video_path):
        raise HTTPException(404, "Video not found")
    return FileResponse(video_path, media_type="video/webm")


# ── WebSocket for real-time transcription ─────────────────────────────────────

@router.websocket("/ws/transcribe/{session_id}")
async def websocket_transcribe(websocket: WebSocket, session_id: str):
    await websocket.accept()
    await handle_streaming_session(websocket, session_id)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _rm(*paths):
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


def _record_token_usage(session_id: str, event: str, token_usage: dict, final: bool = False):
    totals = _SESSION_TOKEN_TOTALS.setdefault(
        session_id,
        {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    )
    input_tokens  = int(token_usage.get("input_tokens", 0))
    output_tokens = int(token_usage.get("output_tokens", 0))
    total_tokens  = int(token_usage.get("total_tokens", input_tokens + output_tokens))

    totals["input_tokens"]  += input_tokens
    totals["output_tokens"] += output_tokens
    totals["total_tokens"]  += total_tokens

    logger.info(
        "[token-usage] event=%s session_id=%s in=%d out=%d total=%d "
        "running_in=%d running_out=%d running_total=%d",
        event, session_id,
        input_tokens, output_tokens, total_tokens,
        totals["input_tokens"], totals["output_tokens"], totals["total_tokens"],
    )

    if final and session_id not in _SESSION_FINAL_LOGGED:
        _SESSION_FINAL_LOGGED.add(session_id)
        logger.info(
            "[token-usage] event=session_total session_id=%s in=%d out=%d total=%d",
            session_id,
            totals["input_tokens"], totals["output_tokens"], totals["total_tokens"],
        )