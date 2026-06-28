"""
auth.py
-------
Blueprint for login / signup / logout.
Passwords hashed with bcrypt. Sessions managed by Flask-Login.
"""

from flask import Blueprint, request, jsonify
from flask_bcrypt import Bcrypt
from flask_login import login_user, logout_user, login_required, current_user
from models import User, get_session_factory

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")
bcrypt  = Bcrypt()


def _db():
    return get_session_factory()()


# ─── Signup ───────────────────────────────────────────────────────────────────

@auth_bp.route("/signup", methods=["POST"])
def signup():
    data     = request.get_json() or {}
    username = (data.get("username") or "").strip()
    email    = (data.get("email") or "").strip() or None
    password = data.get("password") or ""

    if not username or not password:
        return jsonify({"error": "username and password are required"}), 400
    if len(password) < 6:
        return jsonify({"error": "password must be at least 6 characters"}), 400

    sess = _db()
    if sess.query(User).filter_by(username=username).first():
        return jsonify({"error": "username already taken"}), 409

    hashed = bcrypt.generate_password_hash(password).decode("utf-8")
    user   = User(username=username, email=email, hashed_password=hashed)
    sess.add(user)
    sess.commit()

    login_user(user)
    return jsonify({"message": "account created", "user_id": user.id, "username": user.username}), 201


# ─── Login ────────────────────────────────────────────────────────────────────

@auth_bp.route("/login", methods=["POST"])
def login():
    data     = request.get_json() or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    sess = _db()
    user = sess.query(User).filter_by(username=username).first()

    if not user or not bcrypt.check_password_hash(user.hashed_password, password):
        return jsonify({"error": "invalid credentials"}), 401

    login_user(user)
    return jsonify({"message": "logged in", "user_id": user.id, "username": user.username})


# ─── Logout ───────────────────────────────────────────────────────────────────

@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return jsonify({"message": "logged out"})


# ─── Whoami ───────────────────────────────────────────────────────────────────

@auth_bp.route("/me", methods=["GET"])
@login_required
def me():
    return jsonify({"user_id": current_user.id, "username": current_user.username})
