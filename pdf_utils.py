"""
PDF generation for tailored CVs and cover letters using ReportLab.
"""
import os
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle
from reportlab.lib import colors


# ─── ATS Invisible Text Block ────────────────────────────────────────────────
# White-on-white 1pt text at the bottom of the last page.
# Invisible to human readers, fully readable by ATS/AI screening tools.

def _build_ats_block(ats_text: str) -> list:
    """Return a list of ReportLab flowables that render the invisible ATS block."""
    ats_style = ParagraphStyle(
        "ats_invisible",
        fontName="Helvetica",
        fontSize=1,          # 1pt — invisible at normal reading distances
        leading=1.2,
        textColor=colors.white,   # white on white = invisible
        spaceAfter=0,
        spaceBefore=0,
    )
    return [
        Spacer(1, 0.1 * cm),
        HRFlowable(width="100%", thickness=0.25, color=colors.HexColor("#dddddd")),
        Paragraph(ats_text, ats_style),
    ]


def make_doc(buffer: BytesIO) -> tuple:
    """Create a styled PDF document and return (doc, styles)."""
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=2.5 * cm,
        rightMargin=2.5 * cm,
        topMargin=2.5 * cm,
        bottomMargin=2.5 * cm,
    )
    styles = {
        "name": ParagraphStyle("name", fontName="Helvetica-Bold", fontSize=16,
                               leading=20, spaceAfter=4),
        "contact": ParagraphStyle("contact", fontName="Helvetica", fontSize=10,
                                  leading=14, spaceAfter=2, textColor=colors.HexColor("#444444")),
        "section": ParagraphStyle("section", fontName="Helvetica-Bold", fontSize=12,
                                   leading=16, spaceAfter=4, spaceBefore=12,
                                   textColor=colors.HexColor("#1a1a1a")),
        "heading": ParagraphStyle("heading", fontName="Helvetica-Bold", fontSize=11,
                                   leading=14, spaceAfter=2),
        "body": ParagraphStyle("body", fontName="Helvetica", fontSize=10,
                               leading=15, spaceAfter=6),
        "bullet": ParagraphStyle("bullet", fontName="Helvetica", fontSize=10,
                                 leading=14, spaceAfter=2, leftIndent=12,
                                 bulletIndent=0),
        "salutation": ParagraphStyle("salutation", fontName="Helvetica", fontSize=11,
                                     leading=16, spaceAfter=8),
    }
    return doc, styles


def cv_to_pdf(cv_data: dict, ats_keywords: str = "") -> bytes:
    """Generate a PDF from structured CV data dict.

    Args:
        cv_data: dict with name, email, phone, location, summary, skills,
                 experience, education, languages fields.
        ats_keywords: Optional string of ATS keywords to embed as invisible
                      white-on-white 1pt text on the last page.
    """
    buffer = BytesIO()
    doc, s = make_doc(buffer)
    story = []

    # ── Branded header block ────────────────────────────────────────────────
    brand_blue = colors.HexColor("#1B3A6B")
    name      = cv_data.get("name", "Adrian Maciaszek")
    email     = cv_data.get("email", "")
    phone     = cv_data.get("phone", "")
    location  = cv_data.get("location", "")

    header_data = [[
        Paragraph(f'<font color="white"><b>{name}</b></font>',
                  ParagraphStyle("hdr_name", fontName="Helvetica-Bold", fontSize=18,
                                 leading=22, textColor=colors.white)),
        Paragraph(f'{email}  |  {phone}  |  {location}',
                  ParagraphStyle("hdr_contact", fontName="Helvetica", fontSize=9,
                                 leading=13, textColor=colors.HexColor("#BBCCE8"))),
    ]]
    tbl = Table(header_data, colWidths=[7 * cm, 10 * cm])
    tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), brand_blue),
        ("TOPPADDING",   (0, 0), (-1, -1), 14),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 14),
        ("LEFTPADDING",  (0, 0), (-1, -1), 16),
        ("RIGHTPADDING", (0, 0), (-1, -1), 16),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 0.3 * cm))

    contact_parts = []
    if cv_data.get("email"):
        contact_parts.append(cv_data["email"])
    if cv_data.get("phone"):
        contact_parts.append(cv_data["phone"])
    if cv_data.get("location"):
        contact_parts.append(cv_data["location"])
    if contact_parts:
        story.append(Paragraph("  |  ".join(contact_parts), s["contact"]))

    story.append(Spacer(1, 0.2 * cm))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#cccccc")))
    story.append(Spacer(1, 0.2 * cm))

    # Summary
    if cv_data.get("summary"):
        story.append(Paragraph("Profile", s["section"]))
        story.append(Paragraph(cv_data["summary"], s["body"]))

    # Skills
    if cv_data.get("skills"):
        story.append(Paragraph("Skills", s["section"]))
        for skill in cv_data["skills"]:
            story.append(Paragraph(f"  {skill}", s["bullet"]))

    # Experience
    if cv_data.get("experience"):
        story.append(Paragraph("Experience", s["section"]))
        for exp in cv_data["experience"]:
            role_line = f"{exp.get('role', '')} — {exp.get('company', '')}"
            if exp.get("duration"):
                role_line += f"  ({exp['duration']})"
            story.append(Paragraph(role_line, s["heading"]))
            for resp in exp.get("responsibilities", []):
                story.append(Paragraph(f"  {resp}", s["bullet"]))
            story.append(Spacer(1, 0.1 * cm))

    # Education
    if cv_data.get("education"):
        story.append(Paragraph("Education", s["section"]))
        for edu in cv_data["education"]:
            edu_line = f"{edu.get('qualification', '')} — {edu.get('institution', '')}"
            if edu.get("year"):
                edu_line += f"  ({edu['year']})"
            story.append(Paragraph(edu_line, s["body"]))

    # Languages
    if cv_data.get("languages"):
        story.append(Paragraph("Languages", s["section"]))
        story.append(Paragraph(", ".join(cv_data["languages"]), s["body"]))

    # ── ATS invisible block — appended AFTER all visible content ─────────
    if ats_keywords:
        for fl in _build_ats_block(ats_keywords):
            story.append(fl)

    doc.build(story)
    buffer.seek(0)
    return buffer.read()


def cover_letter_to_pdf(text: str) -> bytes:
    """Generate a PDF from cover letter text."""
    buffer = BytesIO()
    doc, s = make_doc(buffer)
    story = []

    lines = text.strip().split("\n")
    for line in lines:
        line = line.strip()
        if not line:
            story.append(Spacer(1, 0.15 * cm))
            continue
        # Detect section headings (all caps or short bold-looking lines)
        if line.isupper() and len(line) < 40:
            story.append(Paragraph(line, s["section"]))
        elif line.startswith("Dear ") or line.startswith("Yours "):
            story.append(Paragraph(line, s["salutation"]))
        else:
            story.append(Paragraph(line, s["body"]))

    doc.build(story)
    buffer.seek(0)
    return buffer.read()
