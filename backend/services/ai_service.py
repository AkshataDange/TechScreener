"""
services/ai_service.py

Adaptive L1 interview engine — phase-driven question generation and evaluation.

Interview phases (computed from num_questions):
  Phase 1 — COMMUNICATION  : first ~20% of questions (min 2, max 4)
             Warm-up; tell me about yourself, walk me through experience/projects.
  Phase 2 — RESUME         : next ~40% of questions
             Probe specific projects, past decisions, and resume claims.
  Phase 3 — TECHNICAL      : final ~40% of questions
             JD-aligned skills; lenient L1 depth, not leetcode grind.

Evaluation tracks three separate dimensions aligned to the phases above.
"""

import json
import math

from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser

from config import settings


# ── LLM client ────────────────────────────────────────────────────────────────

llm = ChatGroq(
    model="llama-3.3-70b-versatile",
    api_key=settings.groq_api_key,
    temperature=0,
)


# ── Phase budget helpers ───────────────────────────────────────────────────────

def compute_phase_budget(num_questions: int) -> dict:
    """
    Given total question count, return start index (0-based) and count for each phase.
    Phase 1 — COMMUNICATION : ~20%, min 2, max 4
    Phase 2 — RESUME        : ~40%
    Phase 3 — TECHNICAL     : remainder (also ~40%)
    """
    comm_count = max(2, min(4, round(num_questions * 0.20)))
    remaining = num_questions - comm_count
    resume_count = math.ceil(remaining * 0.50)   # 50% of remaining ≈ 40% of total
    tech_count = remaining - resume_count

    return {
        "communication": {"start": 0, "count": comm_count},
        "resume":        {"start": comm_count, "count": resume_count},
        "technical":     {"start": comm_count + resume_count, "count": tech_count},
        "total": num_questions,
    }


def get_current_phase(question_idx: int, budget: dict) -> str:
    """Return the phase name for a given 0-based question index."""
    if question_idx < budget["communication"]["start"] + budget["communication"]["count"]:
        return "communication"
    if question_idx < budget["resume"]["start"] + budget["resume"]["count"]:
        return "resume"
    return "technical"


# ── Normalizers ───────────────────────────────────────────────────────────────

def _experience_band(years: float) -> str:
    if years < 1:
        return "entry_level"
    if years < 3:
        return "junior"
    if years < 6:
        return "mid_level"
    return "senior"


def _normalize_question_item(item: dict | str, fallback_idx: int | None = None) -> dict:
    if isinstance(item, str):
        return {
            "question": item.strip(),
            "phase": "communication",
            "type": "conversational",
            "difficulty": "easy",
            "requires_code_editor": False,
            "skill_tag": None,
            "source_focus": "general",
            "followup_to_question_idx": None,
        }

    question_text = str(item.get("question", "")).strip()
    if not question_text:
        raise ValueError("Question payload missing question text")

    q_type = str(item.get("type", "conversational")).strip() or "conversational"
    requires_code = item.get("requires_code_editor", q_type == "practical")

    followup_idx = item.get("followup_to_question_idx", fallback_idx)
    if followup_idx in {"", None}:
        followup_idx = None
    elif isinstance(followup_idx, bool):
        followup_idx = None
    else:
        try:
            followup_idx = int(followup_idx)
        except (ValueError, TypeError):
            followup_idx = None

    return {
        "question": question_text,
        "phase": str(item.get("phase", "communication")).strip() or "communication",
        "type": q_type,
        "difficulty": str(item.get("difficulty", "easy")).strip() or "easy",
        "requires_code_editor": bool(requires_code),
        "skill_tag": str(item.get("skill_tag", "")).strip() or None,
        "source_focus": str(item.get("source_focus", "general")).strip() or "general",
        "followup_to_question_idx": followup_idx,
    }


def _normalize_plan_payload(payload: dict, years: float) -> dict:
    # LLM decides both question count and interview duration — no admin input.
    try:
        num_questions = max(8, min(16, int(payload.get("target_question_count", 10))))
    except (TypeError, ValueError):
        num_questions = 10

    try:
        recommended_time = max(30, min(60, int(payload.get("recommended_interview_time_minutes", 40))))
    except (TypeError, ValueError):
        recommended_time = 40

    # Sanity-check: time should be roughly 3-4 min x questions.
    min_expected = num_questions * 3
    max_expected = num_questions * 4
    if not (min_expected <= recommended_time <= max_expected + 5):
        recommended_time = max(30, min(60, round((min_expected + max_expected) / 2)))

    budget = compute_phase_budget(num_questions)

    def _clean_list(key):
        return [str(x).strip() for x in payload.get(key, []) if str(x).strip()]

    return {
        "interview_title": str(payload.get("interview_title", "L1 Technical Interview")).strip(),
        "candidate_seniority": str(payload.get("candidate_seniority", _experience_band(years))).strip(),
        "years_of_experience": years,
        "target_question_count": num_questions,
        "question_count_reasoning": str(payload.get("question_count_reasoning", "")).strip(),
        "recommended_interview_time_minutes": recommended_time,
        "phase_budget": budget,

        # Communication anchors — used to seed warm-up questions
        "communication_anchors": _clean_list("communication_anchors"),

        # Resume layer — concrete projects and claims to probe
        "resume_projects": _clean_list("resume_projects"),
        "resume_claims_to_validate": _clean_list("resume_claims_to_validate"),
        "resume_skills": _clean_list("resume_skills"),

        # JD layer — skills and must-cover areas for technical phase
        "jd_skills": _clean_list("jd_skills"),
        "jd_must_cover": _clean_list("jd_must_cover"),
        "jd_nice_to_have": _clean_list("jd_nice_to_have"),

        "summary": str(payload.get("summary", "")).strip(),
    }


