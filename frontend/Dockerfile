FROM docker.m.daocloud.io/library/node:20-alpine AS builder
WORKDIR /workspace
RUN npm config set registry https://registry.npmmirror.com \
    && npm config set fetch-retries 5 \
    && npm config set fetch-retry-mintimeout 20000 \
    && npm config set fetch-retry-maxtimeout 120000
COPY package*.json ./
RUN npm install --no-audit --no-fund --legacy-peer-deps \
    || (npm config set registry https://registry.npmjs.org \
        && npm cache clean --force \
        && npm install --no-audit --no-fund --legacy-peer-deps)
COPY . .
RUN npm run build

FROM docker.m.daocloud.io/library/nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /workspace/dist /usr/share/nginx/html
EXPOSE 80
