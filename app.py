#!/usr/bin/env python3
"""
Proposely – Multi-tenant Proposal SaaS backend

Features:
- User registration & login (JWT)
- Proposals stored per user in Postgres (SQLAlchemy)
- /api/proposals/generate → AI (OpenAI) or fallback template
- /api/proposals/create → PDF via ReportLab, stored in Cloudflare R2 or locally
- /api/proposals/all, /api/proposals/<id>, delete, download
- Billing stub: Stripe checkout + webhook
"""

import os
import io
import uuid
import traceback
from datetime import datetime
from typing import Optional, Tuple


from flask import (
    Flask,
    request,
    jsonify,
    send_from_directory,
    abort,
    redirect,
    current_app,
)
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_jwt_extended import (
    JWTManager,
    create_access_token,
    jwt_required,
    get_jwt_identity,
)
from werkzeug.security import generate_password_hash, check_password_hash

# PDF generation
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

# boto3 (S3-compatible for Cloudflare R2)
import boto3
from botocore.exceptions import BotoCoreError, ClientError

# Stripe (optional)
import stripe

# OpenAI (optional)
import openai

# ---------------------
# Load .env
# ---------------------
load_dotenv()

# ---------------------
# Base config
# ---------------------
FLASK_ENV = os.getenv("FLASK_ENV", "production")
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "change_me_jwt")
SECRET_KEY = JWT_SECRET_KEY
PDF_OUTPUT_DIR = os.getenv("PDF_OUTPUT_DIR", "./generated_pdfs")
BASE_URL = os.getenv("BASE_URL")  # if None, will be inferred from request
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "")

# Database URL
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///proposely.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Stripe
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

# OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if OPENAI_API_KEY:
    openai.api_key = OPENAI_API_KEY

# Cloudflare R2
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
R2_ENDPOINT_URL = os.getenv("R2_ENDPOINT_URL")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME")

os.makedirs(PDF_OUTPUT_DIR, exist_ok=True)

# ---------------------
# Flask app
# ---------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY
app.config["JWT_SECRET_KEY"] = JWT_SECRET_KEY
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["PDF_OUTPUT_DIR"] = PDF_OUTPUT_DIR

# ---------------------
# CORS – allow your frontends
# ---------------------

# default allowed origins (can override with ALLOWED_ORIGINS env)
default_origins = [
    "https://proposely.vercel.app",
    "https://proposely-front.vercel.app",
    "https://proposely.lovable.app",
    "http://localhost:5173",
]

if ALLOWED_ORIGINS:
    origins = [o.strip() for o in ALLOWED_ORIGINS.split(",") if o.strip()]
else:
    origins = default_origins

CORS(
    app,
    resources={
        r"/api/*": {
            "origins": origins,
            "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            "allow_headers": ["Content-Type", "Authorization"],
        }
    },
)


@app.after_request
def after_request(response):
    # Extra safety for preflight
    response.headers.add("Access-Control-Allow-Origin", "*")
    response.headers.add(
        "Access-Control-Allow-Headers", "Content-Type,Authorization"
    )
    response.headers.add(
        "Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS"
    )
    return response


# ---------------------
# DB & JWT
# ---------------------
db = SQLAlchemy(app)
jwt = JWTManager(app)

# ---------------------
# R2 client
# ---------------------
_r2_client: Optional[boto3.client] = None
if R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_ENDPOINT_URL and R2_BUCKET_NAME:
    try:
        _r2_client = boto3.client(
            "s3",
            region_name=None,
            endpoint_url=R2_ENDPOINT_URL,
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        )
        app.logger.info(
            "R2 client initialized (endpoint=%s, bucket=%s)",
            R2_ENDPOINT_URL,
            R2_BUCKET_NAME,
        )
    except Exception as e:
        app.logger.exception("Failed to initialize R2 client: %s", e)
        _r2_client = None
else:
    app.logger.info("R2 not configured; falling back to local file storage.")


# ---------------------
# Models
# ---------------------
class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    plan = db.Column(db.String(50), default="free")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    proposals = db.relationship("Proposal", backref="user", lazy=True)


class Proposal(db.Model):
    __tablename__ = "proposals"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    title = db.Column(db.String(255), nullable=False)
    content = db.Column(db.Text, nullable=False)

    pdf_filename = db.Column(db.String(512), nullable=True)
    pdf_url = db.Column(db.String(1024), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)


with app.app_context():
    db.create_all()


# ---------------------
# Utility: base URL
# ---------------------
def get_base_url() -> str:
    global BASE_URL
    if BASE_URL:
        return BASE_URL.rstrip("/")
    # fallback: infer from request
    if request:
        return request.url_root.rstrip("/")
    return "http://localhost:5000"


