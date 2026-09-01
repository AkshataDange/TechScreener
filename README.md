# TechScreener — AI-Powered Adaptive L1 Interview Platform

TechScreener is an AI-driven platform that automates the first round of technical interviews. Admins upload a Job Description + the candidate's resume; the LLM then reads both and **designs an interview plan on the fly** — deciding how many questions to ask, how long the interview should run, and which topics to cover. Candidates take the interview in their browser using voice or text, with real-time transcription via WhisperLive. Every answer is evaluated by the LLM, the full session is video-recorded, and the admin gets a structured report with scores, transcripts, video playback, and proctoring flags.

---

## Table of Contents

1. [What It Does](#what-it-does)
2. [Tech Stack](#tech-stack)
3. [Project Structure](#project-structure)
4. [How It Works — The Big Picture](#how-it-works--the-big-picture)
5. [User Roles](#user-roles)
6. [Detailed Flow](#detailed-flow)
   - [Admin Flow](#1-admin-flow)
   - [Candidate Flow](#2-candidate-flow)
   - [Adaptive Question Loop](#3-adaptive-question-loop)
   - [Real-Time Voice Transcription (WhisperLive)](#4-real-time-voice-transcription-whisperlive)
   - [Video Recording](#5-video-recording)
   - [Proctoring Flow](#6-proctoring-flow)
   - [Report Review Flow](#7-report-review-flow)
7. [Backend Architecture](#backend-architecture)
8. [Frontend Pages](#frontend-pages)
9. [Database Schema](#database-schema)
10. [API Reference](#api-reference)
11. [Interview Phases](#interview-phases)
12. [Evaluation Scoring](#evaluation-scoring)
13. [Setup & Installation](#setup--installation)
14. [Environment Variables](#environment-variables)
15. [Typical Response Times](#typical-response-times)
16. [Troubleshooting](#troubleshooting)
17. [Production Checklist](#production-checklist)

---

## What It Does

- **Admins** create user accounts, upload a JD + the candidate's resume, and let the AI design the interview plan. They review reports (with full video playback) afterwards.
- **Adaptive AI** reads the JD, resume, and years of experience, then decides the number of questions (8–16), interview duration (30–60 min), and a 3-phase question budget. Each subsequent question is generated *after* the previous answer is evaluated, so the interview adapts to how the candidate is performing.
- **Candidates** log in, see their assigned interview, and take it fully in the browser — answering by voice (streamed live to WhisperLive for transcription) or by typing for code questions.
- **The full interview is video-recorded** in the browser (webcam + screen), uploaded in chunks, and stitched together server-side with ffmpeg.
- **Proctoring** logs tab switches, copy-paste, devtools events, etc.
- **Approved candidates** from the upstream Resume Parser system can be auto-imported — TechScreener creates the account, generates a password, builds the interview plan, and emails the candidate their credentials.
- **Reports** include average score, per-question breakdowns, full transcripts, the LLM's final summary, the recorded video, and all proctoring flags.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend Framework | FastAPI (Python) |
| Database | PostgreSQL (`psycopg2`, JSONB columns, no ORM) |
| LLM Provider | Groq API (Llama 3.3 70B) via LangChain |
| Speech-to-Text (streaming) | **WhisperLive** (external WebSocket server, proxied) |
| Speech-to-Text (file upload fallback) | `faster-whisper` (local) |
| Text-to-Speech | Piper TTS (offline, `en_US-lessac-medium`) |
| Resume Parsing | `pypdf` (PDF), `python-docx` (DOCX) |
| Video Stitching | `ffmpeg` (concat demuxer with WebM fallback re-encode) |
| Email | SMTP (Gmail-compatible) for credential delivery |
| Frontend | Vanilla HTML, CSS, JavaScript |
| Auth | Custom bearer-token auth (PBKDF2-SHA256, 7-day TTL) |

---

## Project Structure

```
TechScreener/
├── backend/
│   ├── main.py                       ← FastAPI app, lifespan, routes, static mount
│   ├── config.py                     ← Settings from .env (Groq, DB, WhisperLive, SMTP)
│   ├── database.py                   ← All PostgreSQL operations + migrations
│   ├── schemas.py                    ← Pydantic request/response models
│   ├── auth_service.py               ← Token auth, password hashing, role guards
│   ├── recover_assignments.py        ← Utility to repair orphaned assignments
│   ├── requirements.txt
│   ├── routers/
│   │   ├── auth.py                   ← /auth/*   — register & login
│   │   ├── user.py                   ← /user/*   — candidate-facing assignment flow
│   │   ├── admin.py                  ← /admin/*  — admin management + approved-candidates
│   │   ├── interview.py              ← /interview/* — adaptive interview engine (session,
│   │   │                                  next-question, answer, voice-answer, video,
│   │   │                                  WebSocket transcription, final report)
│   │   └── review.py                 ← /review/* — legacy review endpoints
│   └── services/
│       ├── ai_service.py             ← LLM: plan-building, adaptive Q-gen, evaluation, final report
│       ├── stt_service.py            ← faster-whisper (used for uploaded WAV files)
│       ├── streaming_stt_service.py  ← WhisperLive WebSocket proxy
│       └── tts_service.py            ← Piper TTS wrapper
│
├── frontend/                          ← Vanilla HTML/CSS/JS pages
│   ├── user.html
│   ├── index.html
│   ├── admin.html
│   ├── admin-users.html
│   ├── admin-assign.html
│   ├── admin-reports.html
│   ├── reviewer.html
│   ├── admin-common.js
│   └── admin-shared.css
│
└── videos/                            ← Recorded interview WebM files
                                        (parts uploaded as <session>_part<N>.webm,
                                         finalized to <session>.webm via ffmpeg)
```

---

## How It Works — The Big Picture

```
Admin uploads JD + Resume
         │
         ▼
LLM builds interview plan       ─── decides count, duration, 3 phases, focus areas
         │
         ▼
Candidate logs in & starts
         │
         ▼
Loop, one question at a time:
   • Frontend plays question via Piper TTS
   • Candidate answers (voice → WhisperLive streamed STT, or text)
   • LLM evaluates the answer
   • LLM generates the *next* question based on full history
         │
         ▼
After last question → LLM writes final report (summary, strengths, gaps, hire/no-hire signal)
         │
         ▼
Admin reviews report + video + per-question breakdown + proctoring flags
```

Everything is adaptive: the LLM sees prior questions/answers/evaluations before generating each new question, so the interview drifts toward areas where the candidate needs probing.

---

## User Roles

**Admin**
- Created automatically on first startup from credentials in `.env` (see `auth_service.py → ensure_default_admin`).
- Manages user accounts, assigns interviews (JD + resume), reviews reports.
- Lands on `/admin`.

**User (Candidate)**
- Created either via `/auth/register`, by an admin in the Users page, or auto-created from an *approved candidate* record (Resume Parser integration).
- Sees and takes only their own assignments.
- Lands on `/user`.

---

## Detailed Flow

### 1. Admin Flow

**Step 1 — Add a Candidate**
Candidates are created in one of two ways:
- **Self-registration** — the candidate signs up via `POST /auth/register` and the admin then assigns them an interview.
- **Approved-candidates flow (Resume Parser integration)** — the admin reviews approved records at `GET /admin/approved-candidates`, fills in any missing email via `PATCH /admin/approved-candidates/{id}/email`, then calls `POST /admin/approved-candidates/{id}/assign` to auto-create the user, build an interview plan, and email the credentials.

The Users page (`/admin/users-page`) renders the current users + their assignments via `GET /admin/users-with-assignments` (read-only view).

**Step 2 — Assign an Interview**
On the Assign page (`/admin/assign`), the admin:
- Picks a user
- Uploads or pastes the **job description**
- Uploads the candidate's **resume** (PDF, DOCX, or TXT — parsed server-side via `POST /admin/parse-resume`)
- Enters **years of experience**

The admin clicks Assign and `POST /admin/assignments` is called. The backend immediately invokes `build_interview_plan_with_usage()` in `ai_service.py`. The LLM returns:
- `interview_title`
- `candidate_seniority`
- `target_question_count` (8–16)
- `recommended_interview_time_minutes` (30–60)
- `phase_budget` — how questions are split between Communication / Resume / Technical
- `question_count_reasoning` — text explaining why this size

The plan is stored on the assignment row. No questions are generated yet — that happens when the candidate starts.

**Step 3 — Review**
The Reports page (`/admin/reports-page`) lists every completed interview. Clicking one calls `GET /admin/reports/{assignment_id}` which returns the assignment, candidate info, session, all answers, all flags, and a `video_available` flag. The detail view (`reviewer.html`) renders the structured report and embeds the recorded WebM via `GET /interview/video/{session_id}`.

---

### 2. Candidate Flow

**Step 1 — Login**
The candidate visits `/user`, logs in. The backend returns a 7-day bearer token.

**Step 2 — See Assignments**
`GET /user/assignments` returns their interview list with the LLM-decided title, target question count, recommended duration, and status.

**Step 3 — Start Interview**
Clicking Start calls `POST /user/assignments/{id}/start`. This is the entry point that:
1. Reuses the plan stored on the assignment (or rebuilds it if absent).
2. Generates the **opening question** via `generate_initial_question_with_usage()` — always a Phase 1 (Communication) question.
3. Creates a `sessions` row with the plan, the first question, an empty `history`, and links it to the assignment.
4. Marks the assignment `in_progress`.

If the candidate refreshes mid-interview, hitting Start again returns the same session and resumes from the current unanswered question (`question_idx = len(history)`).

The candidate is taken to `index.html` with the session ID.

---

### 3. Adaptive Question Loop

The interview page (`index.html`) drives this loop until `len(history) >= target_question_count`.

**For each question:**

1. **Question is spoken** — the frontend calls `GET /interview/speak-question?session_id=...&question_idx=...`. Piper TTS converts the question text to WAV and streams it back.

2. **Candidate answers:**

   - **Voice (default)** — the browser opens a WebSocket to `/interview/ws/transcribe/{session_id}`. Float32 PCM audio @ 16 kHz is streamed live to the WhisperLive proxy (`services/streaming_stt_service.py`), which forwards it to an external WhisperLive server. Live partial transcripts are streamed back to the UI. When the user clicks Stop, the proxy returns a final transcript via `{"type":"done","final_transcript":"..."}`.

   - **Voice (file upload fallback)** — `POST /interview/voice-answer` accepts a WAV upload, runs it through the local `faster-whisper` model (`stt_service.py`), and proceeds as a text answer.

   - **Text / Code** — for questions where `phase == "technical"` (the practical ones), the candidate types into the code editor and `POST /interview/answer` is called directly.

3. **Adaptive pipeline** (`routers/interview.py → _process_answer`):
   - **Evaluate** the answer with the phase-appropriate rubric (`evaluate_answer_with_usage`).
   - **Append** the evaluation to `session.history` and persist the answer row.
   - If `len(history) >= target_question_count` → **interview complete**: call `generate_final_report_with_usage(plan, history)` and store the report.
   - Otherwise → call `generate_next_question_with_usage(...)` passing the full history + latest evaluation, append the new question to `session.questions`, and return it to the frontend.

4. **Score is shown** — the per-question evaluation (score, verdict, feedback) is rendered. Candidate proceeds to the next question.

**Ending the interview:**
Triggered either by the last question being answered (final report auto-generated) or by the timer expiring. The frontend then calls `POST /user/assignments/{id}/complete?status=completed|expired`.

---

### 4. Real-Time Voice Transcription (WhisperLive)

TechScreener runs an external **WhisperLive** WebSocket server (typically on `ws://127.0.0.1:8080`) for low-latency live transcription. The browser does not talk to WhisperLive directly — it talks to the FastAPI app, which proxies through `services/streaming_stt_service.py`.

**Why a proxy?**
- Translates the browser-facing handshake/segment protocol into WhisperLive's binary-PCM-plus-`END_OF_AUDIO`-sentinel protocol.
- Hides the WhisperLive URL/model/VAD settings from the client.
- Picks the longest transcript seen during the session as a safety net (WhisperLive's cleanup can occasionally drop the last update on close).

**Configurable** via `WHISPERLIVE_WS_URL`, `WHISPERLIVE_MODEL`, `WHISPERLIVE_LANGUAGE`, `WHISPERLIVE_USE_VAD` in `.env`.

The standalone `services/stt_service.py` (using `faster-whisper` locally) is still used for the file-upload `/interview/voice-answer` route, which exists as a fallback when WebSockets aren't viable.

---

### 5. Video Recording

The interview is video-recorded in the browser using MediaRecorder (webcam + optional screen share) and uploaded in chunks while the interview is happening:

- `POST /interview/video?session_id=...&chunk_index=...&part_index=...&is_final=...` — each chunk is appended to a part file (`<session>_part<N>.webm`). A new part starts whenever the browser splits the recording (e.g., on tab focus changes).
- `POST /interview/video/finalize/{session_id}` — concatenates all part files into the final `<session>.webm` using `ffmpeg -f concat -c copy`, with a re-encode fallback (`libvpx-vp9` / `libopus`) when the parts have mismatched codecs.
- `GET /interview/video/{session_id}` — serves the finalized WebM to the admin. If parts exist but finalize was never called (abandoned interviews, crashed sessions), it builds the final file lazily on first read.

Files live in `techscreen/videos/`. The DB stores only the filename (`<session>.webm`) on the `interview_videos` table — the bytes are on disk.

---

### 6. Proctoring Flow

Runs silently in parallel.

**Events** — tab switches, blurs, copy, paste, devtools, etc. fire `POST /interview/flag` (or `POST /user/interview/flag` for the assignment-scoped flow). Stored on `flags` table.

**Snapshots** — webcam-frame snapshot endpoints (`/interview/snapshot`, `/user/interview/snapshot`) exist on the routers. *Note:* the current `database.py` doesn't define a `snapshots` table; the snapshot endpoints will fail at runtime until that table is added back. The full video recording (above) covers the same need.

---

### 7. Report Review Flow

`GET /admin/reports/{assignment_id}` returns:
- The assignment row
- Candidate info
- The full session (plan, questions, history)
- All answers (with transcripts and per-question evaluations)
- All proctoring flags
- A computed `summary.avg_score` (mean of raw LLM scores × 2, so out of 10)
- A `video_available` boolean

The final LLM-generated report (overall verdict, strengths, gaps, hire/no-hire signal) lives in `session.report` and is also retrievable via `GET /interview/report/{session_id}`.

`reviewer.html` renders all of this — including the embedded video player and proctoring timeline.

---

## Backend Architecture

### Entry Point (`main.py`)

Defines the FastAPI app. The `lifespan` context manager runs `db.init_db()` (creates tables + runs additive migrations) and `ensure_default_admin()` on startup. CORS is `*` for dev. All five routers are registered (`interview`, `review`, `auth`, `user`, `admin`), and the frontend is mounted at `/static`. Page routes (`/`, `/user`, `/admin`, `/admin/users-page`, etc.) return the corresponding HTML files. `/health` returns `{"status":"ok"}`.

### Routers

**`routers/auth.py` — prefix `/auth`** — `POST /register`, `POST /login`, `GET /me`.

**`routers/user.py` — prefix `/user`** — candidate-facing assignment flow. Every endpoint requires `require_role("user")`.
Key endpoints: `/assignments`, `/assignments/{id}/start`, `/assignments/{id}/complete`, `/interview/session/{id}`, `/interview/speak-question`, `/interview/answer`, `/interview/voice-answer`, `/interview/flag`, `/interview/snapshot`.

**`routers/interview.py` — prefix `/interview`** — the adaptive interview engine. **No auth** on most routes (session_id is the capability token). Endpoints include:
- `POST /session` — build plan + first question for a standalone (no-assignment) flow
- `GET /next-question/{session_id}` — current unanswered question
- `GET /speak-question` — Piper TTS
- `POST /answer` / `POST /voice-answer` — runs the full adaptive pipeline (evaluate → maybe generate next or final report)
- `GET /report/{session_id}` — final LLM report
- `POST /flag`, `POST /snapshot`, `GET /snapshot/{id}` — proctoring
- `POST /video`, `POST /video/finalize/{id}`, `GET /video/{id}` — chunked video upload + ffmpeg finalize
- `WS /ws/transcribe/{session_id}` — WebSocket bridge to WhisperLive

**`routers/admin.py` — prefix `/admin`** — admin-only. Every endpoint requires `require_role("admin")`.
- `GET /users`, `GET /users-with-assignments`
- `POST /parse-resume` — extracts text from PDF/DOCX/TXT (limit 10 MB)
- `POST /assignments`, `GET /assignments`, `DELETE /assignments/cleanup`
- `GET /reports`, `GET /reports/{id}`, `DELETE /reports/{id}`
- `GET /snapshots/{id}`
- `GET /approved-candidates`, `PATCH /approved-candidates/{id}/email`, `POST /approved-candidates/{id}/assign` — Resume Parser integration; the last one creates the user, builds the plan, persists the assignment, and emails credentials via SMTP.

**`routers/review.py` — prefix `/review`** — legacy review endpoints. Still functional but the admin panel uses `admin.py`.

### Services

**`services/ai_service.py`** — Groq Llama 3.3 70B via LangChain. Key functions:
- `build_interview_plan_with_usage(jd, resume, yoe)` → plan with phase budget, question count, duration.
- `generate_initial_question_with_usage(plan, yoe)` → opening Phase 1 question.
- `generate_next_question_with_usage(plan, yoe, num, idx, history, latest_eval)` → adaptive next question. Includes a coverage tracker and bigram-based intent dedup so the same area isn't probed twice.
- `evaluate_answer_with_usage(phase, topic, question, answer)` → structured per-question evaluation.
- `generate_final_report_with_usage(plan, history)` → end-of-interview summary.
- `compute_phase_budget(num_questions)` / `get_current_phase(idx, budget)` — phase math (see [Interview Phases](#interview-phases)).

All functions return `(result, token_usage_dict)` for observability — token totals are logged per session.

**`services/stt_service.py`** — `faster-whisper` (Whisper Tiny, `device="cpu"`, `compute_type="int8"`). Used by the file-upload `/interview/voice-answer` and `/user/interview/voice-answer` endpoints.

**`services/streaming_stt_service.py`** — async WebSocket proxy to an external WhisperLive server. Handles the protocol differences (browser sends Float32 PCM + JSON handshake; WhisperLive expects JSON options first then binary; stop sentinel is `b"END_OF_AUDIO"`). Streams partial segments back to the browser.

**`services/tts_service.py`** — wraps the Piper TTS binary. Offline. Voice: `en_US-lessac-medium`.

### Database (`database.py`)

`psycopg2` direct SQL (no ORM). `init_db()` creates all tables with `CREATE TABLE IF NOT EXISTS` and runs additive `ALTER TABLE ... IF NOT EXISTS` migrations for adaptive-interview fields. All timestamps in UTC.

### Authentication (`auth_service.py`)

Custom stateless bearer-token system:
- **Passwords** — PBKDF2-SHA256, 16-byte salt, 100k iterations.
- **Tokens** — `secrets.token_urlsafe(32)`, 7-day TTL.
- **Header** — `Authorization: Bearer <token>`.
- **Role guards** — `require_role("admin")` / `require_role("user")` as FastAPI dependencies.
- **Default admin** — `ensure_default_admin()` upserts from `.env` on startup.

### Schemas (`schemas.py`)

Pydantic models for all request/response bodies. Notable shapes:
- `CreateSessionRequest` / `CreateSessionResponse` — adaptive session bootstrap.
- `AdminCreateAssignmentRequest` — `{user_id, job_description, resume_text, years_of_experience}` (no more `num_questions` / `interview_time_limit_minutes` — the LLM decides).
- `StartAssignmentResponse` — includes the full plan + the first question.
- `AnswerPipelineResponse` — discriminated by `status: "next" | "complete"`, returning either the next question or the final report.
- `EvaluationResponse` — structured per-question evaluation (see [Evaluation Scoring](#evaluation-scoring)).

### Config (`config.py`)

Loaded via `pydantic-settings` from `.env`. See [Environment Variables](#environment-variables).

---

## Frontend Pages

| File | URL | Who uses it | Purpose |
|---|---|---|---|
| `user.html` | `/user` | Candidate | Dashboard — lists assignments |
| `index.html` | `/practice` | Candidate | The interview interface (WS transcription, video recording, code editor) |
| `admin.html` | `/admin` | Admin | Admin home |
| `admin-users.html` | `/admin/users-page` | Admin | Manage candidates |
| `admin-assign.html` | `/admin/assign` | Admin | Assign an interview (JD + resume upload) |
| `admin-reports.html` | `/admin/reports-page` | Admin | Browse reports |
| `reviewer.html` | `/reviewer` | Admin | Deep-dive view of one session (transcripts + video) |
| `admin-common.js` | (shared) | Admin pages | Auth + API helpers |
| `admin-shared.css` | (shared) | Admin pages | Shared styling |

---

## Database Schema

**`users`** — all accounts (admin + candidate).
`id` (TEXT PK), `name`, `email` (UNIQUE), `password_hash`, `role`, `is_active`, `created_at`.

**`auth_tokens`** — login tokens.
`token` (PK), `user_id` (FK), `created_at`, `expires_at`.

**`interview_assignments`** — admin-issued interview record.
Inputs: `user_id`, `job_description`, `resume_text`, `years_of_experience`. Plan is stored alongside (in the linked session). Status fields: `status` (`assigned` / `in_progress` / `completed` / `expired`), `assigned_by`, `session_id`, `assigned_at`, `started_at`, `completed_at`. Legacy fields (`topic`, `num_questions`, `interview_time_limit_minutes`) are kept nullable for old rows.

**`sessions`** — one row per interview attempt.
`id` (PK), `interview_time_limit_minutes`, `user_id`, `assignment_id`, `status`, `created_at`, `completed_at`, plus three JSONB columns:
- `plan` — the LLM-generated interview plan
- `questions` — array of question objects, grows by one after every answered question
- `history` — parallel array of answered question objects (question + evaluation)
- `report` — final LLM report (set when the interview completes)

**`answers`** — one row per answered question.
`id`, `session_id` (FK, cascade), `question_idx`, `question_text`, `transcript`, `evaluation` (JSONB), `answered_at`. `UNIQUE (session_id, question_idx)`.

**`flags`** — proctoring events.
`id`, `session_id` (FK, cascade), `event_type`, `detail`, `flagged_at`.

**`interview_videos`** — pointers to finalized session videos on disk.
`id`, `session_id` (FK, cascade), `video_path`, `question_idx`, `created_at`.

**`approved_candidates`** — pulled from the upstream Resume Parser system. Used by the admin to one-click assign + email credentials.
`candidate_id` (UNIQUE), `candidate_name`, `email`, `score`, `jd_id`, `jd_title`, `resume_filename`, `approved_by`, `is_ready`, `created_at`.

> All child tables use `ON DELETE CASCADE`, so deleting a session removes its answers, flags, and video pointer.

---

## API Reference

### Auth (`/auth`)
| Method | Endpoint | Auth | Description |
|---|---|---|---|
| POST | `/auth/register` | None | Register a candidate |
| POST | `/auth/login` | None | Login, receive bearer token |
| GET | `/auth/me` | User | Current user profile |

### User / Candidate (`/user`)
| Method | Endpoint | Description |
|---|---|---|
| GET | `/user/assignments` | List my assignments |
| POST | `/user/assignments/{id}/start` | Start interview (build session, first question) |
| POST | `/user/assignments/{id}/complete` | Mark complete/expired |
| GET | `/user/interview/session/{id}` | Get my session |
| GET | `/user/interview/speak-question` | Piper TTS for a question |
| POST | `/user/interview/answer` | Submit text/code answer |
| POST | `/user/interview/voice-answer` | Upload WAV answer (faster-whisper) |
| POST | `/user/interview/flag` | Log proctoring event |
| POST | `/user/interview/snapshot` | Save webcam frame |
| GET | `/user/interview/snapshot/{id}` | Fetch a snapshot |

### Interview Engine (`/interview`)
| Method | Endpoint | Description |
|---|---|---|
| POST | `/interview/session` | Build plan + first question (standalone) |
| GET | `/interview/session/{id}` | Get session |
| GET | `/interview/next-question/{id}` | Current unanswered question |
| GET | `/interview/speak-question` | Piper TTS for a question |
| POST | `/interview/answer` | Text answer → evaluate → next question |
| POST | `/interview/voice-answer` | WAV upload → STT → evaluate → next question |
| GET | `/interview/report/{id}` | Final LLM report |
| POST | `/interview/flag` | Log proctoring event |
| POST | `/interview/snapshot` | Save webcam frame |
| GET | `/interview/snapshot/{id}` | Fetch a snapshot |
| POST | `/interview/video` | Upload one video chunk |
| POST | `/interview/video/finalize/{id}` | Concatenate parts → final WebM |
| GET | `/interview/video/{id}` | Stream finalized WebM (lazy-finalizes if needed) |
| WS | `/interview/ws/transcribe/{id}` | Live transcription bridge to WhisperLive |

### Admin (`/admin`)
| Method | Endpoint | Description |
|---|---|---|
| GET | `/admin/users` | List candidates |
| GET | `/admin/users-with-assignments` | Users + their assignments |
| POST | `/admin/parse-resume` | Extract text from a PDF/DOCX/TXT |
| POST | `/admin/assignments` | Create assignment (LLM builds plan) |
| GET | `/admin/assignments` | List all assignments |
| DELETE | `/admin/assignments/cleanup` | Delete *all* assignments (housekeeping) |
| GET | `/admin/reports` | List completed reports (summary) |
| GET | `/admin/reports/{id}` | Full report for one interview |
| DELETE | `/admin/reports/{id}` | Delete a report + session |
| GET | `/admin/snapshots/{id}` | Fetch a proctoring snapshot |
| GET | `/admin/approved-candidates` | List candidates approved by Resume Parser |
| PATCH | `/admin/approved-candidates/{id}/email` | Set/fix candidate email |
| POST | `/admin/approved-candidates/{id}/assign` | Create user + assignment + email credentials |

> Swagger UI: `http://localhost:8000/docs`.

---

## Interview Phases

The LLM splits the interview into three phases (proportions computed by `compute_phase_budget` in `ai_service.py`):

| Phase | Allocation | Purpose |
|---|---|---|
| **Phase 1 — Communication** | ~20%, min 2 / max 4 questions | Warm-up: introductions, motivations, soft-skill signal |
| **Phase 2 — Resume** | ~40% | Probes claims in the candidate's resume — projects, roles, decisions |
| **Phase 3 — Technical** | ~40% | Core technical depth — concepts, scenarios, and code (`requires_code_editor: true` triggers the editor) |

Each question object carries `phase`, `skill_tag`, `source_focus`, `difficulty`, and `requires_code_editor`. The evaluator uses the `phase` field to pick the right rubric.

---

## Evaluation Scoring

The LLM returns a structured evaluation per answer:

| Field | Type | Description |
|---|---|---|
| `score` | int (1–5) | Raw score on this answer |
| `verdict` | string | "Excellent" / "Good" / "Fair" / "Poor" |
| `overview` | string | 2–3 sentence assessment |
| `correct` | string[] | What the candidate got right |
| `missed` | string[] | Important points missed |
| `key_concepts` | `{term, hit}[]` | Concept-coverage tracker |
| `improvement_tip` | string | Actionable feedback |

`summary.avg_score` in `/admin/reports/{id}` is `mean(scores) × 2`, giving a 0–10 display score.

The **final report** (set on `session.report` after the last answer) contains the overall verdict, strengths, gaps, and a hire/no-hire signal — generated by `generate_final_report_with_usage` from the full history.

---

## Setup & Installation

### Prerequisites
- Python 3.11+
- PostgreSQL 12+
- `ffmpeg` (for video stitching) — `sudo apt install ffmpeg`
- Piper TTS binary + `en_US-lessac-medium` voice model
- A running **WhisperLive** server (for live voice answers) — see [WhisperLive repo](https://github.com/collabora/WhisperLive)
- Groq API key — free tier at [console.groq.com](https://console.groq.com)
- SMTP credentials (optional, only needed for the approved-candidates email flow)
- A modern browser (Chrome, Edge, Firefox)

### Step 1 — Install Python dependencies
```bash
cd backend/
pip install -r requirements.txt
```

### Step 2 — Set up Piper TTS
Download the Piper binary from [Piper GitHub Releases](https://github.com/rhasspy/piper/releases) and place it inside `backend/piper/`. Drop the voice model files (`en_US-lessac-medium.onnx` + `.onnx.json`) alongside, or update the path in `services/tts_service.py`.

### Step 3 — Start WhisperLive
Run the WhisperLive server (Docker or local). Default expected at `ws://127.0.0.1:8080`. If you run it elsewhere, set `WHISPERLIVE_WS_URL` in `.env`.

### Step 4 — Create the PostgreSQL database
```bash
createdb techscreen
```

### Step 5 — Configure environment variables
Copy `.env.example` to `backend/.env` and fill in values (see below).

### Step 6 — Run the server
```bash
cd backend/
python -m uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### Step 7 — Access the platform
| Page | URL |
|---|---|
| Candidate Portal | http://localhost:8000/user |
| Admin Panel | http://localhost:8000/admin |
| API Docs (Swagger) | http://localhost:8000/docs |

The default admin account is created automatically from your `.env`.

---

## Environment Variables

Create `backend/.env`:

| Variable | Required | Default | Description |
|---|---|---|---|
| `GROQ_API_KEY` | Yes | — | Your Groq API key |
| `DATABASE_URL` | Yes | — | Postgres URL, e.g. `postgresql://user:pass@localhost:5432/techscreen` |
| `APP_NAME` | No | `TechScreen AI` | Display name |
| `DEBUG` | No | `True` | Enable debug mode |
| `ADMIN_EMAIL` | No | `admin@techscreen.com` | Default admin login |
| `ADMIN_PASSWORD` | No | `admin123` | Default admin password — **change in production** |
| `ADMIN_NAME` | No | `TechScreen Admin` | Admin display name |
| `WHISPERLIVE_WS_URL` | No | `ws://127.0.0.1:8080` | WhisperLive WebSocket URL |
| `WHISPERLIVE_MODEL` | No | `base` | Whisper model size on the WhisperLive side |
| `WHISPERLIVE_LANGUAGE` | No | `en` | Language code |
| `WHISPERLIVE_USE_VAD` | No | `false` | Whether WhisperLive runs VAD |
| `SMTP_HOST` | No | `smtp.gmail.com` | SMTP server |
| `SMTP_PORT` | No | `587` | SMTP port |
| `SMTP_USER` | No | (empty) | SMTP login (leave empty to skip auth) |
| `SMTP_PASSWORD` | No | (empty) | SMTP password / app password |
| `SMTP_USE_TLS` | No | `true` | Use STARTTLS |
| `SMTP_FROM_EMAIL` | No | (empty) | "From" address for credential emails |
| `INTERVIEW_LOGIN_URL` | No | `http://localhost:8000/user` | URL placed in the credentials email |

---

## Typical Response Times

| Operation | Approx. Time |
|---|---|
| Interview-plan generation (LLM) | 3–6 s |
| Single question generation (adaptive) | 2–4 s |
| Piper TTS (question audio) | 0.5–1.5 s |
| WhisperLive live transcription | sub-second latency on partials; final on Stop |
| `faster-whisper` (uploaded WAV, 30 s answer) | 2–5 s |
| LLM evaluation per answer | 2–4 s |
| Final report generation | 3–6 s |
| `ffmpeg` finalize (concat, no re-encode) | < 2 s for typical sessions |

---

## Troubleshooting

**Piper binary not found**
`services/tts_service.py` looks for the Piper binary at a specific path. Verify the `backend/piper/` folder is populated, or update the path.

**WhisperLive unavailable / "SERVER_READY" never arrives**
Confirm the WhisperLive server is running and reachable at `WHISPERLIVE_WS_URL` (default `ws://127.0.0.1:8080`). The proxy logs at `[whisperlive-proxy]` — check Uvicorn output.

**`ffmpeg: command not found`**
Install ffmpeg (`sudo apt install ffmpeg`). Video upload still succeeds, but `/interview/video/finalize/{id}` and the lazy-finalize path in `GET /interview/video/{id}` will fail.

**Groq API quota exceeded**
Adaptive interviews call the LLM ~3× per question (plan once at start, generate next, evaluate). Free tier (~30 req/min) is enough for one or two concurrent interviews. Throttle or upgrade if you hit limits.

**Database connection error**
Verify Postgres is running, `DATABASE_URL` is correct, and the database was created (`createdb techscreen`).

**Token expired / 401**
Tokens expire after 7 days (`TOKEN_TTL_DAYS` in `auth_service.py`). Re-login.

**Snapshot endpoint returns 500**
The current `database.py` does not define a `snapshots` table — the snapshot endpoints will fail until that table is added back. The recorded video covers the same need.

**Assignment stuck in `in_progress`**
Use `DELETE /admin/reports/{assignment_id}` to clean up, or run `backend/recover_assignments.py` to repair orphaned assignments.

**Approved-candidates email never arrives**
Check `SMTP_*` env vars. The router logs a warning (`Failed to send credentials email`) on failure. The account is still created — the admin can share the password manually; the response includes `"email_sent": false` to surface this.

---

## Production Checklist

- Change `ADMIN_PASSWORD` from the default.
- Restrict CORS in `main.py` from `["*"]` to your real domain.
- Use a secrets manager for `GROQ_API_KEY`, `DATABASE_URL`, and `SMTP_PASSWORD`.
- Enable SSL on Postgres.
- Serve behind nginx with HTTPS (Let's Encrypt). Browser webcam/mic only works on HTTPS.
- Set `DEBUG=False`.
- Make sure `videos/` is on a volume with enough space — WebM recordings are typically 50–200 MB per 30-min interview.
- Monitor Groq token usage (logged per session at the `[token-usage]` prefix).
- Run WhisperLive as a managed service (systemd / Docker restart-on-failure) — if it dies mid-interview the candidate can't answer by voice.
