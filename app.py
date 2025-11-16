#!/usr/bin/env python3
"""
Auto Proposal & Quotation Generator - Minimal SaaS backend

Features:
- POST /generate -> accepts JSON { "client_name": "...", "project_type": "..." }
  returns JSON containing:
    - proposal_pdf_download_url
    - pricing_table (array)
    - contract_text
    - cover_letter
    - e_signature (placeholder)
- GET /download/<filename> -> downloads the generated PDF
- GET /health -> simple health check
"""

import os
import io
import uuid
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory, abort
from flask_cors import CORS

app = Flask(__name__)

CORS(app, resources={r"/*": {"origins": [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "https://proposely.lovable.app",
    "*",  # OPTIONAL: Allow all during development
]}})
   # Vite default dev url
# Or during development use:
# CORS(app, resources={r"/*": {"origins": "*"}})

from werkzeug.utils import secure_filename
from dotenv import load_dotenv

# PDF generation: ReportLab
from reportlab.lib.pagesizes import A4
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    PageBreak,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.units import mm

# Load .env
load_dotenv()

FLASK_ENV = os.getenv("FLASK_ENV", "production")
SECRET_KEY = os.getenv("SECRET_KEY", "change_me")
PDF_OUTPUT_DIR = os.getenv("PDF_OUTPUT_DIR", "./generated_pdfs")
BASE_URL = os.getenv("BASE_URL", "http://localhost:5000")

# Ensure output directory exists
os.makedirs(PDF_OUTPUT_DIR, exist_ok=True)

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY
app.config["PDF_OUTPUT_DIR"] = PDF_OUTPUT_DIR
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # sensible default


# ---------------------
# Business logic
# ---------------------
def simple_pricing_for(project_type: str):
    """
    Basic pricing engine: returns list of line items and total.
    This is intentionally simple — replace with real logic later.
    """
    t = project_type.strip().lower()
    # Base price and multipliers by "type"
    if "website" in t:
        items = [
            ("Discovery & Planning", 500.00, "2 weeks"),
            ("Design & Prototyping", 1200.00, "2-3 weeks"),
            ("Development (static pages)", 1800.00, "2-4 weeks"),
            ("QA & Launch", 400.00, "1 week"),
        ]
    elif "mobile" in t or "app" in t:
        items = [
            ("Discovery & Planning", 700.00, "2 weeks"),
            ("UI/UX Design", 1500.00, "3 weeks"),
            ("Development (MVP)", 4500.00, "6-10 weeks"),
            ("QA & Launch", 800.00, "1-2 weeks"),
        ]
    elif "seo" in t or "marketing" in t:
        items = [
            ("SEO Audit", 300.00, "1 week"),
            ("On-page Optimization", 600.00, "2-4 weeks"),
            ("Content & Outreach (monthly)", 800.00, "monthly"),
        ]
    else:
        # Generic project
        items = [
            ("Discovery & Requirements", 400.00, "1-2 weeks"),
            ("Execution", 1500.00, "variable"),
            ("Maintenance (1 month)", 200.00, "1 month"),
        ]

    # Convert to dict form
    line_items = []
    total = 0.0
    for name, price, duration in items:
        line_items.append({"name": name, "price": round(price, 2), "duration": duration})
        total += price

    # Add a small contingency line
    contingency = round(total * 0.05, 2)
    line_items.append({"name": "Contingency (5%)", "price": contingency, "duration": "—"})
    total += contingency

    return line_items, round(total, 2)


def generate_cover_letter(client_name: str, project_type: str, company_name: str = "Your Company"):
    now = datetime.utcnow().strftime("%B %d, %Y")
    letter = (
        f"{now}\n\n"
        f"Dear {client_name},\n\n"
        f"Thank you for considering {company_name} for your {project_type} needs. "
        f"We've prepared the enclosed proposal which outlines scope, pricing, and terms. "
        f"Our goal is to deliver high-quality results on time and within budget. "
        f"If you have questions or need adjustments, we'll be happy to iterate.\n\n"
        f"Warm regards,\n"
        f"{company_name}\n"
    )
    return letter