def _serialize_history(history: list[dict]) -> list[dict]:
    return [
        {
            "question_idx": h.get("question_idx"),
            "phase": h.get("phase"),
            "question": h.get("question"),
            "skill_tag": h.get("skill_tag"),
            "source_focus": h.get("source_focus"),
            "difficulty": h.get("difficulty"),
            "answer": h.get("answer"),
            "evaluation": h.get("evaluation"),
        }
        for h in history
    ]


# ── Token usage extractor ──────────────────────────────────────────────────────

def _extract_token_usage(ai_message) -> dict:
    usage = getattr(ai_message, "usage_metadata", None) or {}
    token_usage = (getattr(ai_message, "response_metadata", None) or {}).get("token_usage", {}) or {}

    input_tokens = usage.get("input_tokens") or token_usage.get("prompt_tokens") or 0
    output_tokens = usage.get("output_tokens") or token_usage.get("completion_tokens") or 0
    total_tokens = usage.get("total_tokens") or token_usage.get("total_tokens") or (input_tokens + output_tokens)

    return {
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "total_tokens": int(total_tokens),
    }


def _invoke_json(prompt: ChatPromptTemplate, payload: dict):
    messages = prompt.format_messages(**payload)
    ai_message = llm.invoke(messages)
    parser = JsonOutputParser()
    parsed = parser.parse(ai_message.content)
    return parsed, _extract_token_usage(ai_message)


# ═══════════════════════════════════════════════════════════════════════════════
# CHAIN 1 — Interview Plan
# ═══════════════════════════════════════════════════════════════════════════════

_plan_prompt = ChatPromptTemplate.from_messages([
    (
        "system",
        """You are a senior L1 technical interviewer at a product company.
Your job is to build a structured, three-phase interview plan from a job description, candidate resume, and years of experience.

This is a screening (L1) round — your goal is to assess baseline fit, communication clarity, and foundational competency.
Do NOT plan deep algorithm puzzles or system design pressure. Keep questions approachable.

Return ONLY valid JSON — no markdown, no prose outside the JSON object."""
    ),
    (
        "human",
        """Job Description:
{job_description}

Candidate Resume:
{resume_text}

Years of Experience: {years_of_experience}

Build a three-phase interview plan and return EXACTLY this JSON:
{{
  "interview_title": "Role-specific title, e.g. L1 Interview — Backend Engineer",

  "candidate_seniority": "entry_level" | "junior" | "mid_level" | "senior",

  "recommended_interview_time_minutes": <integer, 30–60>,

  "target_question_count": <integer, 8–16, see rules below>,

  "question_count_reasoning": "One sentence explaining the count, e.g. 'Mid-level role with 5 JD skills and 2 resume projects warrants 12 questions over 45 minutes.'",

  "communication_anchors": [
    "Specific prompt for opening warm-up, e.g. 'Ask candidate to walk through their most recent role'",
    "Second anchor, e.g. 'Ask why they are looking for a change'"
  ],

  "resume_projects": [
    "Specific project name or description from resume to probe, e.g. 'E-commerce platform built with Django + React'",
    "Another project or achievement worth discussing"
  ],

  "resume_claims_to_validate": [
    "Claim from resume that needs light validation, e.g. 'Led a team of 4 engineers'",
    "Another claim, e.g. 'Reduced API latency by 40%'"
  ],

  "resume_skills": ["skill1", "skill2"],

  "jd_skills": ["core skill from JD", "another core JD skill"],

  "jd_must_cover": [
    "Most important JD skill/area to assess, e.g. 'REST API design'",
    "Second most important, e.g. 'SQL query optimization'"
  ],

  "jd_nice_to_have": [
    "Nice-to-have JD skill, e.g. 'Docker/Kubernetes basics'"
  ],

  "summary": "2–3 sentence planning summary: candidate profile, phase strategy, and key watch-outs."
}}

Rules:
1. communication_anchors should be PROMPTS for the interviewer, not questions themselves. Keep them natural and warm.
2. resume_projects must be CONCRETE items lifted directly from the resume (project names, companies, achievements).
3. jd_must_cover should cover the top skills explicitly required in the JD — prioritise by criticality.
4. This is an L1 screening — flag anything too niche or senior-level to skip in must_cover.

Deciding target_question_count and recommended_interview_time_minutes (BOTH decided by you, no admin input):
  - These two must be consistent: assume ~3–4 minutes per question on average.
  - Base ranges by seniority:
      entry_level : 8–10 questions,  30–35 min
      junior      : 10–12 questions, 35–40 min
      mid_level   : 12–14 questions, 40–50 min
      senior      : 14–16 questions, 50–60 min
  - Adjust UP if: JD has 5+ distinct required skills, or resume has multiple complex projects to probe.
  - Adjust DOWN if: JD is narrow (1–2 core skills), resume is thin, or years_of_experience < 1.
  - Hard limits: target_question_count 8–16 inclusive, recommended_interview_time_minutes 30–60."""
    ),
])