# ---------------------
# AI Proposal Generator
# ---------------------
def generate_proposal_text(data: dict) -> str:
    """
    data keys: client_name, project_title, scope, budget, timeline, tone, notes
    """
    client_name = data.get("client_name", "Client")
    project_title = data.get("project_title", "Project")
    scope = data.get("scope", "")
    budget = data.get("budget", "Not specified")
    timeline = data.get("timeline", "Not specified")
    tone = data.get("tone", "Professional and friendly")
    notes = data.get("notes", "")

    prompt = f"""
You are an expert proposal writer.

Write a clear, client-ready proposal.

Client Name: {client_name}
Project Title: {project_title}
Scope: {scope}
Budget: {budget}
Timeline: {timeline}
Tone: {tone}
Extra Notes: {notes}

Structure with headings:
- Introduction
- Project Understanding
- Scope of Work
- Deliverables
- Timeline
- Investment
- Next Steps
""".strip()

    if not OPENAI_API_KEY:
        # Fallback template (no external API)
        return f"""# Proposal: {project_title}

## Introduction
Thank you, {client_name}, for the opportunity to collaborate on this project.

## Project Understanding
{scope or "We will work closely with you to clarify project goals and success metrics."}

## Scope of Work
- Requirement analysis and planning
- Design and implementation
- Testing and quality assurance
- Launch and post-launch support

## Deliverables
- Fully implemented solution
- Documentation and training (if required)
- Handover of project assets

## Timeline
{timeline}

## Investment
{budget}

## Next Steps
Once this proposal is approved, we will finalize the timeline and begin the onboarding process.
"""

    try:
        completion = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {
                    "role": "system",
                    "content": "You write concise, persuasive proposals for freelancers and agencies.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.4,
            max_tokens=1200,
        )
        content = completion.choices[0].message["content"]
        return content
    except Exception as e:
        app.logger.exception("OpenAI error: %s", e)
        # fallback if API fails
        return f"""# Proposal: {project_title}

## Introduction
Thank you, {client_name}, for considering us.

## Project Understanding
{scope}

## Timeline
{timeline}

## Investment
{budget}

## Next Steps
We can adjust this proposal as needed to best fit your goals.
"""


