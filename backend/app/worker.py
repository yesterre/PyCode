"""Celery entrypoint: celery -A backend.app.worker:app worker."""

from backend.app.infrastructure.celery import create_celery_app


app = create_celery_app()
