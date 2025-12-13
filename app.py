import os
import datetime
import logging
import traceback

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from fpdf import FPDF
from dotenv import load_dotenv

# -------------------------------------------------
# Load environment variables
# -------------------------------------------------
load_dotenv()

# -------------------------------------------------
# Logging
# -------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("propsely")

# -------------------------------------------------
# App config
# -------------------------------------------------
APP_NAME = os.getenv("APP_NAME", "Propsely")

# Local: ./generated_pdfs
# Render: /tmp/generated_pdfs
PDF_OUTPUT_DIR = os.getenv("PDF_OUTPUT_DIR", "./generated_pdfs")

app = FastAPI(title=APP_NAME)

# -------------------------------------------------
# ✅ CORS (FIXED — NO "*", NO DUPLICATES)
# -------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://proposely.lovable.app",
        "https://propsely-front.vercel.app",
        "http://localhost:3000",
        "http://localhost:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------------------------------
# Models
# -------------------------------------------------
class ProposalRequest(BaseModel):
    client_name: str
    project_type: str
    project_budget: float | None = None

# -------------------------------------------------
# Helpers
# -------------------------------------------------
def safe_filename(name: str) -> str:
    clean = "".join(c for c in name if c.isalnum() or c in (" ", "-", "_")).strip()
    return clean.replace(" ", "_") if clean else "client"

def sanitize_text(text: str) -> str:
    """
    pyFPDF only supports latin-1.
    Replace common unicode characters safely.
    """
    if not text:
        return ""

    replacements = {
        "—": "-",
        "–": "-",
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "…": "...",
        "₹": "Rs ",
    }

    for k, v in replacements.items():
        text = text.replace(k, v)

    return text.encode("latin-1", errors="replace").decode("latin-1")

# -------------------------------------------------
# Proposal Text Generator
# -------------------------------------------------
def generate_proposal_text(name: str, project: str, budget: float | None) -> str:
    today = datetime.date.today().strftime("%d-%m-%Y")
    price = (
        f"Estimated Budget: Rs {budget:,.2f}"
        if budget
        else "Pricing will be finalized after discussion."
    )

    return f"""
PROSELY — AUTO PROPOSAL & QUOTATION
Generated on: {today}

Client Name: {name}
Project Type: {project}

----------------------------------------
PROJECT SUMMARY
----------------------------------------
This proposal outlines the scope, deliverables and commercial terms
for the {project} project for {name}.

----------------------------------------
DELIVERABLES
----------------------------------------
1. Requirement Discussion
2. Planning & Strategy
3. UI/UX (If applicable)
4. Development
5. Review & Testing
6. Final Deployment
7. Support

----------------------------------------
PRICING
----------------------------------------
{price}

----------------------------------------
TERMS
----------------------------------------
- 50% advance to start
- Final payment on delivery
- GST extra if applicable
- Scope changes may cost extra

----------------------------------------
COVER LETTER
----------------------------------------
Dear {name},

Thank you for choosing Propsely.
We deliver high-quality work, clear communication and reliable timelines.

Regards,
Team Propsely
"""

# -------------------------------------------------
# PDF Generator
# -------------------------------------------------
def generate_pdf(text: str, client_name: str) -> str:
    try:
        os.makedirs(PDF_OUTPUT_DIR, exist_ok=True)

        filename = f"{safe_filename(client_name)}_proposal.pdf"
        file_path = os.path.abspath(os.path.join(PDF_OUTPUT_DIR, filename))

        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=12)
        pdf.add_page()

        try:
            pdf.set_font("Arial", size=12)
        except Exception:
            pdf.set_font("Times", size=12)

        for line in sanitize_text(text).splitlines():
            if line.strip():
                pdf.multi_cell(0, 7, line)
            else:
                pdf.ln(4)

        pdf.output(file_path)
        logger.info("PDF created at %s", file_path)
        return file_path

    except Exception as e:
        logger.error("PDF generation failed: %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail="PDF generation failed")

# -------------------------------------------------
# Routes
# -------------------------------------------------
@app.get("/")
def health():
    return {"status": "ok", "app": APP_NAME}

@app.post("/generate-proposal")
def generate_proposal(payload: ProposalRequest):
    logger.info(
        "Generate proposal: client=%s project=%s budget=%s",
        payload.client_name,
        payload.project_type,
        payload.project_budget,
    )

    content = generate_proposal_text(
        payload.client_name,
        payload.project_type,
        payload.project_budget,
    )

    pdf_path = generate_pdf(content, payload.client_name)

    if not os.path.exists(pdf_path):
        raise HTTPException(status_code=500, detail="PDF not created")

    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=os.path.basename(pdf_path),
    )
