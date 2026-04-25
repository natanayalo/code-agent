# Step 1: Base image
FROM node:22-slim AS base
WORKDIR /app
COPY package*.json ./

# Step 2: Dependencies
FROM base AS deps
# Use npm ci for faster, more reliable, and reproducible builds
RUN npm ci

# Step 3: Development
FROM deps AS dev
COPY . .
EXPOSE 3000
CMD ["npm", "run", "dev", "--", "--host"]

# Step 4: Builder
FROM deps AS builder
COPY . .
RUN npm run build

# Step 5: Production
FROM nginx:stable-alpine AS production
# Copy the custom nginx configuration for SPA routing and API proxying
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/dist /usr/share/nginx/html
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