def build_interview_plan_with_usage(
    job_description: str,
    resume_text: str,
    years_of_experience: float,
) -> tuple[dict, dict]:
    """
    Build the interview plan. The LLM decides both target_question_count
    and recommended_interview_time_minutes — no admin input required.
    """
    plan, usage = _invoke_json(
        _plan_prompt,
        {
            "job_description": job_description,
            "resume_text": resume_text,
            "years_of_experience": years_of_experience,
        },
    )
    return _normalize_plan_payload(plan, years_of_experience), usage


# ═══════════════════════════════════════════════════════════════════════════════
# CHAIN 2 — Initial Question (always Phase 1: Communication)
# ═══════════════════════════════════════════════════════════════════════════════

_initial_question_prompt = ChatPromptTemplate.from_messages([
    (
        "system",
        """You are a friendly, professional L1 technical interviewer opening a screening interview.

This is the very first question of the interview — Phase 1: Communication.
Your goal is to make the candidate feel comfortable and get them talking naturally.
Think of yourself as a warm, approachable colleague — not an interrogator.

Return ONLY valid JSON."""
    ),
    (
        "human",
        """Interview Plan:
{interview_plan}

Years of Experience: {years_of_experience}
Communication Anchors (use these as inspiration):
{communication_anchors}

Generate the opening question for this interview.

Rules:
1. Always start with a warm, open-ended question like "Tell me about yourself" or "Walk me through your background."
   Rephrase it to feel natural and specific to the candidate's profile if possible.
2. This is Phase 1 — NEVER ask anything technical here. No code, no algorithms, no system design.
3. The tone must be warm, conversational, and encouraging.
4. One question only — no multi-part questions.
5. Difficulty is always "easy" for communication phase.

Return EXACTLY this JSON:
{{
  "question": "The opening question text.",
  "phase": "communication",
  "type": "conversational",
  "difficulty": "easy",
  "requires_code_editor": false,
  "skill_tag": "introduction",
  "source_focus": "general",
  "followup_to_question_idx": null
}}"""
    ),
])


def generate_initial_question_with_usage(
    interview_plan: dict,
    years_of_experience: float,
) -> tuple[dict, dict]:
    question, usage = _invoke_json(
        _initial_question_prompt,
        {
            "interview_plan": json.dumps(interview_plan, ensure_ascii=True),
            "years_of_experience": years_of_experience,
            "communication_anchors": json.dumps(
                interview_plan.get("communication_anchors", []), ensure_ascii=True
            ),
        },
    )
    return _normalize_question_item(question), usage


# ═══════════════════════════════════════════════════════════════════════════════
# CHAIN 3 — Next Question (phase-aware, coverage-driven adaptive)
# ═══════════════════════════════════════════════════════════════════════════════

# ── Coverage tracker ──────────────────────────────────────────────────────────

