"""
schemas.py
Pydantic models for all API request and response bodies.
"""

from pydantic import BaseModel, Field, model_validator
from typing import List, Optional, Any


# ── Interview session ─────────────────────────────────────────────────────────

class CreateSessionRequest(BaseModel):
    """
    Admin no longer supplies topic / num_questions / time_limit.
    The LLM reads the JD + resume and decides everything.
    """
    job_description: str = Field(..., description="Full job description text")
    resume_text: str     = Field(..., description="Candidate's resume as plain text")
    years_of_experience: float = Field(..., ge=0, description="Years of professional experience")
    # Optional: still allow linking to an assignment at session-creation time
    user_id:        Optional[str] = None
    assignment_id:  Optional[str] = None


class QuestionItem(BaseModel):
    """
    A single interview question with full metadata.
    phase / skill_tag / source_focus are new fields added by the adaptive engine.
    """
    question: str
    phase: str = "communication"          # communication | resume | technical
    type: str  = "conversational"         # conversational | conceptual | scenario | practical
    difficulty: str = "easy"             # easy | medium | hard
    requires_code_editor: bool = False
    skill_tag: Optional[str] = None      # e.g. "SQL indexing", "Django REST project"
    source_focus: str = "general"        # general | resume | jd | shared
    followup_to_question_idx: Optional[int] = None

    @model_validator(mode="before")
    @classmethod
    def coerce_string(cls, v: Any) -> Any:
        """Allow legacy plain-string questions from old DB sessions."""
        if isinstance(v, str):
            return {"question": v, "phase": "communication", "type": "conversational"}
        return v


class PhaseBudget(BaseModel):
    """How many questions are allocated per phase."""
    start: int
    count: int


class PhaseBreakdown(BaseModel):
    communication: PhaseBudget
    resume: PhaseBudget
    technical: PhaseBudget
    total: int


class CreateSessionResponse(BaseModel):
    """
    Returned when a new interview session is created.
    Contains plan metadata + the first question to display immediately.
    """
    session_id: str
    interview_title: str
    candidate_seniority: str                  # entry_level | junior | mid_level | senior
    target_question_count: int                # decided by LLM (8–16)
    question_count_reasoning: str            # LLM's one-line explanation for the count
    recommended_interview_time_minutes: int  # decided by LLM (30–60)
    phase_budget: dict                       # {communication, resume, technical} counts
    first_question: QuestionItem             # first question — display this immediately


# ── Answer submission ─────────────────────────────────────────────────────────

class SubmitAnswerRequest(BaseModel):
    session_id: str
    question_idx: int   # 0-based; must equal len(history) at submission time
    answer: str         # typed or transcribed answer text


class EvaluationResponse(BaseModel):
    """
    Per-answer evaluation. Fields vary slightly by phase but this model
    covers the superset. Optional fields may be absent for communication phase.
    """
    score: int                              # 1–5
    verdict: str                            # Excellent | Good | Fair | Poor
    phase: str                              # communication | resume | technical
    dimension: str                          # e.g. "Technical Competency (JD-aligned)"
    overview: str                           # 2–3 sentence assessment
    positives: List[str]                    # what the candidate did well
    gaps: List[str]                         # what was missing or incorrect
    improvement_tip: str                    # one actionable sentence
    quality_bucket: str                     # strong | mixed | weak
    recommended_followup: str              # deepen | clarify | pivot

    # Legacy fields kept for backward compat with old frontend/DB reads
    correct: List[str] = []
    missed: List[str] = []
    key_concepts: List[dict] = []


class AnswerPipelineResponse(BaseModel):
    """
    Wrapper returned by the shared answer pipeline (`_process_answer`).
    Keeps backward compat with frontend code that expects status/next_question.
    """
    status: str                               # "next" | "complete"
    evaluation: EvaluationResponse
    report: Optional[dict] = None             # Final report when status == "complete"
    next_question: Optional[QuestionItem] = None
    next_question_idx: Optional[int] = None


# ── Proctoring ────────────────────────────────────────────────────────────────

class FlagRequest(BaseModel):
    session_id: str
    event_type: str     # "tab_switch" | "window_blur" | "copy" | "paste" | "devtools"
    detail: Optional[str] = None


class SnapshotRequest(BaseModel):
    session_id: str
    image_data: str     # base64-encoded JPEG


# ── Review ────────────────────────────────────────────────────────────────────

class ReviewSummary(BaseModel):
    avg_score: float
    questions_answered: int
    total_questions: int
    flag_count: int


# ── Final report ──────────────────────────────────────────────────────────────

class DimensionScore(BaseModel):
    score: float
    summary: str


class FinalReportResponse(BaseModel):
    overall_score: float
    overall_verdict: str          # Strong Hire | Hire | Borderline | No Hire
    recommendation: str
    dimension_scores: dict        # {communication, resume, technical} → DimensionScore
    strengths: List[str]
    concerns: List[str]
    suggested_l2_focus_areas: List[str]
    overall_summary: str


# ── Auth ──────────────────────────────────────────────────────────────────────

class RegisterUserRequest(BaseModel):
    name: str
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


class AuthUserResponse(BaseModel):
    id: str
    name: str
    email: str
    role: str


class AuthResponse(BaseModel):
    token: str
    user: AuthUserResponse


# ── User assignments ──────────────────────────────────────────────────────────

class AssignmentSummaryResponse(BaseModel):
    id: str
    interview_title: Optional[str] = None      # from plan, populated after session starts
    target_question_count: Optional[int] = None
    recommended_interview_time_minutes: Optional[int] = None
    status: str
    assigned_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    session_id: Optional[str] = None


class StartAssignmentResponse(BaseModel):
    assignment_id: str
    session_id: str
    interview_title: str
    candidate_seniority: str
    target_question_count: int
    question_count_reasoning: str
    recommended_interview_time_minutes: int
    phase_budget: dict
    first_question: QuestionItem
    status: str


# ── Admin ─────────────────────────────────────────────────────────────────────

class AdminCreateAssignmentRequest(BaseModel):
    """
    Admin now supplies JD + resume + experience instead of topic/num_questions/time.
    The LLM decides question count and duration when the candidate starts the interview.
    """
    user_id: str
    job_description: str
    resume_text: str
    years_of_experience: float = Field(..., ge=0)


class AdminAssignmentResponse(BaseModel):
    id: str
    user_id: str
    user_name: str
    user_email: str
    # Plan fields — populated after the interview starts (LLM decides these)
    interview_title: Optional[str] = None
    target_question_count: Optional[int] = None
    recommended_interview_time_minutes: Optional[int] = None
    status: str
    assigned_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    session_id: Optional[str] = None


class AdminReportSummaryResponse(BaseModel):
    assignment_id: str
    session_id: str
    user_id: str
    user_name: str
    user_email: str
    interview_title: Optional[str] = None
    status: str
    # Phase-aware scores (weighted: comm 20%, resume 40%, tech 40%)
    overall_score: Optional[float] = None
    overall_verdict: Optional[str] = None      # Strong Hire | Hire | Borderline | No Hire
    questions_answered: int
    total_questions: int
    flag_count: int
    created_at: Optional[str] = None
    completed_at: Optional[str] = None