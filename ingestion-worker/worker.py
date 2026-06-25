"""
AI Knowledge Centre - Celery Worker Entry Point
Starts the Celery worker listening on the ingestion queue.
"""

from tasks import app

if __name__ == "__main__":
    app.worker_main(["worker", "--loglevel=info", "--concurrency=2", "-Q", "ingestion"])