def build_coverage_tracker(history: list[dict], interview_plan: dict) -> dict:
    """
    Scan history to compute what has been covered per phase and derive
    an explicit adaptive directive for the next question.

    Returns a dict with:
      - jd_coverage    : {skill: {"asked": int, "avg_score": float, "last_quality": str}}
      - resume_coverage: {project_or_claim: {"asked": int, "avg_score": float}}
      - jd_pending     : list of must_cover skills not yet asked (or asked only weakly)
      - resume_pending : list of projects/claims not yet probed
      - current_topic  : skill_tag of the last question answered
      - adaptive_directive: one of "DEEPEN" | "CLARIFY" | "PIVOT" | "NEXT_TOPIC"
      - directive_reason  : short human-readable explanation
    """

    # Index history by phase
    phase_history: dict[str, list[dict]] = {"communication": [], "resume": [], "technical": []}
    for h in history:
        phase = h.get("phase", "technical")
        phase_history.setdefault(phase, []).append(h)

    # ── JD coverage ───────────────────────────────────────────────────────────
    jd_must_cover: list[str] = interview_plan.get("jd_must_cover", [])
    jd_coverage: dict[str, dict] = {}

    for h in phase_history.get("technical", []):
        skill = (h.get("skill_tag") or "").strip().lower()
        if not skill:
            continue
        ev = h.get("evaluation") or {}
        score = ev.get("score", 0) or 0
        quality = ev.get("quality_bucket", "mixed")

        if skill not in jd_coverage:
            jd_coverage[skill] = {"asked": 0, "scores": [], "last_quality": quality}
        jd_coverage[skill]["asked"] += 1
        jd_coverage[skill]["scores"].append(score)
        jd_coverage[skill]["last_quality"] = quality

    # Compute avg scores and determine which must_cover items are truly covered
    jd_coverage_final: dict[str, dict] = {}
    for skill, data in jd_coverage.items():
        avg = round(sum(data["scores"]) / len(data["scores"]), 2) if data["scores"] else 0
        jd_coverage_final[skill] = {
            "asked": data["asked"],
            "avg_score": avg,
            "last_quality": data["last_quality"],
            # "exhausted" = asked at least once AND avg_score >= 4 (strong) OR asked >= 2 times (enough probing)
            "exhausted": (avg >= 4.0) or (data["asked"] >= 2),
        }

    # Pending must_cover = skills that haven't been covered or were weak
    jd_pending = [
        skill for skill in jd_must_cover
        if skill.lower() not in jd_coverage_final
        or not jd_coverage_final[skill.lower()]["exhausted"]
    ]

    # ── Resume coverage ───────────────────────────────────────────────────────
    resume_targets = (
        interview_plan.get("resume_projects", [])
        + interview_plan.get("resume_claims_to_validate", [])
    )
    resume_coverage: dict[str, dict] = {}

    for h in phase_history.get("resume", []):
        skill = (h.get("skill_tag") or h.get("source_focus") or "").strip().lower()
        ev = h.get("evaluation") or {}
        score = ev.get("score", 0) or 0
        if skill not in resume_coverage:
            resume_coverage[skill] = {"asked": 0, "scores": []}
        resume_coverage[skill]["asked"] += 1
        resume_coverage[skill]["scores"].append(score)

    resume_pending = [
        t for t in resume_targets
        if not any(
            word in " ".join(resume_coverage.keys())
            for word in t.lower().split()[:3]   # loose keyword match
        )
    ]

    # ── Adaptive directive ────────────────────────────────────────────────────
    # Computed purely in Python from the latest evaluation — NOT left to LLM judgment.
    latest = history[-1] if history else {}
    latest_eval = latest.get("evaluation") or {}
    recommended_followup = latest_eval.get("recommended_followup", "pivot")
    quality_bucket = latest_eval.get("quality_bucket", "mixed")
    current_topic = (latest.get("skill_tag") or "").strip().lower()
    current_phase_of_latest = latest.get("phase", "technical")

    directive = "PIVOT"
    directive_reason = "Default: move to next topic."

    if current_phase_of_latest == "technical":
        topic_data = jd_coverage_final.get(current_topic, {})
        topic_asked_count = topic_data.get("asked", 0)
        topic_exhausted = topic_data.get("exhausted", False)
        topic_in_must_cover = any(
            current_topic in skill.lower() or skill.lower() in current_topic
            for skill in jd_must_cover
        )

        if recommended_followup == "deepen" and quality_bucket == "strong":
            if not topic_exhausted and topic_in_must_cover:
                directive = "DEEPEN"
                directive_reason = (
                    f"Candidate answered '{current_topic}' strongly. "
                    f"It's a JD must-cover and hasn't been fully probed yet ({topic_asked_count} question(s) so far). "
                    "Go one level deeper on the same topic."
                )
            else:
                directive = "NEXT_TOPIC"
                directive_reason = (
                    f"Candidate answered '{current_topic}' strongly but the topic is either exhausted "
                    f"or not a JD priority. Move to the next pending must-cover skill."
                )

        elif recommended_followup == "clarify" and quality_bucket == "mixed":
            if topic_asked_count < 2:
                directive = "CLARIFY"
                directive_reason = (
                    f"Candidate gave a mixed answer on '{current_topic}'. "
                    "Ask one gentle follow-up to clarify their understanding before moving on."
                )
            else:
                directive = "PIVOT"
                directive_reason = (
                    f"Already asked {topic_asked_count} question(s) on '{current_topic}'. "
                    "Don't over-probe a weak area — pivot to the next topic."
                )

        else:  # weak / pivot
            directive = "PIVOT"
            directive_reason = (
                f"Candidate answered '{current_topic}' weakly or evaluation recommends pivot. "
                "Move to the next pending must-cover skill without pressing the weak area."
            )

    elif current_phase_of_latest == "resume":
        if recommended_followup == "deepen" and quality_bucket == "strong":
            directive = "DEEPEN"
            directive_reason = (
                "Candidate showed strong ownership. Go deeper into the same project/claim "
                "— ask about a specific challenge, decision, or outcome."
            )
        elif recommended_followup == "clarify":
            directive = "CLARIFY"
            directive_reason = "Candidate's answer was vague. Ask one specific follow-up to validate ownership."
        else:
            directive = "NEXT_TOPIC"
            directive_reason = "Move to the next unprobed resume project or claim."

    return {
        "jd_coverage": jd_coverage_final,
        "resume_coverage": resume_coverage,
        "jd_pending": jd_pending,
        "resume_pending": resume_pending,
        "current_topic": current_topic,
        "adaptive_directive": directive,
        "directive_reason": directive_reason,
    }


# ── Phase instruction templates ───────────────────────────────────────────────

