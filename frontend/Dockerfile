# syntax=docker/dockerfile:1
# FRONTEND image: static HTML/CSS/JS served by nginx, which forwards /api and /auth to the backend.
#   build (from the project root): docker build -t pharma-frontend ./frontend
#   run   : docker run -p 8080:8080 -e BACKEND_URL=http://host.docker.internal:8000 pharma-frontend
# Runs as a non-root user on port 8080. BACKEND_URL is read at start-up (see nginx.conf).
FROM nginxinc/nginx-unprivileged:1.27-alpine AS runtime
ENV BACKEND_URL=http://backend:8000
COPY nginx.conf /etc/nginx/templates/default.conf.template
COPY index.html style.css app.js /usr/share/nginx/html/
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8080/ >/dev/null || exit 1
