#!/usr/bin/env python3
"""
Auto Proposal & Quotation Generator - Flask backend (Cloudflare R2 enabled)

- Builds PDF in-memory
- Uploads to Cloudflare R2 (S3-compatible) if configured via env
- Returns presigned download URL (24h default)
- Falls back to local file saving if R2 not configured
"""

import os
import io
import uuid
import traceback
from datetime import datetime
from typing import Optional
from flask import Flask, request, jsonify, send_from_directory, abort
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

# PDF generation
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.units import mm

# boto3 (S3-compatible for Cloudflare R2)
import boto3
from botocore.exceptions import BotoCoreError, ClientError

# CORS
from flask_cors import CORS

load_dotenv()

# ---------------------
# Configuration (env)
# ---------------------
FLASK_ENV = os.getenv("FLASK_ENV", "production")
SECRET_KEY = os.getenv("SECRET_KEY", "change_me")
PDF_OUTPUT_DIR = os.getenv("PDF_OUTPUT_DIR", "./generated_pdfs")
BASE_URL = os.getenv("BASE_URL", "http://localhost:5000")  # used for local fallback URLs
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "")  # comma-separated origins

# Cloudflare R2 specific envs
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID")  # Cloudflare R2 access key id
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")  # Cloudflare R2 secret
R2_ENDPOINT_URL = os.getenv("R2_ENDPOINT_URL")  # example: https://<account_id>.r2.cloudflarestorage.com
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME")  # your bucket name

# Ensure local output dir exists for fallback
os.makedirs(PDF_OUTPUT_DIR, exist_ok=True)

# Flask app
app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY
app.config["PDF_OUTPUT_DIR"] = PDF_OUTPUT_DIR

# Configure CORS
if ALLOWED_ORIGINS:
    origins = [o.strip() for o in ALLOWED_ORIGINS.split(",") if o.strip()]
    CORS(app, resources={r"/*": {"origins": origins}})
else:
    # permissive in development; in production supply ALLOWED_ORIGINS
    if FLASK_ENV == "development":
        CORS(app)
    else:
        CORS(app, resources={r"/*": {"origins": "*"}})

# Initialize R2 (boto3 S3 client) if configured
_r2_client: Optional[boto3.client] = None
if R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_ENDPOINT_URL and R2_BUCKET_NAME:
    try:
        _r2_client = boto3.client(
            "s3",
            region_name=None,  # R2 doesn't require region; boto3 accepts None
            endpoint_url=R2_ENDPOINT_URL,
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        )
        app.logger.info("R2 client initialized (endpoint=%s, bucket=%s)", R2_ENDPOINT_URL, R2_BUCKET_NAME)
    except Exception as e:
        app.logger.exception("Failed to initialize R2 client: %s", e)
        _r2_client = None
else:
    app.logger.info("R2 not configured; falling back to local file storage.")


# ---------------------
# Business logic
# ---------------------
def simple_pricing_for(project_type: str):
    t = (project_type or "").strip().lower()
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
        items = [
            ("Discovery & Requirements", 400.00, "1-2 weeks"),
            ("Execution", 1500.00, "variable"),
            ("Maintenance (1 month)", 200.00, "1 month"),
        ]

    line_items = []
    total = 0.0
    for name, price, duration in items:
        line_items.append({"name": name, "price": round(price, 2), "duration": duration})
        total += price
    contingency = round(total * 0.05, 2)
    line_items.append({"name": "Contingency (5%)", "price": contingency, "duration": "—"})
    total += contingency
    return line_items, round(total, 2)


def generate_cover_letter(client_name: str, project_type: str, company_name: str = "Your Company"):
    now = datetime.utcnow().strftime("%B %d, %Y")
    return (
        f"{now}\n\n"
        f"Dear {client_name},\n\n"
        f"Thank you for considering {company_name} for your {project_type} needs. "
        f"We've prepared the enclosed proposal which outlines scope, pricing, and terms. "
        f"Our goal is to deliver high-quality results on time and within budget. "
        f"If you have questions or need adjustments, we'll be happy to iterate.\n\n"
        f"Warm regards,\n"
        f"{company_name}\n"
    )


