FROM node:18-alpine

WORKDIR /app

COPY package*.json ./
RUN apk add --no-cache ffmpeg \
	&& npm install --production

COPY . .

EXPOSE 3001

CMD ["npm", "start"]
