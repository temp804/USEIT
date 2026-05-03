"""
Vercel ASGI Handler for FastAPI Application
This file exports the FastAPI app for Vercel's serverless environment
"""

import sys
from pathlib import Path
import os

# Set environment variable for serverless
os.environ['VERCEL'] = '1'

# Add parent directory to path to import main module
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from main import app
except Exception as e:
    import logging
    logging.error(f"Failed to import app from main: {e}", exc_info=True)
    raise

# Vercel looks for 'app' or 'handler' variable
# For FastAPI/ASGI apps, exporting 'app' directly works
__all__ = ['app']
