import os
import datetime
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from fpdf import FPDF
from dotenv import load_dotenv

load_dotenv()

APP_NAME = os.getenv("APP_NAME", "Propsely")
PDF_OUTPUT_DIR = os.getenv("PDF_OUTPUT_DIR", "/tmp/generated")
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")

app = FastAPI(title=APP_NAME)

# CORS for Vercel frontend
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


def generate_proposal_text(name, proj_type, budget=None):
    today = datetime.date.today().strftime("%d-%m-%Y")

    price_text = "Pricing will be finalized after discussion."
    if budget:
        price_text = f"Estimated Budget: ₹{budget:,.2f}"

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

Thank you for choosing **Propsely**.  
We deliver high-quality work, clear communication and reliable timelines.

Regards,
Team Propsely
"""
    return content


def generate_pdf(text, client_name):
    os.makedirs(PDF_OUTPUT_DIR, exist_ok=True)

    safe_name = "".join(c for c in client_name if c.isalnum() or c in (' ', '-', '_')).strip()
    file_path = f"{PDF_OUTPUT_DIR}/{safe_name}_proposal.pdf"

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", size=12)

    for line in text.split("\n"):
        pdf.multi_cell(0, 7, txt=line)

    pdf.output(file_path)
    return file_path


@app.get("/")
def home():
    return {"status": "Backend running", "app": APP_NAME}


@app.post("/generate-proposal")
def generate_proposal(payload: ProposalRequest):
    content = generate_proposal_text(
        payload.client_name,
        payload.project_type,
        payload.project_budget
    )

    pdf_path = generate_pdf(content, payload.client_name)

    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=os.path.basename(pdf_path)
    )
