# CVTailor Pipeline

AI-powered CV and cover letter tailoring. Paste a job description, supply your CV, get back a optimised CV and cover letter formatted as PDFs.

## Architecture

```
stages.py      — 5 AI stages (parse CV, parse JD, match, generate, clean)
pipeline.py    — FastAPI REST server wrapping the 5 stages + Supabase storage
pdf_utils.py   — ReportLab PDF generation (cover letters + tailored CVs)
supabase_schema.sql — Database schema
```

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/adriandestiny/cv-pipeline.git
cd cv-pipeline
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your MINIMAX_API_KEY and Supabase credentials
```

### 3. Set up Supabase

Run `supabase_schema.sql` in your Supabase SQL Editor:
https://supabase.com/dashboard

### 4. Run

```bash
# Development
source venv/bin/activate
python pipeline.py

# Production (systemd)
sudo bash setup_service.sh
```

The API will start on `http://localhost:3001`.

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `MINIMAX_API_KEY` | Yes | MiniMax API key (platform.minimaxi.com) |
| `SUPABASE_URL` | Yes | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Yes | Supabase service role key |
| `PORT` | No | Server port (default: 3001) |
| `HERMES_SKILLS_PATH` | No | Path to Hermes skills dir for optional PDF skill |

## API Endpoints

| Method | Endpoint | Auth | Description |
|---|---|---|---|
| `GET` | `/health` | No | Health check |
| `POST` | `/cv/save` | JWT | Upload CV, parse + save to Supabase |
| `POST` | `/pipeline/cv/parse` | No | Parse CV only (no persistence) |
| `POST` | `/pipeline/process` | JWT | Full 5-stage pipeline + save |
| `GET` | `/pipeline/download/{type}` | No | Download CV or cover letter as PDF |
| `GET` | `/applications` | JWT | List user's job applications |

## Supabase Storage Buckets

Create these buckets in Supabase Dashboard → Storage:

- `cvs` — stores original uploaded CV PDFs (private, signed URLs)
- `generated-outputs` — stores generated CV and cover letter PDFs (private)

## Tech Stack

- **AI**: MiniMax M2.7 (Anthropic-compatible API)
- **Database**: Supabase (Postgres + Auth + Storage)
- **PDF**: ReportLab
- **API**: FastAPI + Uvicorn
- **Auth**: Supabase session JWTs
