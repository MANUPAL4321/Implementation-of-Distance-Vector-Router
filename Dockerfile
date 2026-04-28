FROM alpine:latest
RUN apk add --no-cache python3 iproute2
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY router.py router.py
CMD ["python3", "router.py"]