def _build_phase_instructions(phase: str, coverage: dict) -> str:
    """Build dynamic phase instructions that embed the coverage state and directive."""

    directive = coverage["adaptive_directive"]
    reason = coverage["directive_reason"]
    jd_pending = coverage["jd_pending"]
    resume_pending = coverage["resume_pending"]
    current_topic = coverage["current_topic"]

    if phase == "communication":
        return (
            "CURRENT PHASE: Communication (Phase 1 — Warm-up)\n"
            "Goal: Keep the candidate comfortable. Ask about their background, career journey, "
            "motivations, or a high-level view of a project/role from their resume.\n"
            "Tone: Warm, conversational, encouraging. No pressure.\n"
            "NEVER ask anything technically deep or code-related in this phase.\n"
            "Good topics: career transitions, why they're looking for a change, team experience, a project highlight.\n"
            "Type must be 'conversational'. requires_code_editor must be false. Difficulty must be 'easy'."
        )

    if phase == "resume":
        pending_str = "\n  - ".join(resume_pending) if resume_pending else "(all probed)"
        return (
            "CURRENT PHASE: Resume Deep-Dive (Phase 2)\n"
            "Goal: Probe specific projects, decisions, and claims from the candidate's resume.\n"
            "Tone: Curious and collaborative — like a colleague asking 'how did you approach that?'\n"
            "Type should be 'scenario' or 'conceptual'. NEVER ask to write code here.\n"
            "Difficulty: 'easy' or 'medium'. Keep it L1 — open-ended, not interrogation.\n"
            "NEVER invent resume details — use only resume_projects and resume_claims_to_validate from the plan.\n\n"
            f"Resume items not yet probed:\n  - {pending_str}\n\n"
            f"ADAPTIVE DIRECTIVE: {directive}\n"
            f"Reason: {reason}\n\n"
            "Directive rules:\n"
            "  DEEPEN   → Ask a deeper follow-up on the same project/claim (challenge, decision, outcome).\n"
            "  CLARIFY  → Ask one specific question to validate whether the candidate actually owned the work.\n"
            "  NEXT_TOPIC → Move to the first item in the 'Resume items not yet probed' list above.\n"
            "  PIVOT    → Move to the first item in the 'Resume items not yet probed' list above.\n"
            f"Currently active topic: '{current_topic}'"
        )

    # technical
    pending_str = "\n  - ".join(jd_pending) if jd_pending else "(all must-cover topics addressed)"
    covered_str = (
        "\n".join(
            f"  - {skill}: avg_score={data['avg_score']}, asked={data['asked']}x, quality={data['last_quality']}"
            for skill, data in coverage["jd_coverage"].items()
        )
        or "  (none yet)"
    )
    return (
        "CURRENT PHASE: Technical Assessment (Phase 3 — JD-aligned)\n"
        "Goal: Assess foundational technical competency for the specific role.\n"
        "Tone: Professional but fair. This is L1 screening — test understanding, not perfection.\n"
        "L1 leniency rules:\n"
        "  - Test fundamentals, not edge cases or advanced internals.\n"
        "  - Prefer conceptual/scenario questions over 'implement from scratch' demands.\n"
        "  - 'practical' (code) questions allowed, but keep to 5–10 lines max.\n"
        "  - Difficulty target: 'medium'. Use 'easy' if candidate is struggling; 'hard' only if clearly strong.\n\n"
        f"JD must-cover topics NOT yet addressed (priority order):\n  - {pending_str}\n\n"
        f"Topics already covered:\n{covered_str}\n\n"
        f"ADAPTIVE DIRECTIVE: {directive}\n"
        f"Reason: {reason}\n\n"
        "Directive decision rules (follow exactly):\n"
        "  DEEPEN    → The candidate answered the last topic strongly AND it's a JD priority not fully probed.\n"
        "              Ask ONE question that goes one layer deeper on the SAME skill/topic.\n"
        "              Example: if they explained 'what is an index', now ask 'when would you choose a composite index?'\n\n"
        "  CLARIFY   → The candidate gave a mixed/partial answer. Ask ONE gentle follow-up to close the gap.\n"
        "              Keep it simple — rephrase or zoom into one specific aspect they missed.\n"
        "              Do NOT ask a completely different hard question.\n\n"
        "  NEXT_TOPIC → The last topic is exhausted (strong answer, asked enough) or wasn't a JD priority.\n"
        "              Pick the FIRST topic from 'JD must-cover topics NOT yet addressed' and ask about it.\n\n"
        "  PIVOT     → The candidate answered weakly. Do NOT press the same topic again.\n"
        "              Pick the FIRST topic from 'JD must-cover topics NOT yet addressed' and ask about it.\n"
        "              Start fresh — no reference to the weak previous answer.\n\n"
        f"Currently active topic (last answered): '{current_topic}'"
    )


_next_question_prompt = ChatPromptTemplate.from_messages([
    (
        "system",
        """You are a senior L1 technical interviewer conducting an adaptive screening interview.
You receive the interview plan, full conversation history, coverage analysis, adaptive directive, and the latest evaluation.
Your ONLY job: generate exactly ONE next question that follows the adaptive directive precisely.

Return ONLY valid JSON. No markdown, no prose outside JSON."""
    ),
    (
        "human",
        """Interview Plan:
{interview_plan}

Phase Budget:
{phase_budget}

Current Phase: {current_phase}
Question Index Just Answered (0-based): {question_idx}
Questions Remaining (including this one): {questions_remaining}

━━━ ADAPTIVE CONTEXT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{phase_instructions}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Full Interview History:
{history}

Latest Answer Evaluation:
{latest_evaluation}

━━━ GENERATION RULES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Follow the ADAPTIVE DIRECTIVE above — it is the primary instruction. Do not override it.
2. Never repeat a question from history (check skill_tag AND question text).
3. One question only. Maximum two sentences. No multi-part questions.
4. If questions_remaining == 1: make it a high-value closing question on the most important uncovered area.
5. requires_code_editor must be true ONLY for 'practical' type questions.
6. This is L1 — never ask obscure edge cases, trick questions, or senior-level system design.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Return EXACTLY this JSON:
{{
  "question": "The full question text.",
  "phase": "{current_phase}",
  "type": "conversational" | "conceptual" | "scenario" | "practical",
  "difficulty": "easy" | "medium" | "hard",
  "requires_code_editor": true | false,
  "skill_tag": "short label for the specific skill or topic (e.g. 'SQL indexing', 'Django ORM', 'Django REST project')",
  "source_focus": "general" | "resume" | "jd" | "shared",
  "followup_to_question_idx": <0-based integer if this deepens/clarifies a prior question, else null>,
  "directive_applied": "DEEPEN" | "CLARIFY" | "NEXT_TOPIC" | "PIVOT"
}}"""
    ),
])


