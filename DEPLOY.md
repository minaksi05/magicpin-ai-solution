# Deployment

Local:
```bash
pip install -r requirements.txt
uvicorn bot:app --host 0.0.0.0 --port 8080
```

Docker:
```bash
docker build -t vera-bot .
docker run -p 8080:8080 vera-bot
```

Then expose port 8080 publicly and submit:
`https://YOUR-HOST`
