FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src
CMD ["python", "src/control_plane.py", "--id", "pxmx-spoke-1", "--secret", "lab-manager-secret", "--hub", "ws://hub:8765"]
