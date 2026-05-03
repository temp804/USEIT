"""
Vercel ASGI Handler for FastAPI Application
This file exports the FastAPI app for Vercel's serverless environment
"""

import sys
from pathlib import Path

# Add parent directory to path to import main module
sys.path.insert(0, str(Path(__file__).parent.parent))

from main import app

# Export app for Vercel
__all__ = ['app']
