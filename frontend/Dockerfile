# syntax=docker/dockerfile:1.7
# Интерфейс: сборка Vite, раздача через nginx, /api — на сервис api.
# Контекст сборки — корень репозитория: конфиг nginx лежит в infra/nginx.

FROM node:22-alpine AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM nginx:1.27-alpine
COPY infra/nginx/default.conf /etc/nginx/conf.d/default.conf
COPY infra/nginx/security_headers.conf infra/nginx/api_headers.conf /etc/nginx/snippets/
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