# ---------------------
# PDF builder + R2 helpers
# ---------------------
def build_pdf_bytes_from_content(title: str, content: str) -> bytes:
    """
    Very simple multi-page PDF builder from plain text content.
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    y = height - 50
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, y, title[:90])
    y -= 30

    c.setFont("Helvetica", 11)
    for line in content.split("\n"):
        if y < 50:
            c.showPage()
            y = height - 50
            c.setFont("Helvetica", 11)
        c.drawString(50, y, line[:100])
        y -= 16

    c.save()
    buf.seek(0)
    return buf.read()


def upload_bytes_to_r2(file_bytes: bytes, key: str):
    if not _r2_client:
        raise RuntimeError("R2 client not configured")
    try:
        _r2_client.put_object(
            Bucket=R2_BUCKET_NAME,
            Key=key,
            Body=file_bytes,
            ContentType="application/pdf",
        )
        return True
    except (BotoCoreError, ClientError) as e:
        app.logger.exception("R2 upload error: %s", e)
        raise


def presign_r2_key(key: str, expires_in: int = 60 * 60 * 24) -> str:
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


from typing import Optional, Tuple

def store_pdf_and_get_url(pdf_bytes: bytes, filename: str) -> Tuple[str, str]:
    """
    Stores PDF bytes in R2 if available, otherwise locally.
    Returns (pdf_filename, pdf_url).
    """
    uid = uuid.uuid4().hex
    safe_filename = secure_filename(filename) or f"proposal_{uid}.pdf"
    key = f"proposals/{safe_filename}"


    download_url = None
    if _r2_client:
        try:
            upload_bytes_to_r2(pdf_bytes, key)
            download_url = presign_r2_key(key)
        except Exception as e:
            app.logger.exception(
                "R2 upload/presign failed, falling back to local: %s", e
            )
            download_url = None

    if not download_url:
        try:
            filepath = os.path.join(
                current_app.config["PDF_OUTPUT_DIR"], safe_filename
            )
            with open(filepath, "wb") as f:
                f.write(pdf_bytes)
            base = get_base_url()
            download_url = f"{base}/api/proposals/download/{safe_filename}"
        except Exception as e:
            tb = traceback.format_exc()
            app.logger.exception("Failed to store PDF locally: %s", e)
            raise RuntimeError(f"Failed to store PDF: {e}") from e

    return safe_filename, download_url


# ---------------------
# Health
# ---------------------
@app.route("/health", methods=["GET"])
@app.route("/api/health", methods=["GET"])
def health():
    return jsonify(
        {
            "ok": True,
            "service": "Proposely Backend",
            "time": datetime.utcnow().isoformat() + "Z",
        }
    )


# ---------------------
# Auth routes
# ---------------------
@app.route("/api/auth/register", methods=["POST"])
def register():
    data = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"message": "Email and password are required"}), 400

    existing = User.query.filter_by(email=email).first()
    if existing:
        return jsonify({"message": "User already exists"}), 400

    user = User(
        email=email,
        password_hash=generate_password_hash(password),
        plan="free",
    )
    db.session.add(user)
    db.session.commit()

    # identity as string to avoid 422 issues
    token = create_access_token(identity=str(user.id))

    return jsonify(
        {
            "access_token": token,
            "user": {"id": user.id, "email": user.email, "plan": user.plan},
        }
    )


@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"message": "Email and password are required"}), 400

    user = User.query.filter_by(email=email).first()
    if not user or not check_password_hash(user.password_hash, password):
        return jsonify({"message": "Invalid credentials"}), 401

    token = create_access_token(identity=str(user.id))

    return jsonify(
        {
            "access_token": token,
            "user": {"id": user.id, "email": user.email, "plan": user.plan},
        }
    )


@app.route("/api/auth/me", methods=["GET"])
@jwt_required()
def me():
    user_id = int(get_jwt_identity())
    user = User.query.get(user_id)
    if not user:
        return jsonify({"message": "User not found"}), 404

    return jsonify({"id": user.id, "email": user.email, "plan": user.plan})


# ---------------------
# Proposals routes
# ---------------------
@app.route("/api/proposals/generate", methods=["POST"])
@jwt_required()
def proposals_generate():
    """
    Step 2 – Generate proposal text with AI (or fallback).
    """
    if not request.is_json:
        return jsonify({"message": "JSON body required"}), 400

    data = request.get_json() or {}

    try:
        content = generate_proposal_text(data)
        return jsonify({"content": content})
    except Exception as e:
        tb = traceback.format_exc()
        app.logger.exception("Proposal generation failed: %s", e)
        return (
            jsonify(
                {"message": "Generation failed", "details": str(e), "traceback": tb}
            ),
            500,
        )


@app.route("/api/proposals/create", methods=["POST"])
@jwt_required()
def proposals_create():
    """
    Step 3 – Save proposal & create PDF.
    """
    user_id = int(get_jwt_identity())
    user = User.query.get(user_id)
    if not user:
        return jsonify({"message": "User not found"}), 404

    data = request.get_json() or {}
    title = (data.get("title") or "").strip()
    content = (data.get("content") or "").strip()
    generate_pdf_flag = bool(data.get("generate_pdf", True))

    if not title or not content:
        return jsonify({"message": "Title and content are required"}), 400

    proposal = Proposal(user_id=user.id, title=title, content=content)
    db.session.add(proposal)
    db.session.commit()

    pdf_filename = None
    pdf_url = None

    if generate_pdf_flag:
        try:
            pdf_bytes = build_pdf_bytes_from_content(title, content)
            safe_name = f"proposal_{secure_filename(title)[:40]}_{proposal.id}.pdf"
            pdf_filename, pdf_url = store_pdf_and_get_url(pdf_bytes, safe_name)
            proposal.pdf_filename = pdf_filename
            proposal.pdf_url = pdf_url
            db.session.commit()
        except Exception as e:
            tb = traceback.format_exc()
            app.logger.exception("PDF generation/storage failed: %s", e)
            return (
                jsonify(
                    {
                        "message": "Proposal saved but PDF generation failed",
                        "proposal_id": proposal.id,
                        "details": str(e),
                        "traceback": tb,
                    }
                ),
                500,
            )

    return jsonify(
        {
            "id": proposal.id,
            "title": proposal.title,
            "content": proposal.content,
            "pdf_url": proposal.pdf_url,
            "created_at": proposal.created_at.isoformat(),
        }
    )


@app.route("/api/proposals/all", methods=["GET"])
@jwt_required()
def proposals_all():
    user_id = int(get_jwt_identity())
    proposals = (
        Proposal.query.filter_by(user_id=user_id)
        .order_by(Proposal.created_at.desc())
        .all()
    )
    return jsonify(
        [
            {
                "id": p.id,
                "title": p.title,
                "created_at": p.created_at.isoformat(),
                "pdf_url": p.pdf_url,
            }
            for p in proposals
        ]
    )


@app.route("/api/proposals/<int:proposal_id>", methods=["GET"])
@jwt_required()
def proposals_get_one(proposal_id):
    user_id = int(get_jwt_identity())
    proposal = Proposal.query.filter_by(id=proposal_id, user_id=user_id).first()
    if not proposal:
        return jsonify({"message": "Not found"}), 404

    return jsonify(
        {
            "id": proposal.id,
            "title": proposal.title,
            "content": proposal.content,
            "pdf_url": proposal.pdf_url,
            "created_at": proposal.created_at.isoformat(),
        }
    )


@app.route("/api/proposals/<int:proposal_id>", methods=["DELETE"])
@jwt_required()
def proposals_delete(proposal_id):
    user_id = int(get_jwt_identity())
    proposal = Proposal.query.filter_by(id=proposal_id, user_id=user_id).first()
    if not proposal:
        return jsonify({"message": "Not found"}), 404

    # if local PDF, optionally delete file
    if proposal.pdf_filename:
        path = os.path.join(
            current_app.config["PDF_OUTPUT_DIR"], proposal.pdf_filename
        )
        if os.path.isfile(path):
            try:
                os.remove(path)
            except Exception:
                pass

    db.session.delete(proposal)
    db.session.commit()
    return jsonify({"message": "Deleted"})


@app.route("/api/proposals/download/<path:filename>", methods=["GET"])
def proposals_download_by_filename(filename):
    """
    Local storage download endpoint; used when R2 is not configured.
    """
    filename = secure_filename(filename)
    directory = current_app.config["PDF_OUTPUT_DIR"]
    file_path = os.path.join(directory, filename)
    if not os.path.isfile(file_path):
        abort(404)
    return send_from_directory(directory, filename, as_attachment=True)


@app.route("/api/proposals/<int:proposal_id>/download", methods=["GET"])
@jwt_required()
def proposals_download(proposal_id):
    """
    Download endpoint by proposal ID:
    - If pdf_url points to R2 (or any external HTTP), redirect
    - If local, serve the file from disk
    """
    user_id = int(get_jwt_identity())
    proposal = Proposal.query.filter_by(id=proposal_id, user_id=user_id).first()
    if not proposal:
        return jsonify({"message": "Not found"}), 404

    if (
        proposal.pdf_url
        and proposal.pdf_url.startswith("http")
        and "download/" not in proposal.pdf_url
    ):
        # R2 or external URL
        return redirect(proposal.pdf_url)

    if proposal.pdf_filename:
        directory = current_app.config["PDF_OUTPUT_DIR"]
        file_path = os.path.join(directory, proposal.pdf_filename)
        if os.path.isfile(file_path):
            return send_from_directory(
                directory, proposal.pdf_filename, as_attachment=True
            )

    return jsonify({"message": "PDF not available"}), 404


# ---------------------
# Billing (Stripe stub)
# ---------------------
@app.route("/api/billing/create-checkout-session", methods=["POST"])
@jwt_required()
def create_checkout_session():
    if not STRIPE_SECRET_KEY:
        return jsonify({"message": "Stripe not configured"}), 400

    user_id = int(get_jwt_identity())
    user = User.query.get(user_id)
    if not user:
        return jsonify({"message": "User not found"}), 404

    data = request.get_json() or {}
    price_id = data.get("price_id")
    success_url = data.get("success_url") or f"{get_base_url()}/billing/success"
    cancel_url = data.get("cancel_url") or f"{get_base_url()}/billing/cancel"

    if not price_id:
        return jsonify({"message": "price_id is required"}), 400

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            customer_email=user.email,
            success_url=success_url,
            cancel_url=cancel_url,
        )
        return jsonify({"checkout_url": session.url})
    except Exception as e:
        app.logger.exception("Stripe checkout error: %s", e)
        return jsonify({"message": "Stripe error", "details": str(e)}), 500


@app.route("/api/billing/webhook", methods=["POST"])
def stripe_webhook():
    if not STRIPE_WEBHOOK_SECRET:
        return jsonify({"message": "Webhook secret not configured"}), 400

    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        app.logger.exception("Invalid Stripe webhook: %s", e)
        return jsonify({"message": "Invalid payload"}), 400

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        customer_email = session.get("customer_email")
        price_id = None
        if session.get("line_items") and session["line_items"]["data"]:
            price_id = session["line_items"]["data"][0]["price"]["id"]

        # TODO: Map price_id → plan ("pro", "agency") and update user.plan
        # user = User.query.filter_by(email=customer_email).first()
        # if user:
        #     user.plan = "pro"
        #     db.session.commit()

    return jsonify({"received": True})


# ---------------------
# Main
# ---------------------
if __name__ == "__main__":
    app.run(
        debug=(FLASK_ENV == "development"),
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
    )
