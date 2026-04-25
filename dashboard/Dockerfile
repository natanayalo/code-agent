# Step 1: Base image
FROM node:22-slim AS base
WORKDIR /app
COPY package*.json ./

# Step 2: Dependencies
FROM base AS deps
RUN npm install

# Step 3: Development stage
FROM deps AS dev
COPY . .
EXPOSE 3000
CMD ["npm", "run", "dev", "--", "--host"]

# Step 4: Build for production
FROM deps AS builder
COPY . .
RUN npm run build

# Step 5: Production serving stage
FROM nginx:alpine AS production
COPY --from=builder /app/dist /usr/share/nginx/html
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
