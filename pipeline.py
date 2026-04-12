"""
CVTailor Pipeline — FastAPI REST Server
Runs all 5 AI stages and saves results to Supabase.

Storage architecture:
  - original_pdf → Supabase Storage: bucket "cvs"              → DB table: cvs.original_pdf
  - tailored_cv  → Supabase Storage: bucket "generated-outputs" → DB table: generated_documents.pdf_url
  - cover_letter → Supabase Storage: bucket "generated-outputs" → DB table: generated_documents.pdf_url

Security:
  All authenticated endpoints require a valid Supabase session JWT in the
  Authorization: Bearer <token> header. The pipeline verifies the JWT against
  Supabase Auth before processing any request — this prevents UUID-spoofing.
"""
import os
import json
import traceback
import uuid
from datetime import datetime
from io import BytesIO
from typing import Optional

from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import Response, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi import Depends
from pydantic import BaseModel
import pdfplumber
import requests

from stages import parse_cv, parse_job_description, match, generate, clean
from pdf_utils import cv_to_pdf, cover_letter_to_pdf

# ─── Config ──────────────────────────────────────────────────────────────────
MINIMAX_API_KEY = os.getenv("MINIMAX_API_KEY", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
PORT = int(os.getenv("PORT", "3001"))

app = FastAPI(title="CVTailor Pipeline", version="1.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Supabase client ──────────────────────────────────────────────────────────
_supabase_client = None

def get_supabase():
    """Lazily initialise Supabase client (service-role, bypasses RLS)."""
    global _supabase_client
    if _supabase_client is None and SUPABASE_URL and SUPABASE_SERVICE_KEY:
        from supabase import create_client
        _supabase_client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    return _supabase_client


# ─── Security: JWT Session Verification ───────────────────────────────────────

class AuthenticatedUser:
    """Parsed identity of a verified Supabase session."""
    def __init__(self, user_id: str, email: str = ""):
        self.id = user_id
        self.email = email


def verify_supabase_session(authorization: Optional[str] = Header(None)) -> AuthenticatedUser:
    """
    FastAPI dependency. Validates a Supabase session JWT and returns the authenticated user.

    Callers MUST confirm the returned user.id matches the user_id they intend to use
    before processing any data. This ensures the requester is genuinely who they claim
    to be — not a UUID they guessed or borrowed.

    Raises HTTPException(401) if the token is missing, invalid, or expired.
    """
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="Missing Authorization header. Format: 'Bearer <supabase-session-token'"
        )

    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid Authorization header format. Use: 'Bearer <token>'")

    token = parts[1]
    sb = get_supabase()

    if not sb:
        raise HTTPException(status_code=500, detail="Supabase not configured on server")

    try:
        # verify_jwt_token() is available in supabase-py >= 2.0
        user = sb.auth.get_user(token)
        if not user or not user.user:
            raise HTTPException(status_code=401, detail="Invalid or expired session token")
        return AuthenticatedUser(user_id=user.user.id, email=getattr(user.user, "email", ""))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Session verification failed: {str(e)}")


def require_user_id_match(verified_user: AuthenticatedUser, requested_user_id: str) -> None:
    """
    Confirm the authenticated session user matches the user_id in the request.
    Call this at the top of every endpoint that takes a user_id parameter.

    Raises HTTPException(403) if they don't match.
    """
    if verified_user.id != requested_user_id:
        raise HTTPException(
            status_code=403,
            detail="Forbidden: requested user_id does not match authenticated session"
        )


# ─── Pydantic models ──────────────────────────────────────────────────────────

class CVParseRequest(BaseModel):
    cv_pdf_base64: str
    user_id: Optional[str] = None  # still accepted for convenience but verified against JWT


class ProcessRequest(BaseModel):
    cv_id: str                          # ID of the parsed CV in Supabase
    job_description: str
    user_id: str                        # provided by caller, verified against JWT


# ─── Helpers ──────────────────────────────────────────────────────────────────

def extract_text_from_pdf_base64(b64_data: str) -> str:
    """Extract text from a base64-encoded PDF using pdfplumber."""
    import base64
    pdf_bytes = base64.b64decode(b64_data)
    text_parts = []
    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                text_parts.append(t)
    return "\n".join(text_parts)


def _upload_to_supabase_storage(bucket: str, path: str, content: bytes, mime: str) -> Optional[str]:
    """Upload a file to Supabase Storage. Returns the public URL or None on failure."""
    sb = get_supabase()
    if not sb:
        return None
    try:
        res = sb.storage.from_(bucket).upload(path, content, {"content-type": mime})
        # Get public URL
        public_url = sb.storage.from_(bucket).get_public_url(path)
        return public_url
    except Exception as e:
        print(f"[Storage] Upload failed for {path}: {e}")
        return None


def _save_cv_to_supabase(user_id: str, pdf_base64: str, parsed_data: dict) -> Optional[dict]:
    """Save original CV PDF + parsed data to Supabase.

    Returns: {"cv_id": "...", "pdf_url": "..."} or None
    """
    sb = get_supabase()
    if not sb or not user_id:
        return None

    import base64
    try:
        pdf_bytes = base64.b64decode(pdf_base64)

        # 1. Upload original PDF to Storage
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        storage_path = f"{user_id}/cv_{timestamp}.pdf"
        pdf_url = _upload_to_supabase_storage("cvs", storage_path, pdf_bytes, "application/pdf")

        # 2. Deactivate previous active CVs for this user
        sb.table("cvs").update({"is_active": False}).eq("user_id", user_id).eq("is_active", True).execute()

        # 3. Get next version number
        existing = sb.table("cvs").select("version").eq("user_id", user_id).order("version", desc=True).limit(1).execute()
        next_version = (existing.data[0]["version"] + 1) if existing.data else 1

        # 4. Insert new CV record
        cv_record = {
            "user_id": user_id,
            "original_pdf": pdf_url,
            "parsed_data": parsed_data,
            "version": next_version,
            "is_active": True,
        }
        res = sb.table("cvs").insert(cv_record).execute()
        cv_id = res.data[0]["id"]
        return {"cv_id": cv_id, "pdf_url": pdf_url, "version": next_version}

    except Exception as e:
        print(f"[CV Save] Error: {e}")
        return None


def _save_application_and_documents(
    user_id: str,
    cv_id: str,
    job_description: str,
    parsed_jd: dict,
    match_data: dict,
    tailored_cv: str,
    cover_letter: str,
    ats_keywords: str = "",
) -> Optional[dict]:
    """Save job application + generated documents to Supabase.

    Returns: {"application_id": "...", "cv_doc_id": "...", "cl_doc_id": "...", "cv_pdf_url": "...", "cl_pdf_url": "..."}
    """
    sb = get_supabase()
    if not sb or not user_id:
        return None

    try:
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")

        # 1. Insert job application record
        app_record = {
            "user_id": user_id,
            "job_description": job_description,
            "parsed_jd": parsed_jd,
            "match_score": match_data.get("match_score"),
            "match_analysis": match_data,
            "status": "generated",
        }
        app_res = sb.table("job_applications").insert(app_record).execute()
        application_id = app_res.data[0]["id"]

        # 2. Generate and upload tailored CV PDF
        cv_doc_id = cl_doc_id = cv_pdf_url = cl_pdf_url = None

        # Try to build CV PDF from parsed data
        try:
            # Reconstruct cv_data from the stored CV record if needed
            cv_record = sb.table("cvs").select("parsed_data").eq("id", cv_id).single().execute()
            cv_data = cv_record.data["parsed_data"]
            cv_pdf_bytes = cv_to_pdf(cv_data, ats_keywords=ats_keywords)
            cv_storage_path = f"{user_id}/tailored_cv_{timestamp}.pdf"
            cv_pdf_url = _upload_to_supabase_storage("generated-outputs", cv_storage_path, cv_pdf_bytes, "application/pdf")
        except Exception as e:
            print(f"[CV PDF] Could not generate CV PDF: {e}")

        # 3. Generate and upload cover letter PDF
        try:
            cl_pdf_bytes = cover_letter_to_pdf(cover_letter)
            cl_storage_path = f"{user_id}/cover_letter_{timestamp}.pdf"
            cl_pdf_url = _upload_to_supabase_storage("generated-outputs", cl_storage_path, cl_pdf_bytes, "application/pdf")
        except Exception as e:
            print(f"[CL PDF] Could not generate CL PDF: {e}")

        # 4. Insert tailored CV document record
        if tailored_cv:
            cv_doc_record = {
                "user_id": user_id,
                "application_id": application_id,
                "cv_id": cv_id,
                "type": "tailored_cv",
                "content": tailored_cv,
                "pdf_url": cv_pdf_url,
            }
            cv_doc_res = sb.table("generated_documents").insert(cv_doc_record).execute()
            cv_doc_id = cv_doc_res.data[0]["id"]

        # 5. Insert cover letter document record
        if cover_letter:
            cl_doc_record = {
                "user_id": user_id,
                "application_id": application_id,
                "cv_id": cv_id,
                "type": "cover_letter",
                "content": cover_letter,
                "pdf_url": cl_pdf_url,
            }
            cl_doc_res = sb.table("generated_documents").insert(cl_doc_record).execute()
            cl_doc_id = cl_doc_res.data[0]["id"]

        return {
            "application_id": application_id,
            "cv_doc_id": cv_doc_id,
            "cl_doc_id": cl_doc_id,
            "cv_pdf_url": cv_pdf_url,
            "cl_pdf_url": cl_pdf_url,
        }

    except Exception as e:
        print(f"[Application Save] Error: {e}")
        return None


# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


@app.post("/cv/save")
def cv_save(req: CVParseRequest, verified_user: AuthenticatedUser = Depends(verify_supabase_session)):
    """
    Stage 1 variant: Parse a CV PDF AND save it to Supabase in one call.
    Requires: Authorization: Bearer <supabase-session-token> header.

    - Saves original PDF to Supabase Storage (bucket: cvs)
    - Saves parsed CV data to public.cvs table
    - Deactivates previous CVs for this user (versioning)

    Returns: { "status": "done", "cv_id": "...", "pdf_url": "...", "cv_data": {...} }
    """
    # Enforce the authenticated user owns this operation
    user_id = req.user_id or verified_user.id
    require_user_id_match(verified_user, user_id)

    try:
        cv_text = extract_text_from_pdf_base64(req.cv_pdf_base64)
        if not cv_text.strip():
            raise ValueError("Could not extract text from PDF. Is it a valid PDF?")

        cv_data = parse_cv(cv_text)
        supabase_result = _save_cv_to_supabase(user_id, req.cv_pdf_base64, cv_data)

        result = {"status": "done", "cv_data": cv_data}
        if supabase_result:
            result["cv_id"] = supabase_result["cv_id"]
            result["pdf_url"] = supabase_result["pdf_url"]
            result["version"] = supabase_result["version"]

        return JSONResponse(result)

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/pipeline/cv/parse")
def cv_parse(req: CVParseRequest):
    """
    Stage 1 only: Parse a CV PDF into structured JSON.
    No Supabase involvement — use /cv/save if you want persistence.
    """
    try:
        cv_text = extract_text_from_pdf_base64(req.cv_pdf_base64)
        if not cv_text.strip():
            raise ValueError("Could not extract text from PDF. Is it a valid PDF?")

        cv_data = parse_cv(cv_text)

        return JSONResponse({
            "status": "done",
            "cv_data": cv_data,
            "tokens_used": len(cv_text.split()) * 1.3,
        })

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/pipeline/process")
def process(req: ProcessRequest, verified_user: AuthenticatedUser = Depends(verify_supabase_session)):
    """
    Full 5-stage pipeline with Supabase persistence.
    Requires: Authorization: Bearer <supabase-session-token> header.
    Also requires: user_id + cv_id in body.

    Flow:
      1. Verify session matches user_id
      2. Fetch parsed CV data from Supabase (using cv_id, verified against user_id)
      3. Parse JD → Match → Generate → Clean
      4. Save tailored CV + cover letter to Supabase Storage + DB

    Returns: {
        "status": "done",
        "application_id": "...",   ← use this to retrieve documents later
        "match_score": 72,
        "match_analysis": {...},
        "tailored_cv": "...",
        "cover_letter": "...",
        "cv_pdf_url": "...",       ← Supabase Storage URL for PDF download
        "cl_pdf_url": "...",
    }
    """
    sb = get_supabase()
    if not sb:
        raise HTTPException(status_code=500, detail="Supabase not configured. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env")

    try:
        # Security: verify the requesting user owns this data
        require_user_id_match(verified_user, req.user_id)
        user_id = req.user_id
        cv_id = req.cv_id

        # 1. Fetch stored CV data from Supabase
        cv_record = sb.table("cvs").select("parsed_data, original_pdf").eq("id", cv_id).eq("user_id", user_id).single().execute()
        if not cv_record.data:
            raise HTTPException(status_code=404, detail=f"CV not found: {cv_id} for user {user_id}")
        cv_data = cv_record.data["parsed_data"]

        # 2. Stage 2: Parse JD
        jd_data = parse_job_description(req.job_description)

        # 3. Stage 3: Match
        match_data = match(cv_data, jd_data)

        # 4. Stage 4: Generate
        generated = generate(cv_data, jd_data, match_data)
        tailored_cv = generated.get("tailored_cv", "")
        cover_letter = generated.get("cover_letter", "")

        ats_keywords = generated.get("ats_keywords", "")

        # 5. Stage 5: Clean
        cleaned = clean(tailored_cv, cover_letter)
        cleaned_cv = cleaned.get("cleaned_cv", tailored_cv)
        cleaned_cl = cleaned.get("cleaned_cover_letter", cover_letter)

        # 6. Save to Supabase (application + documents + PDFs)
        supabase_result = _save_application_and_documents(
            user_id=user_id,
            cv_id=cv_id,
            job_description=req.job_description,
            parsed_jd=jd_data,
            match_data=match_data,
            tailored_cv=cleaned_cv,
            cover_letter=cleaned_cl,
            ats_keywords=ats_keywords,
        )

        response = {
            "status": "done",
            "match_score": match_data.get("match_score", 0),
            "match_analysis": match_data,
            "tailored_cv": cleaned_cv,
            "cover_letter": cleaned_cl,
        }

        if supabase_result:
            response["application_id"] = supabase_result["application_id"]
            response["cv_pdf_url"] = supabase_result["cv_pdf_url"]
            response["cl_pdf_url"] = supabase_result["cl_pdf_url"]

        return JSONResponse(response)

    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/pipeline/download/{output_type}")
def download(output_type: str, text: str):
    """
    Return a PDF download for a given output type.
    output_type: "cv" or "cover_letter"
    text: the text content to render as PDF
    """
    if output_type == "cv":
        try:
            cv_data = json.loads(text)
            pdf_bytes = cv_to_pdf(cv_data)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid CV data")
    elif output_type == "cover_letter":
        pdf_bytes = cover_letter_to_pdf(text)
    else:
        raise HTTPException(status_code=400, detail="Invalid output type")

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="cvtailor_{output_type}.pdf"'
        },
    )


