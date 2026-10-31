FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY server.py validation.py ./
EXPOSE 8787
CMD ["python", "server.py"]
