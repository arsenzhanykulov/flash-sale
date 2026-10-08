FROM node:24-alpine

WORKDIR /app

# Зависимости отдельным слоем: правка кода не вызывает повторный npm ci.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

EXPOSE 5173

# --host: слушать все интерфейсы, иначе с хоста не достучаться.
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
