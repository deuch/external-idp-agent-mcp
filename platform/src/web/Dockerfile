# Static web client (simulates the mobile app). Non-root nginx listening on 8080.
FROM nginxinc/nginx-unprivileged:1.27-alpine

COPY default.conf.template /etc/nginx/templates/default.conf.template
COPY html/ /usr/share/nginx/html/

EXPOSE 8080
