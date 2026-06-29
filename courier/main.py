import uvicorn

from courier.api import create_app
from courier.config import load_settings

app = create_app()

if __name__ == "__main__":
    s = load_settings()
    uvicorn.run(app, host=s.host, port=s.port)