@app.get("/applications")
def list_applications(user_id: str, verified_user: AuthenticatedUser = Depends(verify_supabase_session)):
    """
    List all job applications for the authenticated user.
    Requires: Authorization: Bearer <supabase-session-token> header.
    """
    require_user_id_match(verified_user, user_id)
    sb = get_supabase()
    if not sb:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    try:
        apps = sb.table("job_applications").select(
            "id, job_description, match_score, status, created_at"
        ).eq("user_id", user_id).order("created_at", desc=True).execute()

        return JSONResponse({"status": "done", "applications": apps.data})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/documents/{application_id}")
def get_documents(application_id: str, user_id: str, verified_user: AuthenticatedUser = Depends(verify_supabase_session)):
    """
    Get all generated documents for an application.
    Requires: Authorization: Bearer <supabase-session-token> header.
    """
    require_user_id_match(verified_user, user_id)
    sb = get_supabase()
    if not sb:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    try:
        docs = sb.table("generated_documents").select(
            "id, type, content, pdf_url, created_at"
        ).eq("application_id", application_id).eq("user_id", user_id).execute()

        return JSONResponse({"status": "done", "documents": docs.data})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Startup ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    print(f"Starting CVTailor Pipeline on port {PORT}")
    print(f"Supabase: {'configured' if SUPABASE_URL else 'NOT configured'}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