def generate_next_question_with_usage(
    interview_plan: dict,
    years_of_experience: float,
    num_questions: int,
    question_idx: int,
    history: list[dict],
    latest_evaluation: dict,
) -> tuple[dict, dict]:
    budget = interview_plan.get("phase_budget") or compute_phase_budget(num_questions)
    next_idx = question_idx + 1
    current_phase = get_current_phase(next_idx, budget)
    questions_remaining = num_questions - next_idx

    # Build coverage tracker — this is the engine of adaptivity
    coverage = build_coverage_tracker(history, interview_plan)

    # Build dynamic phase instructions that embed coverage state + directive
    phase_instructions = _build_phase_instructions(current_phase, coverage)

    question, usage = _invoke_json(
        _next_question_prompt,
        {
            "interview_plan": json.dumps(interview_plan, ensure_ascii=True),
            "phase_budget": json.dumps(budget, ensure_ascii=True),
            "current_phase": current_phase,
            "question_idx": question_idx,
            "questions_remaining": questions_remaining,
            "phase_instructions": phase_instructions,
            "history": json.dumps(_serialize_history(history), ensure_ascii=True),
            "latest_evaluation": json.dumps(latest_evaluation, ensure_ascii=True),
        },
    )
    return _normalize_question_item(question, fallback_idx=question_idx), usage


# ═══════════════════════════════════════════════════════════════════════════════
# CHAIN 4 — Answer Evaluation (phase-aware, per-dimension scoring)
# ═══════════════════════════════════════════════════════════════════════════════

_EVALUATION_RUBRIC = {
    "communication": {
        "description": "Communication & Culture Fit",
        "what_to_assess": (
            "Clarity of expression, structure of storytelling, confidence, conciseness, "
            "and whether the answer gives a genuine sense of the candidate's background and personality."
        ),
        "scoring_guide": """
  5 — Excellent: Clear, well-structured, confident, and engaging. Feels natural and specific.
  4 — Good: Mostly clear with minor gaps. Gets the point across well.
  3 — Fair: Somewhat vague or rambling, but communicates the core idea.
  2 — Weak: Hard to follow, very brief with no substance, or off-topic.
  1 — Poor: Incoherent, completely off-topic, or candidate declined to answer.
""",
        "leniency_note": "Be generous — communication nerves are normal in interviews. If the core intent is clear, score 3 or above.",
    },
    "resume": {
        "description": "Resume / Project Knowledge",
        "what_to_assess": (
            "Whether the candidate has genuine ownership and understanding of the projects/roles on their resume. "
            "Look for: specific details, real challenges, decision-making, and lessons learned. "
            "Red flags: vague answers that could describe any project, or inability to recall specifics."
        ),
        "scoring_guide": """
  5 — Excellent: Specific, detailed, demonstrates clear ownership. Mentions challenges and outcomes.
  4 — Good: Solid recall with minor gaps. Shows genuine involvement.
  3 — Fair: Describes the project at a high level but lacks specifics or depth.
  2 — Weak: Very vague or contradicts resume. Limited ownership evident.
  1 — Poor: Could not answer, invented details, or the claimed work appears fabricated.
""",
        "leniency_note": "Resume questions at L1 are conversational — if the candidate shows genuine involvement even without perfect recall, lean toward 3–4.",
    },
    "technical": {
        "description": "Technical Competency (JD-aligned)",
        "what_to_assess": (
            "Foundational understanding of the concept asked. Look for: correct core idea, "
            "awareness of trade-offs, and practical applicability. "
            "This is L1 — penalise factual errors but don't penalise missing advanced nuances."
        ),
        "scoring_guide": """
  5 — Excellent: Correct, complete, explains trade-offs or practical implications.
  4 — Good: Core concept correct, minor gaps in depth or edge cases.
  3 — Fair: Core idea present but incomplete or slightly imprecise.
  2 — Weak: Partially correct but significant misunderstanding or gaps.
  1 — Poor: Incorrect, no meaningful answer, or completely off-topic.
""",
        "leniency_note": "L1 screening — penalise clear misconceptions, but don't penalise missing senior-level nuances. If the fundamentals are right, score 3 or above.",
    },
}


