-- ============================================================
-- CVTailor Supabase Schema
-- Run this in Supabase SQL Editor: https://supabase.com/dashboard
-- ============================================================

-- Enable UUID extension
create extension if not exists "uuid-ossp";

-- ───────────────────────────────────────────────────────────
-- TABLE 1: cvs
-- Stores parsed CV data for each user (versioned, one active)
-- ───────────────────────────────────────────────────────────
create table if not exists public.cvs (
  id            uuid        primary key default uuid_generate_v4(),
  user_id       uuid        not null references auth.users(id) on delete cascade,
  original_pdf   text,                   -- Supabase Storage URL for the uploaded PDF
  parsed_data   jsonb       not null,   -- Structured JSON from Stage 1 (skills, experience, etc.)
  version       int         default 1,
  is_active     boolean     default true,
  created_at    timestamptz default now(),
  updated_at    timestamptz default now(),

  unique (user_id, version)
);

-- Index for fast lookup by user
create index if not exists cvs_user_id_idx on public.cvs(user_id);
create index if not exists cvs_user_active_idx on public.cvs(user_id) where is_active = true;

-- ───────────────────────────────────────────────────────────
-- TABLE 2: job_applications
-- Each job the user applies for — stores the JD and match analysis
-- ───────────────────────────────────────────────────────────
create table if not exists public.job_applications (
  id              uuid        primary key default uuid_generate_v4(),
  user_id         uuid        not null references auth.users(id) on delete cascade,
  job_description text        not null,   -- Raw JD as entered by user
  parsed_jd       jsonb,                  -- Structured JD from Stage 2
  match_score     int,                     -- Overall match score 0-100
  match_analysis  jsonb,                   -- Full Stage 3 analysis (gaps, strengths, etc.)
  status          text        default 'pending',  -- pending | generated | viewed
  created_at      timestamptz default now(),
  updated_at      timestamptz default now()
);

create index if not exists job_applications_user_id_idx on public.job_applications(user_id);
create index if not exists job_applications_created_at_idx on public.job_applications(created_at desc);

-- ───────────────────────────────────────────────────────────
-- TABLE 3: generated_documents
-- Tailored CV and cover letter for each job application
-- ───────────────────────────────────────────────────────────
create table if not exists public.generated_documents (
  id              uuid        primary key default uuid_generate_v4(),
  user_id         uuid        not null references auth.users(id) on delete cascade,
  application_id  uuid        references public.job_applications(id) on delete set null,
  cv_id           uuid        references public.cvs(id) on delete set null,  -- which CV was used
  type            text        not null,  -- 'tailored_cv' or 'cover_letter'
  content         text        not null,  -- Full generated text content
  pdf_url         text,                  -- Supabase Storage URL (if saved as PDF)
  created_at      timestamptz default now()
);

create index if not exists generated_documents_user_id_idx on public.generated_documents(user_id);
create index if not exists generated_documents_application_id_idx on public.generated_documents(application_id);

-- ───────────────────────────────────────────────────────────
-- Row Level Security (RLS)
-- ============================================================
-- IMPORTANT: Enable RLS on all tables.
-- Users can only see/modify their own data.

alter table public.cvs enable row level security;
alter table public.job_applications enable row level security;
alter table public.generated_documents enable row level security;

-- CVs: user owns their own rows
create policy "Users can view own CVs"    on public.cvs for select using (auth.uid() = user_id);
create policy "Users can insert own CVs"   on public.cvs for insert with check (auth.uid() = user_id);
create policy "Users can update own CVs"   on public.cvs for update using (auth.uid() = user_id);
create policy "Users can delete own CVs"  on public.cvs for delete using (auth.uid() = user_id);

-- Job applications: user owns their own rows
create policy "Users can view own applications"    on public.job_applications for select using (auth.uid() = user_id);
create policy "Users can insert own applications"  on public.job_applications for insert with check (auth.uid() = user_id);
create policy "Users can update own applications"  on public.job_applications for update using (auth.uid() = user_id);

-- Generated documents: user owns their own rows
create policy "Users can view own documents"    on public.generated_documents for select using (auth.uid() = user_id);
create policy "Users can insert own documents"   on public.generated_documents for insert with check (auth.uid() = user_id);
create policy "Users can update own documents"  on public.generated_documents for update using (auth.uid() = user_id);

-- ───────────────────────────────────────────────────────────
-- Storage Buckets
-- Run in Supabase Dashboard > Storage > New bucket, OR via SQL:
-- ───────────────────────────────────────────────────────────
-- The pipeline saves files to Supabase Storage. Create these buckets:
--
-- Bucket 1: "cvs" (for original uploaded PDFs)
-- Bucket 2: "generated-outputs" (for tailored CV + cover letter PDFs)
--
-- In Supabase Dashboard:
--   Storage > Create new bucket > Name: "cvs"          > Public: true
--   Storage > Create new bucket > Name: "generated-outputs" > Public: true
--
-- Or via SQL (if you have storage admin rights):
-- insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
-- values ('cvs', 'cvs', true, 10485760, array['application/pdf']);
--
-- insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
-- values ('generated-outputs', 'generated-outputs', true, 10485760, array['application/pdf']);

-- ───────────────────────────────────────────────────────────
-- Storage Access Policies (allow authenticated users to upload)
-- ───────────────────────────────────────────────────────────

-- Allow users to upload their own CV PDF
create policy "Users can upload own CV PDF"
  on storage.objects for insert
  with check (bucket_id = 'cvs' and auth.uid()::text = (storage.foldername(name))[1]);

create policy "Users can view own CV PDFs"
  on storage.objects for select
  using (bucket_id = 'cvs' and auth.uid()::text = (storage.foldername(name))[1]);

-- Allow users to upload their own generated documents
create policy "Users can upload own generated docs"
  on storage.objects for insert
  with check (bucket_id = 'generated-outputs' and auth.uid()::text = (storage.foldername(name))[1]);

create policy "Users can view own generated docs"
  on storage.objects for select
  using (bucket_id = 'generated-outputs' and auth.uid()::text = (storage.foldername(name))[1]);

-- ───────────────────────────────────────────────────────────
-- Function: update_updated_at
-- (Auto-update updated_at column)
-- ───────────────────────────────────────────────────────────
create or replace function public.handle_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

create trigger cvs_updated_at
  before update on public.cvs
  for each row execute function public.handle_updated_at();

create trigger job_applications_updated_at
  before update on public.job_applications
  for each row execute function public.handle_updated_at();