def generate_contract_text(client_name: str, project_type: str, company_name: str = "Your Company"):
    today = datetime.utcnow().strftime("%B %d, %Y")
    contract = (
        f"Agreement between {company_name} (\"Provider\") and {client_name} (\"Client\")\n\n"
        f"Date: {today}\n\n"
        "1. Scope\n"
        f"Provider will perform the work described in the attached proposal for the {project_type}.\n\n"
        "2. Payment\n"
        "Client agrees to pay the amounts set out in the proposal. Unless otherwise agreed, invoices are due within 14 days.\n\n"
        "3. Intellectual Property\n"
        "Upon full payment, Provider will transfer rights to the deliverables to the Client, excluding any third-party licensed components.\n\n"
        "4. Confidentiality\n"
        "Both parties agree to keep confidential information private.\n\n"
        "5. Termination\n"
        "Either party may terminate with 14 days' written notice. Fees for work performed up to termination are payable.\n\n"
        "6. Governing Law\n"
        "This agreement is governed by the laws applicable to the Provider's jurisdiction.\n\n"
        "Signature: ______________________\n"
        "Name: __________________________\n"
        "Date: ___________________________\n"
    )
    return contract


# ---------------------
# PDF generation
# ---------------------
def generate_proposal_pdf(
    filename: str,
    client_name: str,
    project_type: str,
    pricing_table: list,
    total: float,
    cover_letter: str,
    contract_text: str,
    company_name: str = "Your Company",
):
    """
    Create a PDF file (saved to app.config['PDF_OUTPUT_DIR'] / filename).
    Uses ReportLab to build a simple multipage PDF with cover letter, pricing table, contract.
    """
    filepath = os.path.join(app.config["PDF_OUTPUT_DIR"], filename)
    doc = SimpleDocTemplate(filepath, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=20 * mm, bottomMargin=20 * mm)
    styles = getSampleStyleSheet()
    # Customize styles
    title_style = ParagraphStyle(
        "Title",
        parent=styles["Heading1"],
        fontSize=18,
        leading=22,
    )
    normal = styles["Normal"]
    elements = []

    # Title page / Cover
    elements.append(Paragraph(f"{company_name} — Proposal", title_style))
    elements.append(Spacer(1, 6))
    elements.append(Paragraph(f"Client: <b>{client_name}</b>", normal))
    elements.append(Paragraph(f"Project: <b>{project_type}</b>", normal))
    elements.append(Paragraph(f"Date: {datetime.utcnow().strftime('%B %d, %Y')}", normal))
    elements.append(Spacer(1, 12))
    elements.append(Paragraph("Cover Letter", styles["Heading2"]))
    for para in cover_letter.split("\n\n"):
        elements.append(Paragraph(para.replace("\n", "<br/>"), normal))
        elements.append(Spacer(1, 6))

    elements.append(PageBreak())

    # Pricing Table
    elements.append(Paragraph("Pricing & Scope", styles["Heading2"]))
    elements.append(Spacer(1, 6))

    table_data = [["Item", "Duration", "Price (USD)"]]
    for li in pricing_table:
        table_data.append([li["name"], li.get("duration", "—"), f"${li['price']:.2f}"])
    table_data.append(["", "Total", f"${total:.2f}"])

    table = Table(table_data, colWidths=[90 * mm, 40 * mm, 30 * mm])
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -2), 0.25, colors.grey),
                ("LINEBELOW", (0, 0), (-1, 0), 1, colors.black),
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("ALIGN", (-1, 0), (-1, -1), "RIGHT"),
                ("ALIGN", (1, 0), (1, -1), "CENTER"),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
            ]
        )
    )
    elements.append(table)
    elements.append(Spacer(1, 12))

    elements.append(PageBreak())

    # Contract / Terms
    elements.append(Paragraph("Contract & Terms", styles["Heading2"]))
    for paragraph in contract_text.split("\n\n"):
        elements.append(Paragraph(paragraph.replace("\n", "<br/>"), normal))
        elements.append(Spacer(1, 6))

    # Build and save
    doc.build(elements)
    return filepath


# ---------------------
# Routes
# ---------------------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "Auto Proposal & Quotation Generator", "time": datetime.utcnow().isoformat() + "Z"})