_evaluation_prompt = ChatPromptTemplate.from_messages([
    (
        "system",
        """You are a fair-minded L1 technical interviewer evaluating a candidate's answer.
You evaluate based on the question's phase — communication, resume, or technical — with phase-appropriate criteria.
This is a screening round. Be strict about factual errors but lenient about depth gaps.
Return ONLY valid JSON. No markdown, no explanation outside the JSON."""
    ),

    # ── Few-shot: Communication phase (score 4) ───────────────────────────────
    (
        "human",
        """Phase: communication
Dimension: Communication & Culture Fit
Question: Tell me about yourself and what brought you to software engineering.
Candidate Answer: I have been working as a backend developer for about three years now. I started in a small startup where I wore many hats, mostly Python and Django, and then moved to a product company where I got to focus more on APIs and data pipelines. I enjoy the problem-solving aspect and building things that actually get used by real people. That's what keeps me motivated."""
    ),
    (
        "assistant",
        "{{\"score\": 4, \"verdict\": \"Good\", \"dimension\": \"Communication & Culture Fit\", "
        "\"overview\": \"The candidate gave a clear, genuine, and structured answer. They covered their trajectory, tech stack, and motivation. Slightly brief but well-articulated.\", "
        "\"positives\": [\"clear career timeline\", \"specific technologies mentioned\", \"authentic motivation shared\"], "
        "\"gaps\": [\"could have mentioned a specific achievement or impact\"], "
        "\"improvement_tip\": \"Adding one concrete achievement would make the answer more memorable.\", "
        "\"quality_bucket\": \"strong\", "
        "\"recommended_followup\": \"deepen\""
    ),

    # ── Few-shot: Resume phase (score 3) ──────────────────────────────────────
    (
        "human",
        """Phase: resume
Dimension: Resume / Project Knowledge
Question: You mentioned building a data pipeline at your last role — what was the most challenging part of that project?
Candidate Answer: Yeah it was a pretty complex project. We were ingesting data from multiple sources and had to make sure everything was consistent. It was challenging to keep up with the scale."""
    ),
    (
        "assistant",
        "{{\"score\": 3, \"verdict\": \"Fair\", \"dimension\": \"Resume / Project Knowledge\", "
        "\"overview\": \"The candidate confirms involvement and touches on real challenges (multi-source ingestion, consistency, scale) but stays at a very high level. No specifics about tools, decisions, or how they personally resolved the challenges.\", "
        "\"positives\": [\"correctly identifies multi-source ingestion as a challenge\", \"mentions consistency and scale — relevant concerns\"], "
        "\"gaps\": [\"no specific tools or technologies named\", \"no personal decision or contribution highlighted\", \"no outcome or resolution described\"], "
        "\"improvement_tip\": \"Describe one specific decision you made — e.g. which tool you chose and why — to show genuine ownership.\", "
        "\"quality_bucket\": \"mixed\", "
        "\"recommended_followup\": \"clarify\""
    ),

    # ── Few-shot: Technical phase (score 5) ───────────────────────────────────
    (
        "human",
        """Phase: technical
Dimension: Technical Competency (JD-aligned)
Question: What is the difference between SQL indexes and when would you choose a composite index over a single-column index?
Candidate Answer: An index speeds up data retrieval by creating a lookup structure so the database doesn't do a full table scan. A single-column index works well when you frequently filter or sort by one column. A composite index covers multiple columns and is useful when your queries filter on a specific combination of columns — like WHERE status = 'active' AND created_at > '2024-01-01'. The order of columns in a composite index matters — the leftmost column should be the most selective or most frequently filtered. The trade-off is write overhead and storage since each index needs to be maintained."""
    ),
(
        "assistant",
        "{{\"score\": 5, \"verdict\": \"Excellent\", \"dimension\": \"Technical Competency (JD-aligned)\", "
        "\"overview\": \"Complete and accurate answer. The candidate correctly explained indexes, gave a practical composite index use case, mentioned column ordering, and acknowledged the trade-off. L1-appropriate depth.\", "
        "\"positives\": [\"correct explanation of index purpose\", \"practical composite index use case with real SQL example\", \"column order awareness\", \"trade-off acknowledged (writes + storage)\"], "
        "\"gaps\": [], "
        "\"improvement_tip\": \"No improvements needed for this level of question.\", "
        "\"quality_bucket\": \"strong\", "
        "\"recommended_followup\": \"deepen\""
    ),

    # ── Actual evaluation request ─────────────────────────────────────────────
    (
        "human",
        """Phase: {phase}
Dimension: {dimension}
What to assess: {what_to_assess}

Scoring Guide:
{scoring_guide}

Leniency Note: {leniency_note}

Question: {question}
Skill/Topic: {topic}
Candidate Answer: {answer}

Evaluate the answer using the scoring guide and phase-appropriate criteria.

Return EXACTLY this JSON:
{{
  "score": <1 to 5>,
  "verdict": "Excellent" | "Good" | "Fair" | "Poor",
  "dimension": "{dimension}",
  "overview": "2-3 sentence honest assessment aligned to the phase criteria.",
  "positives": ["specific thing the candidate did well", "..."],
  "gaps": ["specific thing missing or incorrect", "..."],
  "improvement_tip": "One actionable sentence for the candidate.",
  "quality_bucket": "strong" | "mixed" | "weak",
  "recommended_followup": "deepen" | "clarify" | "pivot"
}}

Score-to-Verdict mapping (MUST follow exactly):
  5 → "Excellent"
  4 → "Good"
  3 → "Fair"
  1 or 2 → "Poor"

Rules:
1. Score must match verdict exactly per the mapping.
2. Apply the leniency note — this is a screening round, not a senior panel.
3. An empty or completely off-topic answer always scores 1.
4. Do NOT penalise nervousness or informal phrasing in communication phase answers.
5. In technical phase, penalise factual errors but not missing advanced nuance.
6. Keep feedback specific to THIS question — no generic advice."""
    ),
])


