FROM nginx:alpine

COPY index.html /usr/share/nginx/html/index.html

# Nginx proxy config: serve frontend and proxy /api to backend
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
