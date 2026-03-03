І встав туди:

```python
from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def root():
    return {"status": "AI Voice Copilot is running"}