def generate_contract_text(client_name: str, project_type: str, company_name: str = "Your Company"):
    today = datetime.utcnow().strftime("%B %d, %Y")
    return (
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


# ---------------------
# PDF builder & R2 helpers
# ---------------------
def build_pdf_bytes(client_name, project_type, pricing_table, total, cover_letter, contract_text, company_name="Your Company"):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=20 * mm, bottomMargin=20 * mm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("Title", parent=styles["Heading1"], fontSize=18, leading=22)
    normal = styles["Normal"]
    elements = []

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

    elements.append(Paragraph("Pricing & Scope", styles["Heading2"]))
    elements.append(Spacer(1, 6))
    table_data = [["Item", "Duration", "Price (USD)"]]
    for li in pricing_table:
        table_data.append([li.get("name"), li.get("duration", "—"), f"${li.get('price', 0):.2f}"])
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
    elements.append(Paragraph("Contract & Terms", styles["Heading2"]))
    for paragraph in contract_text.split("\n\n"):
        elements.append(Paragraph(paragraph.replace("\n", "<br/>"), normal))
        elements.append(Spacer(1, 6))

    doc.build(elements)
    buf.seek(0)
    return buf.read()


def upload_bytes_to_r2(file_bytes: bytes, key: str):
    if not _r2_client:
        raise RuntimeError("R2 client not configured")
    try:
        # use put_object; key is the object name in the bucket
        _r2_client.put_object(Bucket=R2_BUCKET_NAME, Key=key, Body=file_bytes, ContentType="application/pdf")
        return True
    except (BotoCoreError, ClientError) as e:
        app.logger.exception("R2 upload error: %s", e)
        raise


def presign_r2_key(key: str, expires_in: int = 60 * 60 * 24):
    if not _r2_client:
        raise RuntimeError("R2 client not configured")
    try:
        url = _r2_client.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": R2_BUCKET_NAME, "Key": key},
            ExpiresIn=expires_in,
        )
        return url
    except (BotoCoreError, ClientError) as e:
        app.logger.exception("R2 presign error: %s", e)
        raise


# ---------------------
# Routes
# ---------------------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "Auto Proposal & Quotation Generator", "time": datetime.utcnow().isoformat() + "Z"})


@app.route("/generate", methods=["POST"])
def generate():
    if not request.is_json:
        return jsonify({"error": "JSON body required"}), 400

    data = request.get_json()
    client_name = (data.get("client_name") or "").strip()
    project_type = (data.get("project_type") or "").strip()
    company_name = (data.get("company_name") or "Your Company").strip()

    if not client_name or not project_type:
        return jsonify({"error": "client_name and project_type are required"}), 400

    pricing_table, total = simple_pricing_for(project_type)
    cover_letter = generate_cover_letter(client_name=client_name, project_type=project_type, company_name=company_name)
    contract_text = generate_contract_text(client_name=client_name, project_type=project_type, company_name=company_name)

    uid = uuid.uuid4().hex
    safe_client = secure_filename(client_name) or "client"
    safe_proj = secure_filename(project_type) or "project"
    filename = f"proposal_{safe_client[:40]}_{safe_proj[:40]}_{uid}.pdf"
    r2_key = f"proposals/{filename}"

    try:
        pdf_bytes = build_pdf_bytes(
            client_name=client_name,
            project_type=project_type,
            pricing_table=pricing_table,
            total=total,
            cover_letter=cover_letter,
            contract_text=contract_text,
            company_name=company_name,
        )
    except Exception as e:
        tb = traceback.format_exc()
        app.logger.exception("PDF build failed: %s", e)
        return jsonify({"error": "PDF build failed", "details": str(e), "traceback": tb}), 500

    download_url = None
    # Try R2 upload
    if _r2_client:
        try:
            upload_bytes_to_r2(pdf_bytes, r2_key)
            download_url = presign_r2_key(r2_key, expires_in=60 * 60 * 24)  # 24 hours
        except Exception as e:
            app.logger.exception("R2 upload/presign failed, will attempt local save: %s", e)
            download_url = None

    # Fallback local save if R2 not available or upload failed
    if not download_url:
        try:
            filepath = os.path.join(app.config["PDF_OUTPUT_DIR"], filename)
            with open(filepath, "wb") as f:
                f.write(pdf_bytes)
            download_url = f"{BASE_URL.rstrip('/')}/download/{secure_filename(filename)}"
        except Exception as e:
            tb = traceback.format_exc()
            app.logger.exception("Failed to save PDF locally: %s", e)
            return jsonify({"error": "Failed to store PDF", "details": str(e), "traceback": tb}), 500

    response = {
        "id": uid,
        "proposal_pdf_filename": secure_filename(filename),
        "proposal_pdf_download_url": download_url,
        "pricing_table": pricing_table,
        "total": total,
        "cover_letter": cover_letter,
        "contract_text": contract_text,
        "e_signature": {"enabled": False, "note": "E-sign integration coming later", "endpoint": None},
    }
    return jsonify(response), 201


@app.route("/download/<path:filename>", methods=["GET"])
def download(filename):
    filename = secure_filename(filename)
    directory = app.config["PDF_OUTPUT_DIR"]
    file_path = os.path.join(directory, filename)
    if not os.path.isfile(file_path):
        abort(404)
    return send_from_directory(directory, filename, as_attachment=True)


if __name__ == "__main__":
    app.run(debug=(FLASK_ENV == "development"), host="0.0.0.0", port=5000)
