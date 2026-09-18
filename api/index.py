"""
Vercel serverless entry point.

Vercel's @vercel/python builder looks for a module-level WSGI callable
named `app`, so this re-exports the Flask app from the `fintnet` package.
The repository root is added to sys.path because this file lives in /api.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fintnet.app import app  # noqa: E402  (must follow the sys.path tweak)

# Importing seed_data here (after `app` is fully loaded) guarantees Vercel's
# dependency tracer bundles the seed module: the app auto-seeds an empty
# database on cold start.
from fintnet import seed_data  # noqa: E402,F401
