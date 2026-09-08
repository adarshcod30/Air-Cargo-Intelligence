"""Serverless entrypoint.

The deployed surface is the read path only: the API reads precomputed rows
and never runs the analytics. That separation is what makes a serverless
deployment possible at all - statsmodels, scipy and pandas together exceed
the function size limit, and none of them is imported here. Forecasts and
anomalies are computed by the scheduled pipeline and arrive as rows.
"""

from services.api.main import app

# Vercel's Python runtime looks for a module-level ASGI application.
__all__ = ["app"]