@app.route("/generate", methods=["POST"])
def generate():
    """
    Expected JSON:
    {
      "client_name": "Acme Corp",
      "project_type": "Website redesign",
      "company_name": "Optional Your Company"
    }
    """
    if not request.is_json:
        return jsonify({"error": "JSON body required"}), 400

    data = request.get_json()
    client_name = data.get("client_name", "").strip()
    project_type = data.get("project_type", "").strip()
    company_name = data.get("company_name", "Your Company").strip()

    if not client_name or not project_type:
        return jsonify({"error": "client_name and project_type are required"}), 400

    # Compute pricing
    pricing_table, total = simple_pricing_for(project_type)

    # Generate texts
    cover_letter = generate_cover_letter(client_name=client_name, project_type=project_type, company_name=company_name)
    contract_text = generate_contract_text(client_name=client_name, project_type=project_type, company_name=company_name)

    # Create unique filename
    uid = uuid.uuid4().hex
    safe_client = secure_filename(client_name) or "client"
    safe_proj = secure_filename(project_type) or "project"
    filename = f"proposal_{safe_client[:40]}_{safe_proj[:40]}_{uid}.pdf"

    # Generate PDF
    try:
        filepath = generate_proposal_pdf(
            filename=filename,
            client_name=client_name,
            project_type=project_type,
            pricing_table=pricing_table,
            total=total,
            cover_letter=cover_letter,
            contract_text=contract_text,
            company_name=company_name,
        )
    except Exception as e:
        app.logger.exception("Failed to generate PDF")
        return jsonify({"error": "PDF generation failed", "details": str(e)}), 500

    # Build response with download URL and payload data
    download_url = f"{BASE_URL.rstrip('/')}/download/{secure_filename(filename)}"
    response = {
        "proposal_pdf_filename": secure_filename(filename),
        "proposal_pdf_download_url": download_url,
        "pricing_table": pricing_table,
        "total": total,
        "cover_letter": cover_letter,
        "contract_text": contract_text,
        # e-sign placeholder for future
        "e_signature": {"enabled": False, "note": "E-sign integration coming later", "endpoint": None},
    }
    return jsonify(response), 201


@app.route("/download/<path:filename>", methods=["GET"])
def download(filename):
    # Security: only serve from configured directory
    filename = secure_filename(filename)
    directory = app.config["PDF_OUTPUT_DIR"]
    file_path = os.path.join(directory, filename)
    if not os.path.isfile(file_path):
        abort(404)
    # send_from_directory will set appropriate headers
    return send_from_directory(directory, filename, as_attachment=True)

# --- Add this to app.py (paste above the __main__ block) ---

from flask import render_template_string, send_from_directory

# Simple landing page so GET / doesn't 404
@app.route("/", methods=["GET"])
def index():
    html = """
    <!doctype html>
    <html>
      <head>
        <meta charset="utf-8"/>
        <title>Auto Proposal & Quotation Generator</title>
      </head>
      <body style="font-family:system-ui,Segoe UI,Arial;padding:20px;">
        <h1>Auto Proposal & Quotation Generator</h1>
        <p>Use the <code>POST /generate</code> endpoint (application/json) to create a proposal PDF.</p>
        <p>Example curl:</p>
        <pre>
curl -X POST http://localhost:5000/generate \\
  -H "Content-Type: application/json" \\
  -d '{"client_name":"Acme Corp","project_type":"Website redesign","company_name":"Zooye Enterprises"}'
        </pre>
        <p><small>Generated PDFs are saved to <code>{{ pdf_dir }}</code>.</small></p>
      </body>
    </html>
    """
    return render_template_string(html, pdf_dir=app.config.get("PDF_OUTPUT_DIR", "./generated_pdfs"))


# Serve a favicon if you create static/favicon.ico; otherwise return 204 (no content)
@app.route("/favicon.ico")
def favicon():
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    favicon_path = os.path.join(static_dir, "favicon.ico")
    if os.path.exists(favicon_path):
        return send_from_directory(static_dir, "favicon.ico")
    # No favicon - return empty 204 so browsers stop asking repeatedly
    return ("", 204)

# For quick local testing
if __name__ == "__main__":
    app.run(debug=(FLASK_ENV == "development"), host="0.0.0.0", port=5000)
