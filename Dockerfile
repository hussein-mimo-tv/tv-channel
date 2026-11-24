# صورة خفيفة: بايثون + ffmpeg فقط
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
RUN mkdir -p videos
# لا تنسخ أي فيديو داخل الصورة: الفيديوهات تُرفع من الواجهة بعد النشر،
# أو تُضاف بروابط مباشرة — قرص الخطة المجانية مؤقت ويُفقد عند إعادة التشغيل.
EXPOSE 10000
CMD ["python3", "app.py"]
