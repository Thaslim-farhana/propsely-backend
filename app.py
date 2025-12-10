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

# Load env
load_dotenv()

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("propsely")

# Environment defaults (use ./generated_pdfs for local dev; change in Render to /tmp/generated)
APP_NAME = os.getenv("APP_NAME", "Propsely")
PDF_OUTPUT_DIR = os.getenv("PDF_OUTPUT_DIR", "./generated_pdfs")
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")

app = FastAPI(title=APP_NAME)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in ALLOWED_ORIGINS],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ProposalRequest(BaseModel):
    client_name: str
    project_type: str
    project_budget: float | None = None


def generate_proposal_text(name: str, proj_type: str, budget: float | None = None) -> str:
    today = datetime.date.today().strftime("%d-%m-%Y")
    price_text = "Pricing will be finalized after discussion."
    if budget:
        price_text = f"Estimated Budget: Rs {budget:,.2f}"

    content = f"""
PROSELY — AUTO PROPOSAL & QUOTATION
Generated on: {today}

Client Name: {name}
Project Type: {proj_type}

----------------------------------------
PROJECT SUMMARY
----------------------------------------
This proposal outlines the scope, deliverables and commercial terms
for the {proj_type} project for {name}.

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
{price_text}

----------------------------------------
TERMS
----------------------------------------
- 50% advance to start.
- Final payment on delivery.
- GST extra if applicable.
- Scope changes may cost extra.

----------------------------------------
COVER LETTER
----------------------------------------
Dear {name},

Thank you for choosing Propsely.
We deliver high-quality work, clear communication and reliable timelines.

Regards,
Team Propsely
"""
    return content


def safe_filename(name: str) -> str:
    s = "".join(c for c in name if c.isalnum() or c in (' ', '-', '_')).strip()
    return s.replace(" ", "_") if s else "client"


def sanitize_text(s: str) -> str:
    """
    Replace common Unicode characters with ASCII equivalents and ensure text is latin-1 safe
    (pyfpdf uses latin-1). Any remaining non-latin1 characters are replaced.
    """
    if not s:
        return ""
    replacements = {
        "—": "-",   # em dash
        "–": "-",   # en dash
        "“": '"', "”": '"',
        "‘": "'", "’": "'",
        "…": "...",
        "₹": "Rs ",
        "©": "(c)",
        "®": "(R)",
        "™": "TM",
    }
    for k, v in replacements.items():
        s = s.replace(k, v)
    # Replace characters not representable in latin-1 with '?'
    safe = s.encode("latin-1", errors="replace").decode("latin-1")
    return safe


def generate_pdf(text: str, client_name: str) -> str:
    """
    Create a PDF from text and return absolute file path.
    Uses sanitize_text to avoid Latin-1 encoding errors in pyfpdf.
    """
    try:
        os.makedirs(PDF_OUTPUT_DIR, exist_ok=True)
        filename = f"{safe_filename(client_name)}_proposal.pdf"
        file_path = os.path.abspath(os.path.join(PDF_OUTPUT_DIR, filename))

        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=10)
        pdf.add_page()

        # Try Arial; fallback to Times if necessary
        try:
            pdf.set_font("Arial", size=12)
        except Exception as font_err:
            logger.warning("Arial font not available, falling back to Times. %s", font_err)
            try:
                pdf.set_font("Times", size=12)
            except Exception as e:
                logger.warning("Fallback font also failed: %s", e)

        # Write sanitized lines
        for line in (text or "").splitlines():
            safe_line = sanitize_text(line)
            if not safe_line.strip():
                pdf.ln(5)
            else:
                try:
                    pdf.multi_cell(0, 6, txt=safe_line)
                except Exception as write_err:
                    logger.error("Error writing line to PDF: %s. Line: %.100s", write_err, safe_line)
                    pdf.multi_cell(0, 6, txt="[content skipped due to write error]")

        pdf.output(file_path)
        logger.info("PDF created at: %s", file_path)
        return file_path

    except Exception as e:
        tb = traceback.format_exc()
        logger.error("PDF generation failed: %s\n%s", e, tb)
        raise HTTPException(status_code=500, detail="PDF generation failed; check server logs")


@app.get("/")
def home():
    return {"status": "Backend running", "app": APP_NAME}


@app.post("/generate-proposal")
def generate_proposal(payload: ProposalRequest):
    logger.info("Received generate-proposal request: client=%s project=%s budget=%s",
                payload.client_name, payload.project_type, payload.project_budget)
    content = generate_proposal_text(payload.client_name, payload.project_type, payload.project_budget)
    pdf_path = generate_pdf(content, payload.client_name)

    if not os.path.exists(pdf_path):
        logger.error("PDF not found after generation: %s", pdf_path)
        raise HTTPException(status_code=500, detail="PDF not created")

    return FileResponse(pdf_path, media_type="application/pdf", filename=os.path.basename(pdf_path))