def evaluate_answer_with_usage(
    phase: str,
    topic: str,
    question: str,
    answer: str,
) -> tuple[dict, dict]:
    rubric = _EVALUATION_RUBRIC.get(phase, _EVALUATION_RUBRIC["technical"])
    normalized_answer = answer.strip() if answer.strip() else "The candidate did not provide an answer."

    result, usage = _invoke_json(
        _evaluation_prompt,
        {
            "phase": phase,
            "dimension": rubric["description"],
            "what_to_assess": rubric["what_to_assess"],
            "scoring_guide": rubric["scoring_guide"],
            "leniency_note": rubric["leniency_note"],
            "topic": topic,
            "question": question,
            "answer": normalized_answer,
        },
    )

    # Normalise output
    result["phase"] = phase
    result["dimension"] = rubric["description"]
    result["positives"] = [str(x).strip() for x in result.get("positives", []) if str(x).strip()]
    result["gaps"] = [str(x).strip() for x in result.get("gaps", []) if str(x).strip()]
    result["quality_bucket"] = str(result.get("quality_bucket", "mixed")).strip() or "mixed"
    result["recommended_followup"] = str(result.get("recommended_followup", "pivot")).strip() or "pivot"
    # Ensure all required fields for EvaluationResponse are present and well-typed.
    # Apply conservative defaults if LLM omitted any fields to avoid response validation errors.
    try:
        result["score"] = int(result.get("score", 1))
    except (ValueError, TypeError):
        result["score"] = 1

    # Map score to verdict if verdict missing or inconsistent
    verdict = result.get("verdict")
    if not isinstance(verdict, str) or not verdict:
        if result["score"] >= 5:
            result["verdict"] = "Excellent"
        elif result["score"] >= 4:
            result["verdict"] = "Good"
        elif result["score"] == 3:
            result["verdict"] = "Fair"
        else:
            result["verdict"] = "Poor"

    result["overview"] = str(result.get("overview", "No assessment provided.")).strip()
    result["improvement_tip"] = str(result.get("improvement_tip", "")).strip()

    # Ensure lists are present
    result["positives"] = result.get("positives", []) if isinstance(result.get("positives", []), list) else []
    result["gaps"] = result.get("gaps", []) if isinstance(result.get("gaps", []), list) else []

    # Final defensive coercions for strings
    for k in ("phase", "dimension", "quality_bucket", "recommended_followup", "verdict"):
        if k in result:
            result[k] = str(result[k])

    return result, usage


# ═══════════════════════════════════════════════════════════════════════════════
# CHAIN 5 — Final Report (aggregate evaluation across all phases)
# ═══════════════════════════════════════════════════════════════════════════════

_final_report_prompt = ChatPromptTemplate.from_messages([
    (
        "system",
        """You are a senior L1 interviewer writing the post-interview assessment for a hiring manager.
Your report is honest, structured, and actionable.
This is a screening round — the bar is baseline fit and foundational competency, not perfection.
Return ONLY valid JSON."""
    ),
    (
        "human",
        """Interview Plan:
{interview_plan}

Full Interview History (questions, answers, per-question evaluations):
{history}

Phase-wise average scores (pre-computed):
  Communication: {comm_avg} / 5
  Resume:        {resume_avg} / 5
  Technical:     {tech_avg} / 5

Generate the final interview report.

Return EXACTLY this JSON:
{{
  "overall_score": <float, weighted average: comm*0.20 + resume*0.40 + tech*0.40, rounded to 1 decimal>,
  "overall_verdict": "Strong Hire" | "Hire" | "Borderline" | "No Hire",
  "recommendation": "2–3 sentence hiring recommendation with specific reasoning.",

  "dimension_scores": {{
    "communication": {{
      "score": <float, 1 to 5>,
      "summary": "1-2 sentence summary of communication performance."
    }},
    "resume": {{
      "score": <float, 1 to 5>,
      "summary": "1-2 sentence summary of resume/project knowledge."
    }},
    "technical": {{
      "score": <float, 1 to 5>,
      "summary": "1-2 sentence summary of technical performance."
    }}
  }},

  "strengths": ["Concrete strength observed, e.g. 'Strong ownership of Django API project'"],
  "concerns": ["Specific concern, e.g. 'Vague on SQL optimisation fundamentals'"],
  "suggested_l2_focus_areas": ["Area to probe deeper in L2, e.g. 'System design basics'"],

  "overall_summary": "3–5 sentence candidate profile summary for the hiring manager."
}}

Verdict thresholds (based on overall_score):
  >= 4.2 → "Strong Hire"
  >= 3.5 → "Hire"
  >= 2.8 → "Borderline"
  <  2.8 → "No Hire"

Rules:
1. Be honest — if technical performance was weak, say so clearly but respectfully.
2. Be specific — cite actual questions and answers, not generic statements.
3. L1 context — "Borderline" should be recommended for L2 if communication and attitude were positive.
4. suggested_l2_focus_areas should help the next interviewer, not repeat L1 findings."""
    ),
])


def generate_final_report_with_usage(
    interview_plan: dict,
    history: list[dict],
) -> tuple[dict, dict]:
    def _avg(phase_name: str) -> float:
        scores = [
            h["evaluation"]["score"]
            for h in history
            if h.get("phase") == phase_name and isinstance(h.get("evaluation"), dict) and "score" in h["evaluation"]
        ]
        return round(sum(scores) / len(scores), 2) if scores else 0.0

    comm_avg = _avg("communication")
    resume_avg = _avg("resume")
    tech_avg = _avg("technical")

    report, usage = _invoke_json(
        _final_report_prompt,
        {
            "interview_plan": json.dumps(interview_plan, ensure_ascii=True),
            "history": json.dumps(_serialize_history(history), ensure_ascii=True),
            "comm_avg": comm_avg,
            "resume_avg": resume_avg,
            "tech_avg": tech_avg,
        },
    )
    return report, usage