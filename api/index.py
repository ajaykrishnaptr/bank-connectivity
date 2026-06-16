"""
Vercel serverless entry point.

Vercel's @vercel/python builder looks for a module-level WSGI callable
named `app`, so we just re-export the Flask app from the project root.
The repo root is added to sys.path because this file lives in /api but
imports modules (app, models, *_client) that sit one level up.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402  (must follow the sys.path tweak)

# Importing seed_data here (after `app` is fully loaded, so the
# `from app import app` inside it resolves) guarantees Vercel's
# dependency tracer bundles the seed module — the app auto-seeds an
# empty /tmp database on cold start.
import seed_data  # noqa: E402,F401

__all__ = ["app"]
