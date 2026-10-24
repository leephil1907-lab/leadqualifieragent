web: gunicorn --bind 0.0.0.0:$PORT --workers 2 --timeout 30 'web.standalone:create_app()'
worker: python -m web.worker
